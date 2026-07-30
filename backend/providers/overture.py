"""Overture Maps 建筑轮廓数据源(全球,含中国)。

Overture buildings 主题融合了 OSM + Microsoft ML + Esri + Google Open Buildings,
全球约 24 亿栋,以 **GeoParquet** 形式公开托管在 S3(us-west-2),按月发布。

取数方式(关键):全球数据压缩后数百 GB,绝不能整包下载。Overture 的 Parquet
按空间排序且带 `bbox` STRUCT 列,配合 DuckDB 的**谓词下推**,取一个市级范围
通常只需传输几十到几百 MB——只读命中 bbox 的行组。

几何列是标准 WKB BLOB,直接用 shapely 解析即可,无需 DuckDB spatial 扩展
(只需 httpfs 访问 S3)。

中国区注意:
  - S3 直连在境内常超时,支持 config 配置 http_proxy;
  - 境内建筑 height 缺失率高于欧美(多来自微软 ML 无高度轮廓),
    高度补全策略见 core/buildings.py。
"""
from __future__ import annotations

import threading
import time
from typing import Iterator

from ..core.logs import logger
from .buildings import Building3DSource, BuildingFeature

# Overture 公开数据集 S3 位置(us-west-2,匿名可读)
OVERTURE_BUCKET = "overturemaps-us-west-2"
OVERTURE_REGION = "us-west-2"
OVERTURE_S3_BASE = f"s3://{OVERTURE_BUCKET}/release"

# 回退发布版本(自动探测失败时使用)。
# 注意:Overture 的 S3 桶**只保留最近约两个版本**,旧版本会被删除,
# 所以这个常量会过期——探测机制才是主路径,它只是兜底。
OVERTURE_FALLBACK_RELEASE = "2026-07-22.0"

# S3 REST 列举端点(匿名可读)。不用 DuckDB 的 glob():实测部分 Windows 环境下
# httpfs 的 S3 列举会卡数十秒后静默返回空列表,而同样的匿名 ListObjectsV2
# 走 HTTPS 只需 2 秒。故版本探测改由标准库直接发 REST 请求完成。
OVERTURE_LIST_URL = (
    f"https://{OVERTURE_BUCKET}.s3.{OVERTURE_REGION}.amazonaws.com/"
    "?list-type=2&max-keys=100&delimiter=/&prefix=release/"
)

# buildings 主题下的建筑要素分区路径。
# 用 /* 而非 /*.parquet:与 Overture 官方文档示例一致(文件名形如
# part-00000-....zstd.parquet,但目录下不含其他文件,通配更宽松更保险)。
BUILDINGS_SUBPATH = "theme=buildings/type=building/*"

# 单次查询最多返回的建筑数(防止误选超大范围把内存打爆;超出会截断并告警)
MAX_FEATURES = 2_000_000

_release_cache: str | None = None
_release_lock = threading.Lock()


#: 单条 DuckDB 查询的墙钟上限(秒)。超时即打断。
#: Overture 跨境查询可能几小时不返回,而 DuckDB 的 execute() 无法靠轮询
#: should_stop 中断——那会把单 worker 的任务队列彻底堵死(暂停/删除都失效)。
QUERY_DEADLINE = 600


def _execute_interruptible(con, sql: str, params=None, *,
                           should_stop=None, deadline: int = QUERY_DEADLINE):
    """执行查询,支持被取消标志或超时打断。

    DuckDB 的 execute() 是不可中断的阻塞调用,但 connection.interrupt() 可
    从另一个线程打断它。这里起一个看门狗线程轮询 should_stop 与截止时间,
    命中即 interrupt,让阻塞调用抛 InterruptException 返回。
    """
    import threading

    stop_watch = threading.Event()
    fired: dict[str, str] = {}

    def watchdog():
        t0 = time.time()
        while not stop_watch.wait(0.5):
            if should_stop and should_stop():
                fired["why"] = "cancel"
                con.interrupt()
                return
            if deadline and (time.time() - t0) > deadline:
                fired["why"] = "timeout"
                con.interrupt()
                return

    th = threading.Thread(target=watchdog, daemon=True)
    th.start()
    try:
        return con.execute(sql, params) if params is not None else con.execute(sql)
    except Exception as e:
        why = fired.get("why")
        if why == "timeout":
            raise TimeoutError(
                f"Overture 查询超过 {deadline} 秒未完成已中止。"
                "跨境读取 Overture 的 parquet 元数据极慢(实测单个分片 footer 约百秒、"
                "共 512 个分片),境内网络下基本不可用——建议改用 OSM 建筑轮廓数据源,"
                "或配置 buildings.proxy 后重试。"
            ) from e
        if why == "cancel":
            raise InterruptedError("查询已被取消") from e
        raise
    finally:
        stop_watch.set()


