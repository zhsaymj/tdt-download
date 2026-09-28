"""配置加载:优先读取 config.yaml,缺失项回落到默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .paths import runtime_root

# 可写数据根:开发时为项目根,打包后为 exe 所在目录。
# output/data/config.yaml 等可写内容都相对它解析。
ROOT = runtime_root()


@dataclass
class TiandituConfig:
    token: str = ""            # 下载用密钥
    basemap_token: str = ""    # 底图显示用密钥(与下载密钥区分,便于分别控配额)

    def basemap_or_download(self) -> str:
        """底图密钥缺省时回落到下载密钥。"""
        return self.basemap_token or self.token


@dataclass
class DownloadConfig:
    concurrency: int = 8
    max_retries: int = 3
    timeout: int = 30
    cache_dir: str = "./data/tiles"


@dataclass
class OutputConfig:
    dir: str = "./output"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass
class BuildingsConfig:
    """三维建筑白模数据源配置(OSM/Overpass 与 Overture)。"""
    # Overture 发布版本;留空则自动探测最新版(失败回退代码内置的已知版本)
    release: str = ""
    # HTTP 代理(如 "127.0.0.1:7890")。Overture 托管在 AWS S3,境内直连常超时
    proxy: str = ""
    # Overpass 镜像列表(逗号分隔或 YAML 列表)。留空用内置默认。
    # 公共实例负载波动大,可在此指定更快的实例或自建实例。
    overpass_mirrors: str | list = ""
    # 单次 Overpass 查询的范围分块跨度(度)。范围大时块数按面积增长;
    # 城区建筑密集可调小(避免单块响应过大被服务端截断)。
    overpass_chunk_deg: float = 0.05
    # 建筑轮廓本地缓存有效期(天)。0 = 永不过期(只能手动刷新)。
    # 按 0.05° 网格逐格缓存,第二次下载同城区几乎零网络请求。
    cache_ttl_days: float = 30.0

    # ---- 在线建筑数据包 ----
    # 已准备好的网格数据包目录地址(公开读)。这是**使用机器唯一需要配的项**:
    # 配好后按下载范围按需取数(几十 KB 起),不必导入 pbf。
    # 目录结构:<remote_url>/<source>/index.json 与 b_{row}_{col}.json.gz
    remote_url: str = ""

    # ---- 以下仅"准备数据"的机器需要(update-building-data.bat)----
    # 本地 osm.pbf 路径。需自行下载(如 Geofabrik 的 china-latest.osm.pbf),
    # 工具不联网获取它——只在准备数据时用一次,不值得做下载/续传那套东西。
    pbf_path: str = "./data/pbf/china.osm.pbf"


    def mirror_list(self) -> list[str] | None:
        """归一化镜像配置为列表;未配置返回 None(用内置默认)。"""
        v = self.overpass_mirrors
        if not v:
            return None
        items = v if isinstance(v, list) else str(v).split(",")
        out = [str(x).strip() for x in items if str(x).strip()]
        return out or None


@dataclass
class ToolsConfig:
    """外部三维处理器路径配置。留空表示未配置,诊断接口会跳过试跑。"""
    tiles3d_exe: str = ""        # fanvanzh/3dtiles 可执行文件路径(tools/3dtiles/3dtiles.exe)
    pdal_exe: str = ""           # pdal CLI 路径
    py3dtiles_python: str = ""   # 装有 py3dtiles 的 python 解释器(独立 venv,避免污染主环境)
    dsm2dtm_python: str = ""     # 预留,一期不用


@dataclass
class WorkerConfig:
    """worker 进程池配置。

    num_workers 是能**同时执行**的任务数。单个任务内部已有瓦片级并发,
    默认 2 可并行跑两个任务;代价是内存(每个常驻 worker 约 60MB)与
    磁盘 I/O 竞争 —— 大范围任务同时跑会加速磁盘消耗。
    """
    num_workers: int = 2


@dataclass
class GoogleConfig:
    """Google 影像(非官方瓦片端点)。

    实测直连不通(超时),**必须配代理**。端点忽略 key 参数(带/不带/空 key
    返回字节完全相同的瓦片),故刻意不设 key 字段,避免"填了 key 才有权限"的误解。
    """
    enabled: bool = False
    # HTTP 代理。可写 "127.0.0.1:6789" 或 "http://127.0.0.1:6789",
    # 代码会补 scheme(见 providers/base.normalize_proxy)。不支持 socks5。
    proxy: str = ""
    # 非官方端点会变更(实测旧 khms 端点 v=1000 已返回 404),故可配置。
    url_template: str = "https://mt{s}.google.com/vt/lyrs={lyrs}&x={x}&y={y}&z={z}"
    subdomains: str = "0,1,2,3"
    # 实测陆地处处可到 z21(含西部城市),z22 仅部分地区有 —— 21 是全球陆地
    # 可用的临界值。无地区性降级,故不需要按区域探测。
    max_zoom: int = 21

    def subdomain_list(self) -> list[str]:
        """归一化子域名配置为列表;未配置时回落默认。"""
        items = [x.strip() for x in str(self.subdomains or "").split(",")]
        return [x for x in items if x] or ["0", "1", "2", "3"]


@dataclass
class EsriImageryConfig:
    """Esri World Imagery。

    与项目现用的 Esri Terrain3D(DEM)是**不同服务**:Terrain3D 直连可用,
    World Imagery 实测直连不通,必须配代理。
    """
    enabled: bool = False
    proxy: str = ""
    url_template: str = ("https://services.arcgisonline.com/ArcGIS/rest/services"
                         "/World_Imagery/MapServer/tile/{z}/{y}/{x}")
    # 服务级天花板。实测 z19 是亚欧城市的实际上限(z20 仅美国境内有),
    # 且 z19 载有真实新增细节(高频能量比 z18 上采样高 33~46%),不是插值放大。
    max_zoom: int = 19
    # 是否按选区探测该地区的实际最高级别。默认开启:实测西藏/青海/新疆无人区
    # z18 即无影像(最高 z17),不探测的话用户选 z18 会下到一整片灰色占位图。
    probe_max_zoom: bool = True
    # 探测结果按量化 bbox 缓存的有效期(小时)。0 = 不缓存。
    # 探测是逐级网络请求,用户拖拽选区会连续触发,缓存不是优化而是必需。
    probe_cache_hours: float = 24.0


@dataclass
class Config:
    tianditu: TiandituConfig = field(default_factory=TiandituConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    buildings: BuildingsConfig = field(default_factory=BuildingsConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    google: GoogleConfig = field(default_factory=GoogleConfig)
    esri_imagery: EsriImageryConfig = field(default_factory=EsriImageryConfig)

    def abs_path(self, rel: str) -> Path:
        """把配置里的相对路径解析为基于项目根目录的绝对路径。"""
        p = Path(rel)
        return p if p.is_absolute() else (ROOT / p).resolve()

    @property
    def cache_dir(self) -> Path:
        return self.abs_path(self.download.cache_dir)

    @property
    def output_dir(self) -> Path:
        return self.abs_path(self.output.dir)


def _merge(dc, data: dict):
    """把 dict 中的已知字段覆盖到 dataclass 实例上。"""
    for key, value in (data or {}).items():
        if hasattr(dc, key):
            setattr(dc, key, value)


# 首次启动自动生成的空配置模板(不含任何真实密钥)。
_CONFIG_TEMPLATE = """# 天地图下载处理工具 - 配置文件
# 首次启动自动生成。请填写自己的天地图密钥,或在界面「密钥管理」中添加。

