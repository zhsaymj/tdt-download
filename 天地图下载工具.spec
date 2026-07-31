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

# ---- pyogrio:矢量成果导出(Shapefile/GPKG/等高线)。
#      它**自带一整套 GDAL**(约 23 MB,与 rasterio 那份是两套独立副本——
#      rasterio 的 dll 是改名混淆的 gdal-<hash>.dll,无法共享),故必须单独
#      collect_all 把 pyogrio.libs 下的 dll 与 gdal_data 收全。
#      漏收的后果:打包后 import pyogrio 失败 → GPKG/Shapefile/等高线导出全部
#      不可用,而影像/DEM 管线看着正常,问题只在用户选矢量格式时才暴露。----
pyogrio_datas, pyogrio_binaries, pyogrio_hidden = collect_all("pyogrio")

# ---- 只读资源 ----
datas = list(rio_datas) + list(lerc_datas) + list(pyogrio_datas)
datas += [
    (str(PROJECT / "frontendvue" / "dist"), "frontendvue/dist"),
    (str(PROJECT / "frontend"), "frontend"),
    (str(PROJECT / "exmple-data" / "NaturalEarthII"), "exmple-data/NaturalEarthII"),
]

# ---- 隐藏导入 ----
hiddenimports = list(rio_hidden) + list(lerc_hidden) + list(pyogrio_hidden)
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
    # 矢量导出:pyogrio.raw 是 cython 扩展,静态分析常抓不到
    "pyogrio", "pyogrio.raw", "pyogrio._io", "pyogrio._ogr", "pyogrio._err",
    "pyogrio._geometry", "pyogrio._vsi", "pyogrio.core", "pyogrio.errors",
    # 等高线用到 shapely 的 wkb 编解码(shapely 2.x 是 C 扩展 + lgeos)
    "shapely", "shapely.wkb", "shapely.geometry",
]

binaries = list(rio_binaries) + list(lerc_binaries) + list(pyogrio_binaries)


a = Analysis(
    ["run_app.py"],
    pathex=[str(PROJECT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter 不能排除:本地数据入库要用它弹系统文件对话框选文件
    # (本机自用,前后端同机,故由后端弹框拿真实路径,不做上传。见
    #  core/file_dialog.py)。排除掉的话该功能在打包版直接失效,
    #  而开发环境完全正常——这类问题只能靠打包后实测发现。
    excludes=["matplotlib", "pytest"],
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
