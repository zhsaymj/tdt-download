"""瓦片缓存目录由 `{key}/` 改名成 `{key}_{grid}/` 的一次性迁移。

**为什么必须带网格**(设计 D2):`_c`(geodetic)用 geodetic 行号、`_w`(mercator)用
XYZ 行号,第 z 级行数分别是 `2^(z-1)` 与 `2^z` —— **同一个 `(col, row)` 指的不是
同一地点**。两套混进同一目录会:

* 互相覆盖(后写的赢)
* 断点续传时把另一套网格的瓦片当成命中,拼出**静默错乱**的成果

现有缓存实测(2026-09-29):`tianditu_img` 18 级共 181,777 张、`tianditu_cia`
159,346 张 —— 不迁移等于白下,故做一次目录改名。

迁移映射见 `_GRID_MAP`。**注记类那几个 key 现有缓存全是 `_w`**,不能按 `_c` 处理。
"""
from __future__ import annotations

from pathlib import Path

from .logs import logger

#: key → 网格后缀(迁移目标)。
#:
#: **只列影像/矢量/地形**:它们的下载网格从来就是 geodetic(`grid_of(provider)`),
#: 无歧义。
#:
#: ⚠️ **注记(cia/cva/cta)刻意不迁**,尽管不迁会白白重下(实测 cia 有 15.9 万张)。
#: 原因:注记 provider 的网格跟随**任务**(runner 传 `grid_of(task["provider"])`),
#: 于是天地图任务写 `cia_c`、Google/Esri 任务写 `cia_w`,**两种瓦片混在同一个
#: `tianditu_cia/` 目录里**。而文件名只有 `{col}_{row}`,**无法分辨每一张属于哪套
#: 网格** —— 整体改名必然给其中一种贴错标签,后果是静默给出错误的路网注记
#: (能看到,但很难察觉是错的)。宁可不迁:新键 `tianditu_cia_c` / `_w` 各自从空
#: 开始,按需重下那几百张注记 PNG 即可(注记本身很小)。
#:
#: 实测佐证:`tianditu_img/9` 与 `tianditu_cia/9` 的 (col,row) **交集为 0**,
#: 说明两者不同网格;但两侧行号又都落在 geodetic 的 0..255 内,单看范围分不出来。
_GRID_MAP: dict[str, str] = {
    "tianditu_img": "c",
    "tianditu_vec": "c",
    "tianditu_ter": "c",
}


def migrate_cache_grids(tiles_root: Path) -> int:
    """把 tiles_root 下已知的 key 目录从 `{key}/` 改名为 `{key}_{grid}/`。

    返回实际迁移的 key 数(0 = 无需迁移)。

    安全性:
    * 源目录不存在 → 跳过(首次启动、或该图层从没用过)
    * 目标已存在   → **跳过、不覆盖**(上次迁移中断过,或新键已经下过数据;
      宁可不迁,也不能把已迁的那份盖掉)
    * 改名是 `os.rename`,同盘上原子完成、不产生中间态

    幂等:迁完再调一次返回 0。
    """
    moved = 0
    for key, grid in _GRID_MAP.items():
        src = tiles_root / key
        if not src.is_dir():
            continue
        dst = tiles_root / f"{key}_{grid}"
        if dst.exists():
            logger.debug("缓存迁移跳过 %s:目标 %s 已存在", src.name, dst.name)
            continue
        src.rename(dst)
        moved += 1
        logger.info("缓存迁移:%s → %s", src.name, dst.name)
    return moved
