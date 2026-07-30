# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置(onedir + 控制台)。

打包:pyinstaller 天地图下载工具.spec  (或运行 build.bat)
产物:dist/天地图下载工具/天地图下载工具.exe

关键点:
  - datas 打包只读资源(前端 dist、旧版 frontend、离线底图、GDAL/proj 数据)。
  - config.yaml / output / data 等可写内容不打包,运行时落在 exe 旁边。
  - rasterio 的 C 扩展与数据目录用 collect_* 收全,避免 GDAL/proj 找不到数据文件。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

PROJECT = Path(SPECPATH)

# ---- rasterio:收集全部子模块、动态库与数据文件(gdal_data/proj_data)----
rio_datas, rio_binaries, rio_hidden = collect_all("rasterio")

# ---- lerc:Esri LERC 解码库(地形 DEM 瓦片解码依赖)。
#      该包用 ctypes 加载同目录下的 Lerc.dll(见 lerc/_lerc.py),必须把包目录
#      连同 dll 一起收集到 _internal/lerc/,否则 import lerc 在函数内被 try/except
#      吞掉 → DEM 瓦片全部当无数据 → 拼出的高程 GeoTIFF 无法打开。----
lerc_datas, lerc_binaries, lerc_hidden = collect_all("lerc")

# ---- 只读资源 ----
datas = list(rio_datas) + list(lerc_datas)
datas += [
    (str(PROJECT / "frontendvue" / "dist"), "frontendvue/dist"),
    (str(PROJECT / "frontend"), "frontend"),
    (str(PROJECT / "exmple-data" / "NaturalEarthII"), "exmple-data/NaturalEarthII"),
]

# ---- 隐藏导入 ----
hiddenimports = list(rio_hidden) + list(lerc_hidden)
# rasterio 常被漏收的内部模块
hiddenimports += collect_submodules("rasterio")
hiddenimports += [
    "rasterio._shim", "rasterio.sample", "rasterio.vrt", "rasterio._features",
    "rasterio.control", "rasterio.crs", "rasterio.rpc",
    # uvicorn 运行时按需导入的实现(字符串导入,静态分析抓不到)
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on",
    "websockets", "websockets.legacy",
    # 地形/DEM 相关
    "lerc", "pymartini", "quantized_mesh_encoder",
]

binaries = list(rio_binaries) + list(lerc_binaries)


a = Analysis(
    ["run_app.py"],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="天地图下载工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 保留控制台窗口显示日志
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="天地图下载工具",
)
