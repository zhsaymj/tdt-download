"""打包入口:启动内置后端服务并自动打开浏览器。

PyInstaller 冻结后由此文件作为程序入口(见 天地图下载工具.spec)。
以编程方式启动 uvicorn(不依赖命令行),延迟片刻后自动打开默认浏览器。
保留控制台窗口以显示运行日志,关闭窗口即退出服务。
"""
from __future__ import annotations

import threading
import time
import webbrowser

import uvicorn

from backend.config import settings


def _open_browser(url: str, delay: float = 1.5) -> None:
    """延迟打开浏览器,等 uvicorn 起来后再访问。"""
    def _run():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def main() -> None:
    host = settings.server.host or "127.0.0.1"
    port = settings.server.port or 8000
    # 浏览器访问用地址:0.0.0.0 时改用 127.0.0.1
    view_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{view_host}:{port}"

    print("=" * 52)
    print("  天地图下载处理工具")
    print(f"  服务地址: {url}")
    print("  首次使用请在界面「密钥管理」添加天地图密钥,")
    print("  或编辑本目录下 config.yaml 填写 tianditu.token。")
    print("  关闭此窗口即停止服务。")
    print("=" * 52)

    _open_browser(url)
    # 直接传 app 对象(冻结环境无法用 "backend.main:app" 字符串导入路径)
    from backend.main import app
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