tianditu:
  # 天地图访问密钥(下载用)。申请地址:https://console.tianditu.gov.cn/api/key
  token: ""
  # 底图显示用密钥。与下载密钥区分,分别控配额;留空则回落到 token。
  basemap_token: ""

download:
  concurrency: 8       # 单任务最大并发下载数
  max_retries: 3       # 单张瓦片失败最大重试次数
  timeout: 30          # 单张瓦片请求超时(秒)
  cache_dir: "./data/tiles"   # 瓦片缓存目录(断点续传依赖)

output:
  dir: "./output"      # 成果输出根目录

server:
  host: "127.0.0.1"
  port: 8000

buildings:
  # 三维建筑白模。默认数据源为 OSM(Overpass),境内可直连。
  # Overpass 镜像,留空用内置默认(社区镜像优先,主站最后)
  overpass_mirrors: ""
  # 范围分块跨度(度),城区建筑密集可调小
  overpass_chunk_deg: 0.05
  # 建筑轮廓本地缓存有效期(天),0=永不过期
  cache_ttl_days: 30
  # 在线建筑数据包目录(公开读)。**使用机器只需配这一项**:
  # 配好后按下载范围按需取数(一个城区几十 KB),不必准备 pbf。
  # remote_url: "https://your-bucket.cos.ap-guangzhou.myqcloud.com/building-china"
  # 仅"准备数据"的机器需要:本地 osm.pbf 路径(自行下载,工具不联网获取)。
  # 准备流程见 update-building-data.bat
  pbf_path: "./data/pbf/china.osm.pbf"
  # 以下仅 Overture 数据源用到(需境外网络)
  release: ""          # 发布版本,留空自动探测最新版
  proxy: ""            # HTTP 代理,如 "127.0.0.1:7890";境内直连 S3 常超时

