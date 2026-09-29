"""GDAL 的进程级配置 —— 只设一次,必须在任何 GDAL 调用之前。

**为什么不在每张瓦片里 `with rasterio.Env(...)`**:`rasterio.Env.__enter__` 用
`threading.local()` 判断"我是不是最外层",于是**每个线程都以为自己是最外层**,
各自去 `defenv()` 设置、退出时还原**进程级全局**的 GDAL 配置。多线程切瓦片时这
是一个真实的数据竞争 —— 实测踩到:OSM 切到 99.3% 时 8 个线程**同时**报
`libpng: No IDATs written into file`(8 = 线程池大小,而不是某一两张瓦片的数据问题;
同一张瓦片单独写两次都成功)。

配置只关掉 PAM:否则每张瓦片旁边会生成一个 `.png.aux.xml`,与示例数据不一致。
"""
from __future__ import annotations

import os

#: 关掉 PAM(.aux.xml 边车文件)。GDAL 在**首次使用**时从环境变量读取配置,
#: 所以模块被 import(早于任何栅格读写)就够了。
os.environ.setdefault("GDAL_PAM_ENABLED", "NO")