def _connect(proxy: str | None = None):
    """建立 DuckDB 连接并配置 httpfs 匿名访问 S3。

    httpfs 扩展首次使用需联网安装(装到用户目录,之后离线可用)。

    **不要设置 s3_access_key_id/secret**:Overture 是公开桶,要走匿名(unsigned)
    请求。显式置空字符串会让 DuckDB 认为"有凭据"从而做 SigV4 签名,空密钥签名
    会被 AWS 拒绝,且错误常被 glob 吞成空结果——这正是之前查不到文件的原因。
    仅设置 region 即可让 httpfs 走匿名访问。
    """
    import duckdb

    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs;")
    except Exception:
        pass          # 已安装或离线;下面 LOAD 失败才是真问题
    con.execute("LOAD httpfs;")
    con.execute(f"SET s3_region='{OVERTURE_REGION}';")
    if proxy:
        con.execute(f"SET http_proxy='{proxy}';")
    return con


def list_releases(proxy: str | None = None) -> list[str]:
    """列出 S3 上全部发布版本(升序)。失败抛异常并带上真实原因。

    用标准库直接发匿名 ListObjectsV2 请求解析 XML,不走 DuckDB 的 glob()
    (见 OVERTURE_LIST_URL 注释:glob 在部分环境会卡几十秒后返回空列表,
    把"列举失败"伪装成"列举到 0 个版本",极难排查)。
    """
    import re
    import urllib.request

    handlers = []
    if proxy:
        p = proxy if "://" in proxy else f"http://{proxy}"
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    opener = urllib.request.build_opener(*handlers)

    with opener.open(OVERTURE_LIST_URL, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"S3 列举返回 HTTP {resp.status}")
        xml = resp.read().decode("utf-8", "replace")

    # <CommonPrefixes><Prefix>release/2026-07-22.0/</Prefix></CommonPrefixes>
    names = re.findall(r"<Prefix>release/([^<]+?)/?</Prefix>", xml)
    # 仅保留形如 YYYY-MM-DD[.N] 的发布目录
    valid = {n for n in names if len(n) >= 10 and n[:4].isdigit() and n[4] == "-"}
    return sorted(valid)


def list_parquet_files(release: str, proxy: str | None = None) -> list[str]:
    """枚举某版本 buildings 分区下的全部 parquet 文件的 s3:// 路径。

    用途:`read_parquet('s3://.../*')` 里的通配符要靠 DuckDB 自己列举 S3,
    而该列举在部分环境不可用(见 OVERTURE_LIST_URL 注释)。此时改为把**显式
    文件列表**交给 read_parquet,完全绕开 DuckDB 的列举能力。

    ListObjectsV2 单次最多返回 1000 个 key,故按 continuation token 分页。
    """
    import re
    import urllib.parse
    import urllib.request

    handlers = []
    if proxy:
        p = proxy if "://" in proxy else f"http://{proxy}"
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    opener = urllib.request.build_opener(*handlers)

    prefix = f"release/{release}/theme=buildings/type=building/"
    base = f"https://{OVERTURE_BUCKET}.s3.{OVERTURE_REGION}.amazonaws.com/"
    keys: list[str] = []
    token: str | None = None

    while True:
        params = {"list-type": "2", "max-keys": "1000", "prefix": prefix}
        if token:
            params["continuation-token"] = token
        url = base + "?" + urllib.parse.urlencode(params)
        with opener.open(url, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"S3 列举返回 HTTP {resp.status}")
            xml = resp.read().decode("utf-8", "replace")
        keys.extend(k for k in re.findall(r"<Key>([^<]+)</Key>", xml)
                    if k.endswith(".parquet"))
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", xml)
        if not m:
            break
        token = m.group(1)

    return [f"s3://{OVERTURE_BUCKET}/{k}" for k in keys]