tools:
  # 外部三维处理器路径。留空表示未配置;三维任务提交前可访问 /api/tools/diagnose 自检。
  tiles3d_exe: ""        # fanvanzh/3dtiles 可执行文件,如 "tools/3dtiles/3dtiles.exe"
  pdal_exe: ""           # pdal CLI,如 "tools/pdal/bin/pdal.exe"
  py3dtiles_python: ""   # 装有 py3dtiles 的独立 venv 解释器,如 "tools/py3dtiles-venv/Scripts/python.exe"

google:
  # Google 影像(非官方瓦片端点)。实测直连不通,必须配代理。
  enabled: false
  # 代理地址。可写 "127.0.0.1:6789" 或带 scheme 的完整 URL,两种都行。
  # 注意:不支持 socks5(aiohttp 不内置 SOCKS 支持)。
  proxy: ""
  # 端点会变更,失效时先改这里。不设 key 字段:该端点忽略 key 参数。
  url_template: "https://mt{s}.google.com/vt/lyrs={lyrs}&x={x}&y={y}&z={z}"
  subdomains: "0,1,2,3"
  max_zoom: 21          # 实测陆地处处可用到 21;22 级仅部分地区有

esri_imagery:
  # Esri World Imagery。与现用的 Esri Terrain3D(DEM)是不同服务:
  # Terrain3D 直连可用,World Imagery 实测直连不通,必须配代理。
  enabled: false
  proxy: ""
  url_template: "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
  # 服务级天花板。实测 z19 是亚欧城市上限(z20 仅美国境内有)。
  max_zoom: 19
  # 按选区探测该地区实际最高级别。实测西部无人区最高仅 z17,
  # 关掉后用户选 z18 会下到一整片灰色占位图。
  probe_max_zoom: true
  probe_cache_hours: 24
"""


def _ensure_config_file(cfg_path: Path) -> None:
    """config.yaml 不存在时,在运行根写一份空模板(不含真实密钥),便于用户编辑填密钥。"""
    if cfg_path.exists():
        return
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    except OSError:
        pass


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg_path = Path(path) if path else (ROOT / "config.yaml")
    _ensure_config_file(cfg_path)
    cfg = Config()

    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        _merge(cfg.tianditu, raw.get("tianditu"))
        _merge(cfg.download, raw.get("download"))
        _merge(cfg.output, raw.get("output"))
        _merge(cfg.server, raw.get("server"))
        _merge(cfg.buildings, raw.get("buildings"))
        _merge(cfg.tools, raw.get("tools"))
        _merge(cfg.worker, raw.get("worker"))
        _merge(cfg.google, raw.get("google"))
        _merge(cfg.esri_imagery, raw.get("esri_imagery"))

    # 环境变量可覆盖密钥,便于不落盘
    env_token = os.environ.get("TIANDITU_TOKEN")
    if env_token:
        cfg.tianditu.token = env_token
    env_basemap = os.environ.get("TIANDITU_BASEMAP_TOKEN")
    if env_basemap:
        cfg.tianditu.basemap_token = env_basemap
    # worker 数也可用环境变量覆盖(非密钥,但同样省得改配置文件);
    # 非法值静默忽略而非抛错:配置系统整体是"缺失/写坏则回落默认值"的语义,
    # 为一个可选调优项在启动路径上抛异常得不偿失。
    env_workers = os.environ.get("NUM_WORKERS")
    if env_workers:
        try:
            cfg.worker.num_workers = max(1, int(env_workers))
        except ValueError:
            pass
    # 代理可用环境变量覆盖(便于临时切换而不改配置文件)。
    # spawn 下子进程继承环境变量,worker 里重新 load_config 同样读得到。
    env_gproxy = os.environ.get("GOOGLE_PROXY")
    if env_gproxy:
        cfg.google.proxy = env_gproxy
    env_eproxy = os.environ.get("ESRI_PROXY")
    if env_eproxy:
        cfg.esri_imagery.proxy = env_eproxy

    return cfg


# 全局单例
settings = load_config()
