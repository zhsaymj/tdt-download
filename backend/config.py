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
class Config:
    tianditu: TiandituConfig = field(default_factory=TiandituConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    buildings: BuildingsConfig = field(default_factory=BuildingsConfig)

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

    # 环境变量可覆盖密钥,便于不落盘
    env_token = os.environ.get("TIANDITU_TOKEN")
    if env_token:
        cfg.tianditu.token = env_token
    env_basemap = os.environ.get("TIANDITU_BASEMAP_TOKEN")
    if env_basemap:
        cfg.tianditu.basemap_token = env_basemap

    return cfg


# 全局单例
settings = load_config()
