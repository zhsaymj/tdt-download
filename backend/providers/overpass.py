"""Overpass API 建筑轮廓数据源(OSM,境内可直连)。

为什么用它替代 Overture:Overture 是 512 个 parquet 分片、每个分片 footer 跨境
读要百秒量级,bbox 裁剪必须读遍全部 footer,单次查询十几小时——在境内网络下
不可行(实测详见沟通记录)。Overpass 是服务端做空间索引,按 bbox 直接返回命中
要素,一个城区响应仅几百 KB、秒级返回。

代价:覆盖率不如 Overture(后者融合了微软 AI 提取轮廓,郊区更全),
高度标签在国内也偏稀疏。高度补全策略见 core/buildings.py。

OSM 建筑的两种几何形态:
  way      简单闭合轮廓 → 单个外环
  relation type=multipolygon,成员按 role 分 outer/inner → 外环 + 洞
后者必须按 role 组装,否则口字楼、内院会被填实。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

from ..core.logs import logger
from .buildings import Building3DSource, BuildingFeature

# 公共镜像候选。**顺序不写死**:实测哪个快并不固定(同一次任务里,计数请求
# 主站 4 秒返回、社区镜像连吃两个 504;取数请求又是 private.coffee 先成功),
# 故改为按运行时实测表现动态排序,见 _MirrorStats / ordered_mirrors()。
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

#: 单次查询的服务端超时(秒),写进 QL 头
QUERY_TIMEOUT = 180
#: HTTP 读取超时(秒),留出比服务端超时更多余量
HTTP_TIMEOUT = 240
#: 单个查询块的最大跨度(度)。Overpass 对单次响应体积有限制,
#: 城区 0.05° 见方约几 MB,取 0.05 兼顾块数与单块体积。
MAX_CHUNK_DEG = 0.05
#: 镜像全部失败后的重试等待(秒),递增
RETRY_WAIT = [2, 5, 10]

#: 对冲延迟(秒):首选镜像超过这个时间没响应,就并行再叫下一个镜像,
#: 谁先成功用谁。实测单个 504 要等约 38 秒才返回,串行等待完全是浪费——
#: 一个小范围查询本身只需几秒,没必要为某个镜像的排队买单。
HEDGE_DELAY = 6.0
#: 同时在飞的最大请求数(对冲用),避免对公共实例造成不必要压力
MAX_INFLIGHT = 3

# 建筑相关标签:building 覆盖主体,building:part 是 3D 细分构件(如塔楼分段)
# 只取 building——building:part 会与主体重叠导致白模穿插。
OVERPASS_QL = """[out:json][timeout:{timeout}];
(
  way["building"]({s},{w},{n},{e});
  relation["building"]["type"="multipolygon"]({s},{w},{n},{e});
);
out geom;
"""


class _MirrorStats:
    """镜像表现统计,按实测动态排序候选顺序。

    公共 Overpass 实例的可用性随时波动:同一次任务里,某个镜像可能这一秒
    4 秒返回、下一秒连吃 504。写死顺序必然经常押错,所以改成"用实测说话":
    成功且快的排前面,刚失败过的排后面并在冷却期内降权。

    统计只存在进程内(不落盘):镜像状态本就是短时效信息,重启后重新探明即可。
    """

    #: 失败后的冷却时长(秒);冷却期内该镜像被降权但不完全排除
    COOLDOWN = 120.0
    #: 平均耗时的滑动权重(新样本占比),越大越跟随近期表现
    ALPHA = 0.4

    def __init__(self):
        self._lock = threading.Lock()
        # url -> {avg: 平均耗时秒, ok: 成功次数, fail: 失败次数, last_fail: 时间戳}
        self._s: dict[str, dict] = {}

    def _get(self, url: str) -> dict:
        st = self._s.get(url)
        if st is None:
            st = self._s[url] = {"avg": None, "ok": 0, "fail": 0, "last_fail": 0.0}
        return st

    def record_ok(self, url: str, elapsed: float) -> None:
        with self._lock:
            st = self._get(url)
            st["ok"] += 1
            st["avg"] = elapsed if st["avg"] is None \
                else st["avg"] * (1 - self.ALPHA) + elapsed * self.ALPHA

    def record_fail(self, url: str) -> None:
        with self._lock:
            st = self._get(url)
            st["fail"] += 1
            st["last_fail"] = time.time()

    def _score(self, url: str, now: float) -> tuple:
        """排序键:(是否冷却中, 预期耗时)。越小越优先。"""
        st = self._s.get(url)
        if st is None:
            # 没测过的给中等预期,让它有机会被试到(否则永远排在已知快的后面)
            return (0, 10.0)
        cooling = 1 if (now - st["last_fail"]) < self.COOLDOWN else 0
        # 没有成功记录时按"较慢"处理,但不排除
        expect = st["avg"] if st["avg"] is not None else 20.0
        return (cooling, expect)

    def ordered(self, urls: list[str]) -> list[str]:
        now = time.time()
        with self._lock:
            return sorted(urls, key=lambda u: self._score(u, now))

    def summary(self) -> str:
        now = time.time()
        parts = []
        with self._lock:
            for u, st in self._s.items():
                host = u.split("//")[-1].split("/")[0]
                avg = f"{st['avg']:.1f}s" if st["avg"] is not None else "—"
                cd = "冷却中" if (now - st["last_fail"]) < self.COOLDOWN else ""
                parts.append(f"{host} 均{avg} 成{st['ok']}/败{st['fail']}{cd}")
        return "; ".join(parts) or "(无统计)"


#: 进程内共享:多个任务/多个块的探测结果互相受益
mirror_stats = _MirrorStats()


def _split_bbox(bbox, max_deg: float = MAX_CHUNK_DEG):
    """把 bbox 切成不超过 max_deg 见方的块列表(避免单次响应过大被服务端截断)。"""
    west, south, east, north = bbox
    import math
    # 减一个相对容差再 ceil:否则 (116.2-116.0)/0.05 因浮点误差得 4.0000000000000057,
    # ceil 成 5,多切出没有实际宽度的薄片块,每块都会白发一次网络请求。
    EPS = 1e-9
    nx = max(1, math.ceil((east - west) / max_deg - EPS))
    ny = max(1, math.ceil((north - south) / max_deg - EPS))
    dx = (east - west) / nx
    dy = (north - south) / ny
    out = []
    for i in range(nx):
        for j in range(ny):
            out.append((
                west + i * dx, south + j * dy,
                west + (i + 1) * dx, south + (j + 1) * dy,
            ))
    return out


class OverpassBuildingSource(Building3DSource):
    """Overpass API(OSM)建筑轮廓数据源。"""

    key = "osm_buildings"
    label = "OSM 建筑轮廓(Overpass)"

    def __init__(self, mirrors: list[str] | None = None, proxy: str | None = None,
                 max_chunk_deg: float = MAX_CHUNK_DEG):
        self.mirrors = mirrors or list(OVERPASS_MIRRORS)
        self.proxy = proxy or None
        self.max_chunk_deg = max_chunk_deg
        self._opener = self._build_opener()

    def _build_opener(self):
        handlers = []
        if self.proxy:
            p = self.proxy if "://" in self.proxy else f"http://{self.proxy}"
            handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
        return urllib.request.build_opener(*handlers)

    # ---------- 网络 ----------

    def _one_request(self, url: str, data: bytes) -> dict:
        """向单个镜像发一次查询,成功返回解析后的 JSON,失败抛异常。

        同时把耗时记入 mirror_stats,用于后续排序。
        """
        t0 = time.time()
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    # Overpass 要求可识别的 UA,匿名 UA 易被限流
                    "User-Agent": "tianditu-downloader/1.0 (building footprints)",
                })
            with self._opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            mirror_stats.record_ok(url, time.time() - t0)
            return payload
        except Exception:
            mirror_stats.record_fail(url)
            raise

    def _post(self, ql: str, should_stop=None) -> dict:
        """向镜像发查询:按实测表现排序 + 对冲并发,首个成功即返回。

        为什么要对冲:公共实例返回 504 往往要等约 38 秒,而查询本身只需几秒。
        串行等待会把一次小查询拖到两分多钟(实测 11 栋建筑耗时 155 秒,其中
        约 130 秒纯粹是在等 504)。这里首选镜像超过 HEDGE_DELAY 未响应就并行
        叫下一个,谁先成功用谁——用少量额外请求换掉大段无谓等待。

        每轮结束检查 should_stop:任务队列是单 worker,长阻塞会让暂停/删除失效。
        """
        data = urllib.parse.urlencode({"data": ql}).encode("utf-8")
        last_err: Exception | None = None

        for wait in [0, *RETRY_WAIT]:
            if wait:
                # 分段睡眠,便于及时响应取消
                for _ in range(int(wait * 2)):
                    if should_stop and should_stop():
                        raise InterruptedError("取数已被取消")
                    time.sleep(0.5)
            if should_stop and should_stop():
                raise InterruptedError("取数已被取消")

            order = mirror_stats.ordered(self.mirrors)
            try:
                return self._hedged_round(order, data, should_stop)
            except InterruptedError:
                raise
            except Exception as e:
                last_err = e
                logger.warning("Overpass 本轮全部镜像失败(%s),镜像状态:%s",
                               str(e)[:120], mirror_stats.summary())

        raise RuntimeError(
            f"Overpass 查询失败(已尝试 {len(self.mirrors)} 个镜像、"
            f"{len(RETRY_WAIT) + 1} 轮)。若在受限网络下可在 config.yaml 配置 "
            f"buildings.proxy。原始错误:{last_err}")

    def _hedged_round(self, order: list[str], data: bytes, should_stop=None) -> dict:
        """一轮对冲请求:按 order 依次投放(间隔 HEDGE_DELAY),取最先成功的结果。

        用守护线程 + 队列而非 ThreadPoolExecutor:后者的 with 退出会
        shutdown(wait=True) 阻塞等待所有在飞请求结束——慢镜像要等满 30 秒,
        对冲就完全失去意义;且它的工作线程是非守护线程,解释器退出时会 join,
        后端重启可能被拖住最多 HTTP_TIMEOUT。守护线程两个问题都没有:
        主流程拿到首个成功结果就走,剩下的请求自行超时结束。
        """
        import queue as _queue

        urls = order[:MAX_INFLIGHT] or list(order)
        results: _queue.Queue = _queue.Queue()

        def worker(u: str):
            try:
                results.put(("ok", u, self._one_request(u, data)))
            except Exception as e:      # noqa: BLE001 - 汇总到主流程统一处理
                results.put(("err", u, e))

        last_err: Exception | None = None
        launched = 0
        outstanding = 0
        while True:
            if should_stop and should_stop():
                raise InterruptedError("取数已被取消")
            # 还有未投放的镜像 → 投放一个
            if launched < len(urls):
                u = urls[launched]
                launched += 1
                outstanding += 1
                if launched > 1:
                    logger.info("Overpass 对冲:并行尝试 %s",
                                u.split("//")[-1].split("/")[0])
                threading.Thread(target=worker, args=(u,), daemon=True,
                                 name=f"overpass-{launched}").start()
            if outstanding <= 0:
                break
            # 还有待投放的 → 只等 HEDGE_DELAY,到点就再加一个候选;
            # 已全部投放 → 分段等待(便于响应取消)
            timeout = HEDGE_DELAY if launched < len(urls) else 1.0
            try:
                kind, u, payload = results.get(timeout=timeout)
            except _queue.Empty:
                continue          # 到点未响应:回到循环顶部投放下一个候选
            outstanding -= 1
            if kind == "ok":
                logger.info("Overpass 采用 %s 的响应",
                            u.split("//")[-1].split("/")[0])
                return payload
            last_err = payload
            if isinstance(payload, urllib.error.HTTPError):
                logger.warning("Overpass %s 返回 HTTP %s", u, payload.code)
            else:
                logger.warning("Overpass %s 请求失败:%s", u, str(payload)[:160])

        raise last_err or RuntimeError("Overpass 无可用镜像")

    # ---------- 对外接口 ----------

    def fetch(self, bbox, *, on_progress=None, should_stop=None) -> Iterator[BuildingFeature]:
        """按 bbox 分块拉取 OSM 建筑。

        逐块请求并即时 yield,不在内存里累积整个范围的原始响应。
        块间做 should_stop 检查,暂停能较快生效(单块请求不可中断)。
        """
        chunks = _split_bbox(bbox, self.max_chunk_deg)
        logger.info("Overpass 取数:范围切为 %d 块(每块≤%.3f°)",
                    len(chunks), self.max_chunk_deg)
        fetched = 0
        seen: set[str] = set()      # 跨块去重:跨边界建筑会在相邻块重复出现

        for idx, (w, s, e, n) in enumerate(chunks, start=1):
            if should_stop and should_stop():
                logger.info("Overpass 取数被中断,已拉取 %d 栋", fetched)
                return
            ql = OVERPASS_QL.format(timeout=QUERY_TIMEOUT, s=s, w=w, n=n, e=e)
            payload = self._post(ql, should_stop=should_stop)
            elements = payload.get("elements") or []
            logger.info("Overpass 第 %d/%d 块:返回 %d 个要素",
                        idx, len(chunks), len(elements))

            for el in elements:
                feat = _element_to_feature(el)
                if feat is None:
                    continue
                if feat.fid in seen:
                    continue        # 跨块重复
                seen.add(feat.fid)
                fetched += 1
                yield feat

            if on_progress:
                on_progress(fetched, None)

        logger.info("Overpass 取数完成:%d 栋建筑(%d 块)", fetched, len(chunks))

    def count(self, bbox) -> int:
        """用 out count 取建筑数,作为进度分母。失败返回 0(未知)。

        单块范围直接返回 0 跳过:那种情况下 fetch 只发一次请求,分母在拿到
        结果时自然就知道了,专门再跑一次查询是纯粹的浪费——实测这个预查询
        在镜像不稳时要花 80 秒,只为得到"11 栋"这个数字。
        """
        chunks = _split_bbox(bbox, self.max_chunk_deg)
        if len(chunks) <= 1:
            logger.info("Overpass 单块范围,跳过预计数(分母由取数结果确定)")
            return 0

        west, south, east, north = bbox
        ql = (f"[out:json][timeout:{QUERY_TIMEOUT}];"
              f'(way["building"]({south},{west},{north},{east});'
              f'relation["building"]["type"="multipolygon"]'
              f"({south},{west},{north},{east}););out count;")
        try:
            payload = self._post(ql)
            for el in payload.get("elements") or []:
                tags = el.get("tags") or {}
                total = tags.get("total")
                if total is not None:
                    return int(total)
        except Exception as e:
            logger.warning("Overpass 计数失败(不影响取数):%s", str(e)[:200])
        return 0


# ---------- 标签解析 ----------

def _parse_height(tags: dict) -> float | None:
    """解析 OSM height 标签。

    OSM 里 height 单位默认为米,但实际数据常见带单位后缀:
      "25"  "25 m"  "25m"  "82'"(英尺)  "12.5"
    带引号的英尺值要换算。无法解析返回 None(交由上层按层数/面积补全)。
    """
    raw = tags.get("height") or tags.get("building:height")
    if not raw:
        return None
    s = str(raw).strip().lower().replace(",", ".")
    try:
        if s.endswith("'"):                     # 英尺
            return float(s[:-1]) * 0.3048
        if s.endswith("ft"):
            return float(s[:-2].strip()) * 0.3048
        if s.endswith("m"):
            s = s[:-1].strip()
        return float(s)
    except (ValueError, TypeError):
        return None


def _parse_floors(tags: dict) -> int | None:
    """解析层数。building:levels 可能是 "5"、"5.5"、"3;4"(多值)。

    多值取最大(建筑整体高度以最高部分为准);地下层 building:levels:underground
    不计入地上高度。
    """
    raw = tags.get("building:levels") or tags.get("levels")
    if not raw:
        return None
    best: float | None = None
    for part in str(raw).replace(",", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        try:
            v = float(part)
        except (ValueError, TypeError):
            continue
        if v > 0 and (best is None or v > best):
            best = v
    if best is None:
        return None
    return max(1, int(round(best)))


def _closed_ring(coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """去掉闭合重复的末点(上层约定环不闭合)。"""
    if len(coords) >= 2 and abs(coords[0][0] - coords[-1][0]) < 1e-12 \
            and abs(coords[0][1] - coords[-1][1]) < 1e-12:
        return coords[:-1]
    return coords


def _geom_of(el: dict) -> list[tuple[float, float]]:
    """从 out geom 的 geometry 数组取 (lon, lat) 序列。"""
    return [(float(p["lon"]), float(p["lat"]))
            for p in (el.get("geometry") or [])
            if p and p.get("lon") is not None and p.get("lat") is not None]


def _stitch_rings(segments: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """把 relation 成员的线段拼成闭合环。

    OSM multipolygon 的一个环可能由多条 way 拼成,且方向不定。用端点匹配
    逐段接续:取一段作起点,反复找与当前尾端相接的下一段(必要时反转),
    首尾相接即闭合成环。拼不上的残段丢弃(数据本身不完整)。
    """
    EPS = 1e-9

    def same(a, b):
        return abs(a[0] - b[0]) < EPS and abs(a[1] - b[1]) < EPS

    pool = [list(s) for s in segments if len(s) >= 2]
    rings: list[list[tuple[float, float]]] = []

    while pool:
        cur = pool.pop(0)
        closed = False
        while not closed:
            if same(cur[0], cur[-1]):
                closed = True
                break
            # 找能接到尾端的段
            for i, seg in enumerate(pool):
                if same(cur[-1], seg[0]):
                    cur.extend(seg[1:])
                    pool.pop(i)
                    break
                if same(cur[-1], seg[-1]):
                    cur.extend(list(reversed(seg))[1:])
                    pool.pop(i)
                    break
            else:
                break        # 无可接续段:该环不完整
        if closed:
            ring = _closed_ring(cur)
            if len(ring) >= 3:
                rings.append(ring)
    return rings


def _element_to_feature(el: dict) -> BuildingFeature | None:
    """把一个 Overpass 要素转成 BuildingFeature。不可用返回 None。"""
    tags = el.get("tags") or {}
    if "building" not in tags:
        return None
    # building=no 明确表示不是建筑
    if str(tags.get("building")).lower() == "no":
        return None

    etype = el.get("type")
    eid = el.get("id")
    fid = f"{etype}/{eid}"

    if etype == "way":
        ring = _closed_ring(_geom_of(el))
        if len(ring) < 3:
            return None
        rings = [ring]
    elif etype == "relation":
        # 按 role 分组:outer 拼外环,inner 拼洞
        outer_seg, inner_seg = [], []
        for m in el.get("members") or []:
            if m.get("type") != "way":
                continue
            g = _geom_of(m)
            if len(g) < 2:
                continue
            role = (m.get("role") or "outer").lower()
            (inner_seg if role == "inner" else outer_seg).append(g)
        outers = _stitch_rings(outer_seg)
        inners = _stitch_rings(inner_seg)
        if not outers:
            return None
        # 一个 relation 可能含多个外环(多体建筑);取最大的那个,
        # 其余舍弃——上层 BuildingFeature 约定 rings[0] 为单一外环。
        outers.sort(key=_ring_abs_area, reverse=True)
        rings = [outers[0], *inners]
    else:
        return None

    return BuildingFeature(
        fid=fid,
        rings=rings,
        height=_parse_height(tags),
        num_floors=_parse_floors(tags),
        name=tags.get("name") or "",
    )


def _ring_abs_area(ring: list[tuple[float, float]]) -> float:
    """环的鞋带面积绝对值(度²,仅用于比较大小,不需要真实单位)。"""
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


# provider 登记
OSM_BUILDING_PROVIDERS = {"osm_buildings"}


def build_overpass_source(proxy: str | None = None,
                          mirrors: list[str] | None = None,
                          max_chunk_deg: float = MAX_CHUNK_DEG) -> OverpassBuildingSource:
    return OverpassBuildingSource(mirrors=mirrors, proxy=proxy,
                                  max_chunk_deg=max_chunk_deg)
