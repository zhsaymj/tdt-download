@echo off
chcp 65001 >nul
cd /d %~dp0
echo ============================================
echo   打包天地图下载处理工具为 Windows 可执行程序
echo ============================================

echo [1/4] 检查/安装 PyInstaller...
.venv\Scripts\python.exe -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo     未安装,正在安装 pyinstaller...
    .venv\Scripts\python.exe -m pip install pyinstaller
)

echo [2/4] 确认前端已构建(frontendvue\dist)...
if not exist "frontendvue\dist\index.html" (
    echo     未找到前端构建产物,正在构建...
    pushd frontendvue
    call npm run build
    popd
)

echo [3/4] 清理旧的 build\dist 目录...
if exist build rmdir /s /q build
if exist "dist\天地图下载工具" rmdir /s /q "dist\天地图下载工具"

echo [4/4] 运行 PyInstaller 打包(onedir)...
.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean "天地图下载工具.spec"

if errorlevel 1 (
    echo.
    echo 打包失败,请查看上方错误信息。
    pause
    exit /b 1
)

echo.
echo ============================================
echo   打包完成!
echo   产物目录: dist\天地图下载工具\
echo   双击运行: dist\天地图下载工具\天地图下载工具.exe
echo ============================================
pause
