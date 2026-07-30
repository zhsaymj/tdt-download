@echo off
chcp 65001 >nul
cd /d %~dp0
setlocal

rem ============================================================
rem  重新生成三维建筑网格数据,并打包供手动上传到对象存储。
rem
rem  这是"准备数据"的运维操作,不在界面里暴露:全国跑一遍约半小时。
rem  普通使用机器只需在 config.yaml 配好 buildings.remote_url,
rem  按需从线上取数即可,不需要 pbf、也不需要执行本脚本。
rem
rem  pbf 文件需自行准备(如 Geofabrik 的 china-latest.osm.pbf),
rem  路径在 config.yaml 的 buildings.pbf_path 配置,默认
rem  data\pbf\china.osm.pbf。本脚本不联网下载 pbf。
rem
rem  可选参数(直接跟在命令后):
rem    --pbf <路径>    临时指定 pbf 文件(默认用配置里的路径)
rem    --pack-only     跳过导入,只把现有网格缓存重新打包
rem    --import-only   只导入,不打包
rem    --bbox w,s,e,n  只导入指定范围(默认全国)
rem
rem  例:  update-building-data.bat
rem       update-building-data.bat --pack-only
rem       update-building-data.bat --pbf D:\data\china-latest.osm.pbf
rem ============================================================

echo.
echo ============================================================
echo   三维建筑数据更新
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv,请先按 README 创建并安装依赖。
    echo.
    pause
    exit /b 1
)

rem 全国导入耗时较长(约半小时),先跟用户确认,避免误双击
if "%~1"=="" (
    echo 将执行:导入全国建筑轮廓 + 打包,预计约半小时。
    echo 若只想重新打包已有数据,请改用:update-building-data.bat --pack-only
    echo.
    choice /c YN /n /m "确定继续吗? [Y/N] "
    if errorlevel 2 (
        echo 已取消。
        echo.
        pause
        exit /b 0
    )
    echo.
)

.venv\Scripts\python.exe -m backend.tools.update_building_data %*
set RC=%ERRORLEVEL%

echo.
if %RC%==0 (
    echo [完成] 数据已生成。产物目录见上方输出,请手动上传到对象存储。
    rem 顺手打开产物目录,省去手动翻找
    if exist "data\buildings\dist" start "" "data\buildings\dist"
) else (
    echo [失败] 退出码 %RC%,请查看上方错误信息或 data\logs\app.log
)

echo.
pause
exit /b %RC%
