# 天地图下载处理工具

在地图上绘制范围,下载天地图影像瓦片,自动拼接为带坐标的 GeoTIFF。B/S 架构、单机自用。

## 功能(首版 · 最小闭环)

- 网页地图上绘制矩形范围
- 填写天地图密钥、缩放级别区间,预估瓦片数
- 加入下载队列,并发下载(失败重试、断点续传)
- 下载完成自动拼接为 **EPSG:4326 带坐标 GeoTIFF**(LZW 压缩 + 概视图)
- WebSocket 实时进度
- 成果落到本地 `output/{任务ID}/` 目录

> 已支持:矢量面导入(shp/geojson/kml)、TMS / OSM 瓦片导出、CGCS2000（4547/4545 等)投影转换、路网注记叠加、**真实 DEM 高程下载**(Esri Terrain3D,全球免费无密钥,LERC 解码为真实海拔 GeoTIFF,可本地生成晕渲图)。影像/地形分 tab 参数、可勾选同时下载。
> 后续迭代:瓦片金字塔 / MBTiles 导出。

## 环境准备

已创建独立虚拟环境 `.venv`,依赖已装好(rasterio 自带 GDAL,无需另装系统库)。

如需在新机器重建:

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
  fastapi==0.115.6 "uvicorn[standard]==0.34.0" websockets==14.1 \
  aiohttp==3.11.11 rasterio==1.4.4 numpy==2.2.1 PyYAML==6.0.2 pydantic==2.10.4
```

## 配置密钥

复制 `config.example.yaml` 为 `config.yaml`,填写你在
[天地图控制台](https://console.tianditu.gov.cn/api/key) 申请的密钥:

```yaml
tianditu:
  token: "你的天地图密钥"
```

也可用环境变量 `TIANDITU_TOKEN` 覆盖(不落盘)。

## 启动

```bash
# Windows
start.bat

# 或手动
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000

## 使用流程

1. 点「在地图上绘制矩形」,框选范围
2. 填任务名、起止缩放级别(建议先小范围 + 10-12 级试跑)
3. 看「预计瓦片数」,确认后点「加入下载队列」
4. 右侧地图、左侧任务列表看进度
5. 完成后到 `output/{任务ID}/` 取 GeoTIFF,可在 QGIS/ArcGIS 中叠加

## 目录说明

```
backend/          后端(FastAPI)
  core/           瓦片换算、下载器、拼接、队列、任务执行
  providers/      数据源(天地图影像;DEM 预留)
  api/            REST + WebSocket
frontend/         前端(OpenLayers 单页)
data/tiles/       瓦片缓存(断点续传依赖)
output/           成果输出
config.yaml       你的配置(含密钥,已 gitignore)
```

## 注意

- 天地图密钥有日调用配额,大范围高级别瓦片量极大,请按需下载。
- 级别越高瓦片越多:每升 1 级瓦片数约 ×4。