def probe_release(version: str, proxy: str | None = None) -> bool:
    """探测某版本是否真实可读(取一行即可,代价很低)。

    与取数走同一套回退:先通配路径,再显式文件列表。只要有一种可行就算可用,
    这样诊断结论才与实际取数行为一致。
    """
    candidates = [f"'{OVERTURE_S3_BASE}/{version}/{BUILDINGS_SUBPATH}'"]
    try:
        files = list_parquet_files(version, proxy)
        if files:
            # 探测只需一个文件即可判定可读性,不必把上百个路径都塞进 SQL
            candidates.append(f"['{files[0]}']")
    except Exception as e:
        logger.warning("枚举版本 %s 的 parquet 文件失败:%s", version, str(e)[:200])

    for source in candidates:
        con = _connect(proxy)
        try:
            # 探测给短期限:诊断接口要能较快返回,不能挂在这里几分钟
            _execute_interruptible(
                con, f"SELECT 1 FROM read_parquet({source}) LIMIT 1",
                deadline=90).fetchone()
            return True
        except Exception as e:
            logger.warning("Overture 版本 %s 以 %s 方式不可读:%s", version,
                           "显式文件" if source.startswith("[") else "通配路径",
                           str(e)[:200])
        finally:
            con.close()
    return False


def latest_release(proxy: str | None = None) -> str:
    """确定要用的发布版本。结果进程内缓存。

    顺序:S3 列举取最新 → 列举不可用时探测内置回退版本 → 都不行则抛错。
    绝不静默使用一个未经验证的版本号(那会让后续报错完全误导排查方向)。
    """
    global _release_cache
    with _release_lock:
        if _release_cache:
            return _release_cache

        list_err: Exception | None = None
        try:
            releases = list_releases(proxy)
            if releases:
                _release_cache = releases[-1]
                logger.info("Overture 可用发布 %d 个,选用最新:%s",
                            len(releases), _release_cache)
                return _release_cache
            logger.warning("Overture 版本列举返回空(可能匿名列举受限),改为探测内置版本")
        except Exception as e:
            list_err = e
            logger.warning("Overture 版本列举失败,改为探测内置版本:%s", str(e)[:200])

        # 列举不可用(部分网络环境禁 ListBucket,但 GetObject 可用):
        # 直接探测内置版本能否读到数据。
        if probe_release(OVERTURE_FALLBACK_RELEASE, proxy):
            _release_cache = OVERTURE_FALLBACK_RELEASE
            logger.info("Overture 采用内置版本:%s", _release_cache)
            return _release_cache

        raise RuntimeError(
            "无法确定 Overture 发布版本:S3 目录列举不可用"
            f"{f'({str(list_err)[:120]})' if list_err else ''},"
            f"且内置版本 {OVERTURE_FALLBACK_RELEASE} 也读不到数据。"
            "请在 config.yaml 的 buildings.release 手动指定一个有效版本"
            "(可用版本见 https://docs.overturemaps.org/release/latest/ ),"
            "或配置 buildings.proxy 后重试。"
        )


