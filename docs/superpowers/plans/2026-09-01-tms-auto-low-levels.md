# TMS Auto Low Levels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** 本地 GeoTIFF 生成 TMS 瓦片时，连续高层用各自原始层级切片，断档后的低层用连续段最低级兜底补齐到 TMS 0 级。

**Architecture:** 在线天地图瓦片导出仍按已下载层级无损搬运；本地 tif 这种“从源图重采样切 TMS”的路径先识别从最高层级开始的连续段。连续段内的高层级用各自对应的 GeoTIFF 切片；低于连续段最低级、以及断档后传入的层级，统一由连续段最低级 GeoTIFF 降采样补齐。

**Tech Stack:** Python、rasterio、unittest。

---

### Task 1: TMS 源图层级规划

**Files:**
- Modify: backend/core/tms.py:68-331
- Modify: backend/core/runner.py:643-678
- Test: tests/test_local_raster_edges.py

- [ ] **Step 1: Write the failing test**

~~~python
class TmsSourceLevelExpansionTest(unittest.TestCase):
    def test_source_tms_level_plan_uses_contiguous_high_levels_only(self):
        from backend.core.tms import source_tms_level_plan

        self.assertEqual(
            source_tms_level_plan([18, 17, 16, 13]),
            [(16, list(range(1, 17))), (17, [17]), (18, [18])],
        )

    def test_export_tms_from_source_fills_down_to_tms_zero(self):
        from backend.core.tms import export_tms_from_source

        bounds = (0.0, 0.0, 1.0, 1.0)
        data = np.full((3, 32, 32), 150, dtype=np.uint8)

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "source.tif"
            out_dir = Path(d) / "tms"
            with rasterio.open(
                src, "w", driver="GTiff", height=32, width=32,
                count=3, dtype="uint8", crs="EPSG:4326",
                transform=from_bounds(*bounds, 32, 32),
            ) as ds:
                ds.write(data)

            _, exported, ext, stopped = export_tms_from_source(
                src, bounds, [4], out_dir, concurrency=1,
            )

            self.assertFalse(stopped)
            self.assertEqual(ext, "png")
            self.assertEqual(exported, [1, 2, 3, 4])
            self.assertEqual(
                {p.name for p in out_dir.iterdir() if p.is_dir()},
                {"0", "1", "2", "3"},
            )
~~~

- [ ] **Step 2: Run test to verify it fails**

Run: .venv\Scripts\python.exe -m unittest tests.test_local_raster_edges.TmsSourceLevelExpansionTest.test_export_tms_from_source_fills_down_to_tms_zero -v
Expected: FAIL because the planner function does not exist yet, and current runner only uses the highest source once.

- [ ] **Step 3: Write minimal implementation**

Add planning helpers in backend/core/tms.py:

~~~python
def expand_source_tms_levels(levels: list[int]) -> list[int]:
    if not levels:
        return []
    return list(range(1, max(levels) + 1))


def source_tms_level_plan(levels: list[int]) -> list[tuple[int, list[int]]]:
    clean = sorted({int(z) for z in levels if int(z) >= 1}, reverse=True)
    if not clean:
        return []
    available = set(clean)
    contiguous = []
    z = clean[0]
    while z in available:
        contiguous.append(z)
        z -= 1
    base_z = contiguous[-1]
    plan = [(base_z, list(range(1, base_z + 1)))]
    for z in sorted((lv for lv in contiguous if lv > base_z)):
        plan.append((z, [z]))
    return plan
~~~

Use source_tms_level_plan in backend/core/runner.py for local tif TMS. For [18,17,16,13], call export_tms_from_source three times: z16 source outputs 1..16, z17 source outputs 17, z18 source outputs 18. Pass sorted exported levels to write_tilemapresource so XML lists 0..17.

- [ ] **Step 4: Run targeted test to verify pass**

Run: .venv\Scripts\python.exe -m unittest tests.test_local_raster_edges.TmsSourceLevelExpansionTest.test_export_tms_from_source_fills_down_to_tms_zero -v
Expected: PASS.

- [ ] **Step 5: Run related regression tests**

Run: .venv\Scripts\python.exe -m unittest tests.test_local_raster_edges -v
Expected: all tests pass.
