"""调系统文件对话框选取本地文件,返回真实路径。

为什么不做上传:本工具是**本机自用**(服务绑 127.0.0.1,前后端同机)。上传等于把
文件复制一遍——一份 2 GB 的影像会在 data/ 下再占 2 GB,而后端明明能直接
rasterio.open() 原始路径。上传还要等完整拷贝走完 HTTP 才能开始处理。

浏览器拿不到本地文件的真实路径(安全设计,只给文件名和内容,路径被抹成
C:\\fakepath\\x.tif),所以由后端弹对话框——反正后端就在用户这台机器上。

实测确认(2026-07-31):FastAPI 的 asyncio.to_thread 里创建 Tk 不崩、对话框可见
可操作、返回真实完整路径、中文路径正常、取消返回空列表不挂住。

**安全性质**:此端点能弹出一个可浏览整个磁盘的对话框,是"本机专用"假设下的产物。
若哪天要改成局域网共享,必须先关掉它(见 api/local.py 的 _require_local)。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from .logs import logger

#: 可选取的栅格扩展名。vrt 也列上:GDAL 虚拟栅格常用于拼多个文件,rasterio 能读。
RASTER_PATTERNS = "*.tif *.tiff *.img *.vrt *.dem *.asc *.hgt"

#: 矢量扩展名(后续本地矢量入库用;shp 要连附属文件一起在同目录)
VECTOR_PATTERNS = "*.shp *.geojson *.json *.gpkg *.kml *.fgb"

#: 点云扩展名(LAS/LAZ)。与上面两个字符串 patterns 不同,这里直接给
#: tkinter filetypes 的 (说明, 模式) 元组列表——点云只有一种类型,
#: 不需要「支持的文件」这个兜底标签。
POINTCLOUD_PATTERNS = [("LAS/LAZ 点云", "*.las *.laz")]

#: 对话框最长等待。用户可能开着对话框去别处翻文件,给足时间;
#: 但不能无限等——挂住的话这个工作线程就永久占用了。
DIALOG_TIMEOUT = 300.0


def _new_root():
    """创建隐藏 root 窗口:只要对话框不要主窗口,置顶 + 抢焦点。

    不抢焦点的话对话框可能出现在浏览器后面,用户得去任务栏找;
    抢焦点失败不影响对话框本身。
    """
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        root.lift()
        root.focus_force()
    except Exception:
        pass
    return root


def _close_root(root) -> None:
    # root.update() 让对话框销毁事件走完,避免残留隐形窗口
    try:
        root.update()
        root.destroy()
    except Exception:
        pass


def _filetypes(patterns) -> list:
    """把 patterns 规整成 tkinter filetypes 列表(末尾带「所有文件」兜底)。"""
    if isinstance(patterns, str):
        return [("支持的文件", patterns), ("所有文件", "*.*")]
    return [*patterns, ("所有文件", "*.*")]


def _pick_sync(title: str, patterns, multiple: bool,
               initial_dir: str | None) -> list[str]:
    """在工作线程里弹文件对话框(tkinter 不是线程安全的,故每次新建再销毁 root)。"""
    from tkinter import filedialog

    root = _new_root()
    kw = {
        "parent": root,
        "title": title,
        "filetypes": _filetypes(patterns),
    }
    if initial_dir and Path(initial_dir).is_dir():
        kw["initialdir"] = initial_dir

    try:
        if multiple:
            picked = filedialog.askopenfilenames(**kw)
            return [str(p) for p in (picked or ())]
        one = filedialog.askopenfilename(**kw)
        return [str(one)] if one else []
    finally:
        _close_root(root)


def _pick_dir_sync(title: str, initial_dir: str | None) -> list[str]:
    """在工作线程里弹目录对话框(OSGB 模型目录这类"输入是一个目录"的源用)。"""
    from tkinter import filedialog

    root = _new_root()
    kw = {"parent": root, "title": title}
    if initial_dir and Path(initial_dir).is_dir():
        kw["initialdir"] = initial_dir

    try:
        picked = filedialog.askdirectory(**kw)
        return [str(picked)] if picked else []
    finally:
        _close_root(root)


async def pick(kind: str = "file", *, title: str = "选择文件",
               patterns=RASTER_PATTERNS, multiple: bool = True,
               initial_dir: str | None = None) -> list[str]:
    """弹出系统文件/目录对话框,返回选中的绝对路径列表(取消时为空列表)。

    kind="file" 走文件对话框(patterns 过滤、multiple 多选);
    kind="dir" 走目录对话框(单选,patterns/multiple 无意义)。
    对话框是阻塞 UI,放线程池执行,避免阻塞事件循环。
    """
    if kind == "dir":
        work = lambda: _pick_dir_sync(title, initial_dir)
    else:
        work = lambda: _pick_sync(title, patterns, multiple, initial_dir)
    try:
        return await asyncio.wait_for(asyncio.to_thread(work),
                                      timeout=DIALOG_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning("文件对话框超过 %.0f 秒未返回,已放弃", DIALOG_TIMEOUT)
        return []
    except Exception as e:
        # tkinter 缺失(打包时被 excludes 掉)或无图形环境时会走到这里
        logger.error("打开文件对话框失败:%s", e)
        raise RuntimeError(
            f"无法打开文件选择对话框:{e}。"
            "若为打包版,请确认打包时未排除 tkinter。") from e


def dialog_available() -> bool:
    """能否弹对话框(供界面决定是否显示「浏览…」按钮)。

    只检查 tkinter 能否导入并创建 root——打包后 tkinter 可能被 excludes 掉,
    此时应让界面回落到手工填路径,而不是点了按钮才报错。
    """
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception as e:
        logger.info("系统文件对话框不可用(将回落手工填路径):%s", e)
        return False