class OvertureBuildingSource(Building3DSource):
    """Overture Maps 建筑轮廓数据源。"""

    key = "overture_buildings"
    label = "Overture 建筑轮廓"

    def __init__(self, release: str | None = None, proxy: str | None = None):
        self.proxy = proxy or None
        self._release = release or None    # 未指定则延迟到首次使用时探测

    @property
    def release(self) -> str:
        """发布版本(延迟解析)。

        不在 __init__ 里探测:探测要联网、可能抛错,而构造发生在阶段循环之外,
        那里抛异常会让任务崩在队列 worker 里、而不是被记为「拉取建筑轮廓」
        阶段失败。延迟到 fetch/count 内部才解析,错误就能落到阶段上、可单独重试。
        """
        if not self._release:
            self._release = latest_release(self.proxy)
        return self._release

    # ---------- 内部 ----------

    def _dataset_url(self) -> str:
        """数据集 URL。只用 s3://——HTTPS 直读不支持路径通配,而 Overture
        一个版本下有上百个 part 文件,必须靠 glob 展开,故无 HTTPS 回退。
        """
        return f"{OVERTURE_S3_BASE}/{self.release}/{BUILDINGS_SUBPATH}"

    def _source_expr(self, explicit: bool) -> str:
        """read_parquet 的数据源表达式。

        explicit=False:单个通配路径(简洁,但要 DuckDB 自己列举 S3)
        explicit=True :显式文件列表(绕开 DuckDB 列举,由 REST 枚举得到)
        """
        if not explicit:
            return f"'{self._dataset_url()}'"
        files = list_parquet_files(self.release, self.proxy)
        if not files:
            raise RuntimeError(
                f"版本 {self.release} 下未找到任何 parquet 文件"
                "(该版本可能已从 S3 移除,Overture 只保留最近约两个版本)")
        logger.info("Overture 显式文件列表:%d 个 parquet", len(files))
        joined = ", ".join(f"'{f}'" for f in files)
        return f"[{joined}]"

    def _query_sql(self, source: str) -> str:
        """bbox 谓词下推查询。

        Overture 的 bbox 是 STRUCT(xmin,xmax,ymin,ymax);用相交条件而非
        中心点包含,避免漏掉跨边界的大建筑。
        """
        return f"""
            SELECT
                id,
                names.primary            AS name,
                height,
                num_floors,
                geometry                 AS wkb
            FROM read_parquet({source}, hive_partitioning=1)
            WHERE bbox.xmin <= ? AND bbox.xmax >= ?
              AND bbox.ymin <= ? AND bbox.ymax >= ?
        """

    def _open_cursor(self, bbox, should_stop=None):
        """执行 bbox 查询,返回已就绪的 DuckDB 连接(用 fetchmany 流式取)。

        先试通配路径;若 DuckDB 无法列举 S3(报 No files found 等),自动改用
        REST 枚举出的显式文件列表重试。查询可被取消/超时打断。
        """
        west, south, east, north = bbox
        # 谓词参数顺序对应 xmin<=east, xmax>=west, ymin<=north, ymax>=south
        params = [east, west, north, south]
        last_err: Exception | None = None
        for explicit in (False, True):
            try:
                source = self._source_expr(explicit)
                con = _connect(self.proxy)
                try:
                    _execute_interruptible(con, self._query_sql(source), params,
                                           should_stop=should_stop)
                except Exception:
                    con.close()
                    raise
                logger.info("Overture 查询已建立(release=%s,%s)", self.release,
                            "显式文件列表" if explicit else "通配路径")
                return con
            except (TimeoutError, InterruptedError):
                raise          # 超时/取消不再换方式重试(换了同样会慢/已被取消)
            except Exception as e:
                last_err = e
                logger.warning("Overture 查询失败(%s):%s",
                               "显式文件列表" if explicit else "通配路径",
                               str(e)[:200])
        raise RuntimeError(
            f"Overture 数据查询失败(release={self.release})。"
            f"已尝试通配路径与显式文件列表两种方式。"
            f"可在 config.yaml 的 buildings.release 指定有效版本"
            f"(见 /api/buildings/diagnose 返回的 releases_tail)、"
            f"或配置 buildings.proxy。原始错误:{last_err}"
        ) from last_err

    # ---------- 对外接口 ----------

    def fetch(self, bbox, *, on_progress=None, should_stop=None) -> Iterator[BuildingFeature]:
        """流式拉取建筑要素。

        用 fetchmany 分批取,避免一次性把结果集全load进内存;
        每批检查 should_stop,支持暂停/取消及时生效。
        """
        from shapely import wkb as _wkb

        con = self._open_cursor(bbox, should_stop=should_stop)
        fetched = 0
        truncated = False
        try:
            while True:
                if should_stop and should_stop():
                    logger.info("Overture 取数被中断,已拉取 %d 栋", fetched)
                    return
                rows = con.fetchmany(2000)
                if not rows:
                    break
                for fid, name, height, num_floors, raw in rows:
                    if raw is None:
                        continue
                    try:
                        geom = _wkb.loads(bytes(raw))
                    except Exception:
                        continue        # 单个要素几何损坏:跳过,不影响整批
                    for rings in _iter_polygon_rings(geom):
                        if not rings or len(rings[0]) < 3:
                            continue
                        yield BuildingFeature(
                            fid=str(fid or ""),
                            rings=rings,
                            height=float(height) if height is not None else None,
                            num_floors=int(num_floors) if num_floors is not None else None,
                            name=str(name or ""),
                        )
                    fetched += 1
                    if fetched >= MAX_FEATURES:
                        truncated = True
                        break
                if on_progress:
                    on_progress(fetched, None)
                if truncated:
                    logger.warning("建筑数超过上限 %d,已截断(请缩小范围)", MAX_FEATURES)
                    break
        finally:
            con.close()
        logger.info("Overture 取数完成:%d 栋建筑", fetched)

    def count(self, bbox) -> int:
        """COUNT(*) 预估范围内建筑数。

        仅用于进度分母,失败返回 0(未知)而不抛异常——真正的可用性判定交给
        fetch(),那里会给出可操作的错误信息,不必在计数阶段重复报错。
        """
        west, south, east, north = bbox
        # 与 _open_cursor 用同一套回退顺序,否则诊断结论会与实际取数不一致
        for explicit in (False, True):
            try:
                source = self._source_expr(explicit)
                con = _connect(self.proxy)
                try:
                    sql = f"""
                        SELECT count(*) FROM read_parquet({source}, hive_partitioning=1)
                        WHERE bbox.xmin <= ? AND bbox.xmax >= ?
                          AND bbox.ymin <= ? AND bbox.ymax >= ?
                    """
                    # 计数只是进度分母,给较短期限:拖太久不如直接放弃
                    cur = _execute_interruptible(
                        con, sql, [east, west, north, south], deadline=120)
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
                finally:
                    con.close()
            except Exception as e:
                logger.warning("Overture 计数失败(%s,不影响取数):%s",
                               "显式文件列表" if explicit else "通配路径",
                               str(e)[:200])
        return 0


