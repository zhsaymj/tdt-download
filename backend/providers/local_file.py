"""本地栅格文件作为"数据源":不下载,只描述文件本身。

为什么要实现 TileProvider:导出管线里有几处只把 provider 当**元信息载体**用——
`tms.write_tilemapresource` 取 ext 决定瓦片 MIME、`metadata.write_metadata` 记
数据源信息、拼接模块读 bands。让本地文件也提供这几个属性,这些地方就无需到处
判 `provider is None`。

tile_url 恒抛异常:走到它说明有人试图"下载"本地文件,那是调用方的逻辑错误
(本地源任务不该有 download 阶段,见 models.build_stage_defs),应当尽早暴露
而不是静默返回一个假 URL。
"""
from __future__ import annotations

from pathlib import Path

from .base import TileProvider


class LocalFileProvider(TileProvider):
    """描述一个本地栅格文件。key 为 local_image / local_dem。"""

    def __init__(self, key: str, path: Path):
        self.key = key
        self.path = Path(path)
        self._bands = 3
        self._ext = "png"
        try:
            import rasterio
            with rasterio.open(self.path) as ds:
                self._bands = int(ds.count)
                # 瓦片输出格式:影像用 jpg(体积小),单波段/带 alpha 用 png
                self._ext = "png" if ds.count in (1, 2, 4) else "jpg"
        except Exception:
            pass                     # 读不出就用默认值;真正读取失败会在阶段里报错

    @property
    def ext(self) -> str:            # type: ignore[override]
        return self._ext

    @property
    def bands(self) -> int:          # type: ignore[override]
        return self._bands

    def tile_url(self, col: int, row: int, z: int) -> str:
        raise RuntimeError(
            "本地文件数据源没有瓦片 URL——本地源任务不应包含下载阶段。"
            "若走到这里,说明阶段构成有误(见 models.build_stage_defs)。")

    def min_zoom(self) -> int:
        return 0

    def max_zoom(self) -> int:
        return 18
