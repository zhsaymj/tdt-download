"""瓦片缓存目录由 `{key}/` 改名为 `{key}_{grid}/` 的一次性迁移。

设计 D2:缓存键必须带网格 —— `_c` 用 geodetic 行号、`_w` 用 XYZ 行号,第 z 级行数
分别是 `2^(z-1)` 与 `2^z`,**同一个 (col,row) 指的不是同一地点**。两套混进同一
目录会互相覆盖,断点续传时还会把另一套的瓦片当成命中,拼出静默错乱的成果。

现有缓存实测:`tianditu_img` 18 级共 181,777 张、`tianditu_cia` 159,346 张
(注记那批**全是 `_w`**)—— 不迁移等于白下,故做一次目录改名。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from backend.core.cache_migrate import migrate_cache_grids


class CacheMigrateTest(unittest.TestCase):
    def test_renames_known_keys(self):
        """影像/矢量/地形的缓存迁到 _c(它们的下载网格从来是 geodetic,无歧义)。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            for key in ("tianditu_img", "tianditu_vec", "tianditu_ter"):
                (root / key / "18").mkdir(parents=True)
            n = migrate_cache_grids(root)
            self.assertEqual(n, 3)
            for key in ("tianditu_img", "tianditu_vec", "tianditu_ter"):
                self.assertTrue((root / f"{key}_c" / "18").is_dir(),
                                f"{key} 应迁成 _c")
                self.assertFalse((root / key).exists(),
                                 "旧目录应已不在(是改名不是复制)")

    def test_annotation_keys_are_not_migrated(self):
        """★ 注记刻意不迁 ★

        注记 provider 的网格跟随**任务**(天地图任务写 cia_c、Google/Esri 写 cia_w),
        两种瓦片混在同一个 `tianditu_cia/` 里,而文件名只有 {col}_{row} ——
        **分不出每张属于哪套网格**。整体改名会给其中一种贴错标签,静默给出错误的
        路网注记。宁可不迁:新键从空开始,按需重下(注记 PNG 很小)。
        """
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "tianditu_cia").mkdir()
            (root / "tianditu_cva").mkdir()
            n = migrate_cache_grids(root)
            self.assertEqual(n, 0, "注记类 key 不该被迁移")
            self.assertTrue((root / "tianditu_cia").exists(),
                            "注记缓存应原样留在原地")
            self.assertFalse((root / "tianditu_cia_c").exists())
            self.assertFalse((root / "tianditu_cia_w").exists())

    def test_skips_missing_keys(self):
        """目录不存在时跳过、不抛错(首次启动/没有缓存的情形)。"""
        with TemporaryDirectory() as d:
            self.assertEqual(migrate_cache_grids(Path(d)), 0)

    def test_skips_existing_targets(self):
        """目标已存在时跳过、不覆盖(重跑或上次迁移中断过)。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "tianditu_img" / "18").mkdir(parents=True)
            (root / "tianditu_img_c").mkdir()   # 目标已在
            n = migrate_cache_grids(root)
            self.assertEqual(n, 0, "目标已存在时不该再迁")
            self.assertTrue((root / "tianditu_img" / "18").is_dir(),
                            "源目录应原样保留(宁可不迁,也不能覆盖已迁的那份)")

    def test_leaves_unknown_keys_alone(self):
        """非天地图的 key 不迁(Google/Esri 只有一套网格,key 不带后缀)。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "google_img" / "18").mkdir(parents=True)
            n = migrate_cache_grids(root)
            self.assertEqual(n, 0)
            self.assertTrue((root / "google_img" / "18").is_dir())

    def test_rename_failure_does_not_raise(self):
        """★ 最终审查 Important 4 ★ rename 失败不能抛出去。

        本函数在 lifespan 里调用 —— 一次 rename 失败(Windows 上目录被别的进程
        占着 → WinError 5/32)会让**整个后端起不来**。设计 D2 明写"改名失败就当
        不迁移",而它动的是用户真实缓存(实测 18 万张)。
        """
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "tianditu_img" / "18").mkdir(parents=True)
            with mock.patch.object(Path, "rename",
                                   side_effect=PermissionError("WinError 32")):
                n = migrate_cache_grids(root)      # 不应抛
            self.assertEqual(n, 0, "失败的迁移不该计入成功数")
            self.assertTrue((root / "tianditu_img" / "18").is_dir(),
                            "源目录应原样保留")

    def test_is_idempotent(self):
        """迁移完再跑一次应无事发生(启动期每次都会调)。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "tianditu_img" / "18").mkdir(parents=True)
            self.assertEqual(migrate_cache_grids(root), 1)
            self.assertEqual(migrate_cache_grids(root), 0)


if __name__ == "__main__":
    unittest.main()