def _iter_polygon_rings(geom):
    """把 shapely 几何拆成 [外环, 洞...] 的坐标环列表(逐个多边形 yield)。

    MultiPolygon 拆成多个;非面状几何忽略。环坐标去掉闭合的重复末点。
    """
    gtype = geom.geom_type
    if gtype == "Polygon":
        polys = [geom]
    elif gtype == "MultiPolygon":
        polys = list(geom.geoms)
    else:
        return
    for poly in polys:
        if poly.is_empty:
            continue
        rings = []
        for ring in [poly.exterior, *poly.interiors]:
            coords = list(ring.coords)
            if len(coords) >= 2 and coords[0] == coords[-1]:
                coords = coords[:-1]        # 去闭合重复点
            if len(coords) >= 3:
                rings.append([(float(x), float(y)) for x, y in coords])
        if rings:
            yield rings


# 数据源登记已上移到 providers/buildings.py(与具体实现解耦)。
# 这里保留同名转发,避免早先按 `from ..providers.overture import ...` 写的
# 导入失效。新代码请直接从 providers.buildings 导入。
from .buildings import (  # noqa: E402  (置于文件尾以避免循环导入)
    BUILDING_LAYERS,
    BUILDING_PROVIDERS,
    build_building_source,
    is_building_provider,
)

__all__ = [
    "OvertureBuildingSource", "list_releases", "list_parquet_files",
    "probe_release", "latest_release", "BUILDING_LAYERS", "BUILDING_PROVIDERS",
    "build_building_source", "is_building_provider",
    "OVERTURE_FALLBACK_RELEASE", "OVERTURE_S3_BASE",
]
