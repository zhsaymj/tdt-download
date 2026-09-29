"""级别范围必须只有一处判定。

**这是实现期发现的真 bug 的回归护栏**:

  `api/tasks.py` 用「数据源的 max_zoom」判定级别上限(Google 21 / Esri 19),
  而 `models.py::level_list` 对所有非 DEM 源**硬编码 18**。两者矛盾,且
  `create_task` 走的是后者 —— 于是:
    - google_img 请求 [20,21] → 过滤成 [] → 任务根本建不出来
    - esri_imagery 请求 [18,19] → 静默变成 [18] → z19 永远下不到

  用户把 Esri 从 z18 提到 z19 的决定、以及"Google 开到 z21"的能力,
  都因为这些测试只覆盖 ≤18 的级别而**一直没生效**也没有报错。

现在级别范围统一由 core.formats 的 z_cap_of / z_floor_of 给出,两处共用。
"""
import unittest

from backend.core.formats import z_cap_of, z_floor_of
from backend.models import TaskCreate


def _levels(provider, levels):
    d = TaskCreate(name="t", provider=provider,
                   bbox=[116.38, 39.99, 116.39, 40.0], levels=list(levels))
    return d.level_list()


class TestZCapOf(unittest.TestCase):
    def test_google_cap_is_21(self):
        """Google 实测陆地处处可到 z21 —— 级别上限必须放行到 21。"""
        self.assertEqual(z_cap_of("google_img"), 21)
        for k in ("google_hybrid", "google_road", "google_terrain"):
            self.assertEqual(z_cap_of(k), 21, k)

    def test_esri_imagery_cap_is_19(self):
        """用户明确把 Esri 从 z18 提到 z19。"""
        self.assertEqual(z_cap_of("esri_imagery"), 19)

    def test_tianditu_cap_is_18(self):
        self.assertEqual(z_cap_of("tianditu_img"), 18)
        self.assertEqual(z_cap_of("tianditu_vec"), 18)
        self.assertEqual(z_cap_of("tianditu_ter"), 18)

    def test_dem_cap_from_registry(self):
        from backend.providers.terrain import DEM_LAYERS
        self.assertEqual(z_cap_of("esri_terrain"), DEM_LAYERS["esri_terrain"][2])

    def test_z_floor(self):
        self.assertEqual(z_floor_of("esri_terrain"), 0)   # DEM 允许 0 级
        self.assertEqual(z_floor_of("google_img"), 1)     # 影像从 1 起
        self.assertEqual(z_floor_of("tianditu_img"), 1)


class TestLevelListHonoursProviderCap(unittest.TestCase):
    """`level_list` 必须按数据源的上限过滤,不能硬编码 18。"""

    def test_google_keeps_19_to_21(self):
        """★ 核心用例 ★ 之前这里会被砍成 [] 或 [17,18]。"""
        self.assertEqual(_levels("google_img", [17, 18, 19]), [17, 18, 19])
        self.assertEqual(_levels("google_img", [20, 21]), [20, 21])
        self.assertEqual(_levels("google_img", [21]), [21])

    def test_esri_imagery_keeps_19(self):
        """★ 核心用例 ★ 之前 [18,19] 会静默变成 [18]。"""
        self.assertEqual(_levels("esri_imagery", [18, 19]), [18, 19])
        self.assertEqual(_levels("esri_imagery", [19]), [19])

    def test_google_drops_beyond_21(self):
        """超过各自上限的仍要过滤(只是上限不再是 18)。"""
        self.assertEqual(_levels("google_img", [21, 22, 23]), [21])
        self.assertEqual(_levels("esri_imagery", [19, 20]), [19])

    def test_tianditu_still_capped_at_18(self):
        """回归护栏:天地图的 18 上限不变。"""
        self.assertEqual(_levels("tianditu_img", [17, 18, 19]), [17, 18])

    def test_dem_keeps_zero_and_caps(self):
        self.assertEqual(_levels("esri_terrain", [0, 1, 16]), [0, 1, 16])
        self.assertEqual(_levels("esri_terrain", [16, 17]), [16])

    def test_below_floor_dropped(self):
        """0 级对影像源无效。"""
        self.assertEqual(_levels("google_img", [0, 1]), [1])


class TestLegacyZMinZMaxNotCappedAt18(unittest.TestCase):
    """`TaskCreate.z_min/z_max` 曾写死 `le=18`。

    这是"硬编码 18"的**第三处**:用旧式 z_min/z_max 表达高级别的客户端
    会被 pydantic 直接 422 拒绝("Input should be less than or equal to 18"),
    连"静默截断"都算不上。级别的实际取舍由 level_list 按数据源处理,
    pydantic 层不该再有固定上限。
    """

    def _make(self, provider, z_min, z_max):
        return TaskCreate(name="t", provider=provider,
                          bbox=[116.38, 39.99, 116.39, 40.0],
                          levels=[], z_min=z_min, z_max=z_max)

    def test_google_accepts_z_max_21(self):
        d = self._make("google_img", 1, 21)
        self.assertEqual(d.level_list(), list(range(1, 22)))

    def test_google_accepts_z_min_19(self):
        d = self._make("google_img", 19, 21)
        self.assertEqual(d.level_list(), [19, 20, 21])

    def test_esri_accepts_z_max_19(self):
        d = self._make("esri_imagery", 18, 19)
        self.assertEqual(d.level_list(), [18, 19])

    def test_tianditu_still_filtered_by_level_list(self):
        """天地图的 18 上限改由 level_list 兜住(不再是 pydantic 拒绝)。"""
        d = self._make("tianditu_img", 17, 21)
        self.assertEqual(d.level_list(), [17, 18])

    def test_dem_still_accepts_zero(self):
        d = self._make("esri_terrain", 0, 16)
        self.assertEqual(d.level_list(), list(range(0, 17)))

    def test_negative_rejected(self):
        """ge=0 仍应保留:负级别无意义。"""
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._make("google_img", -1, 5)


class TestSingleSourceOfTruth(unittest.TestCase):
    """回归护栏:级别范围判定不得再出现第二处。"""

    def test_api_tasks_delegates(self):
        """api/tasks.py 的 _z_cap_for 应委托给 core.formats。"""
        from backend.api.tasks import _z_cap_for
        for k in ("google_img", "esri_imagery", "tianditu_img", "esri_terrain"):
            self.assertEqual(_z_cap_for(k), z_cap_of(k), k)

    def test_models_delegates_to_formats(self):
        """level_list 必须调用 z_cap_of/z_floor_of。"""
        import inspect
        from backend.models import TaskCreate
        src = inspect.getsource(TaskCreate.level_list)
        self.assertIn("z_cap_of", src)
        self.assertIn("z_floor_of", src)

    def test_models_has_no_hardcoded_18_in_logic(self):
        """level_list 的**逻辑**里不该再有字面量 18。

        先剥掉 docstring 与行注释再检查 —— 文档里写"天地图 1-18"是正常的,
        要防的是把 18 写进判定逻辑(原 bug 是 `z_floor, z_cap = 1, 18`)。
        """
        import inspect
        import re
        from backend.models import TaskCreate
        src = inspect.getsource(TaskCreate.level_list)
        src = re.sub(r'"""[\s\S]*?"""', "", src)     # 去 docstring
        src = re.sub(r"#[^\n]*", "", src)             # 去行注释
        self.assertNotIn("18", src,
                         "level_list 的逻辑里仍有硬编码 18 —— 应由 z_cap_of 给")


if __name__ == "__main__":
    unittest.main()
