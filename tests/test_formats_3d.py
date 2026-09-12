import unittest

from backend.core.formats import (DataKind, PROVIDER_KIND, STAGES, plan_stages,
                                  stages_for)


class Formats3DTest(unittest.TestCase):
    def test_new_datakinds_exist(self):
        self.assertEqual(DataKind.MESH_OSGB, "mesh_osgb")
        self.assertEqual(DataKind.POINT_CLOUD, "point_cloud")
        self.assertEqual(DataKind.TILES_3D_MODEL, "tiles_3d_model")
        self.assertEqual(DataKind.TILES_3D_POINT, "tiles_3d_point")

    def test_local_providers_registered(self):
        self.assertEqual(PROVIDER_KIND["local_osgb"], DataKind.MESH_OSGB)
        self.assertEqual(PROVIDER_KIND["local_pointcloud"], DataKind.POINT_CLOUD)

    def test_plan_stages_osgb(self):
        stages = [s.key for s in plan_stages("local_osgb", {"tile_3d"})]
        self.assertEqual(stages, ["convert_3d"])

    def test_plan_stages_pointcloud(self):
        stages = [s.key for s in plan_stages("local_pointcloud", {"dem", "dsm", "tile_3d"})]
        # 按 order 排序:pc_dsm(10) < pc_dem(20) < pc_tile_3d(30)
        self.assertEqual(stages, ["pc_dsm", "pc_dem", "pc_tile_3d"])

    def test_no_key_conflict_with_existing(self):
        # dem/tile_3d 已被栅格/建筑管线占用(ExportStage 单实例、pipeline 字段互斥),三维必须另起 key
        self.assertEqual(STAGES["dem"].pipeline, "raster")
        self.assertEqual(STAGES["tile_3d"].pipeline, "building")

    def test_pointcloud_stages_not_in_raster_dem(self):
        # 点云专属的 pc_dsm/pc_dem 不得串进栅格 DEM 的阶段列表(kind 互斥)
        keys = [s.key for s in stages_for(DataKind.RASTER_DEM)]
        self.assertNotIn("pc_dem", keys)
        self.assertNotIn("pc_dsm", keys)


if __name__ == "__main__":
    unittest.main()
