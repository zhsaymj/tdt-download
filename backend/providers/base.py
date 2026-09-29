"""数据源抽象基类。

影像(天地图)是第一个实现;DEM 等后续数据源实现同一接口后,
下载器与拼接管线即可复用,无需改动上层逻辑。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


def normalize_proxy(raw: str | None) -> str | None:
    """把配置里的代理串归一化为 aiohttp 可用的 URL;空值返回 None(直连)。

    aiohttp 的 proxy 参数必须是带 scheme 的完整 URL —— 传 "127.0.0.1:6789"
    会抛 InvalidURL(实测 3.11.11)。用户在 config.yaml 里习惯只写 host:port
    (buildings.proxy 现有配置就是这个形式),故这里补 scheme,而不是让用户改写法。

    socks5:// 原样返回但**不受支持**:aiohttp 不内置 SOCKS(需 aiohttp-socks)。
    这里刻意不静默降级成 http —— 那会连到 SOCKS 端口发 HTTP 请求,报出的错
    与真正病因毫无关系。原样传下去让 aiohttp 自己报 scheme 不支持,更好排查。

    放在 base 而非各 provider 内:worker 里的下载器与主进程里的预览端点
    都要用同一份归一化结果,复制两份必然漂移。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    return f"http://{raw}"


class TileProvider(ABC):
    """瓦片数据源接口。"""

    #: 数据源标识,如 "tianditu_img"
    key: str = "base"
    #: 瓦片文件后缀,如 "png" / "jpg" / "tif"
    ext: str = "png"
    #: 波段数(RGB=3,RGBA=4,DEM 单波段=1),供拼接模块使用
    bands: int = 3

    @property
    def headers(self) -> dict:
        """下载该数据源瓦片时附带的 HTTP 请求头(默认无;子类可覆盖)。"""
        return {}

    @property
    def proxy(self) -> str | None:
        """下载该数据源瓦片时使用的 HTTP 代理 URL(默认无;子类可覆盖)。

        返回值必须已归一化(带 scheme),可直接传给 aiohttp —— 子类应当返回
        normalize_proxy(cfg.proxy) 而不是 cfg.proxy 本身。

        刻意做成 provider 属性而非全局开关:天地图与 Esri Terrain3D 直连可用,
        若用全局代理(或 aiohttp 的 trust_env),用户为别的软件设的系统代理会把
        它们也绕进去 —— 那是现有功能的静默回归。
        """
        return None

    def missing_statuses(self) -> frozenset[int]:
        """该数据源用这些 HTTP 状态码表示"此瓦片无数据"(默认空集)。

        与 is_empty_tile 的分工:后者判 200 响应的**内容**(占位图),
        本方法判**状态码**。Google 对无影像位置返回 404 + 标准错误页
        (实测,设计 §3.12);天地图与 Esri 都用 200 + 内容,故默认为空。

        下载器据此判定:不重试、不计失败、不写缓存(该处确实没有数据)。
        """
        return frozenset()

    @abstractmethod
    def tile_url(self, col: int, row: int, z: int) -> str:
        """返回单张瓦片的下载 URL。"""
        raise NotImplementedError

    def min_zoom(self) -> int:
        return 1

    def max_zoom(self) -> int:
        return 18

    def is_empty_tile(self, data: bytes) -> bool:
        """响应体是否为"该位置没有数据"的占位瓦片(默认认为都有数据)。

        有些服务(如 Esri Terrain3D)超出可用级别时返回 HTTP 200 + 极小的空瓦片
        而非 404。下载器据此判定不写入缓存,避免空瓦片被当成有效数据续传复用。
        """
        return False
