@echo off
chcp 65001 >nul
cd /d %~dp0
echo 启动天地图下载处理工具...
echo 浏览器访问 http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
pause
