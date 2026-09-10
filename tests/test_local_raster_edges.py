import tempfile
import sys
import types
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_bounds


class _Ctx:
    def should_stop(self):
        return False


class LocalRasterEdgeMaskTest(unittest.TestCase):
    def _stub_optional_terrain_deps(self):
        if "pymartini" not in sys.modules:
            pymartini = types.ModuleType("pymartini")
            pymartini.Martini = object
            pymartini.rescale_positions = lambda *args, **kwargs: None
            sys.modules["pymartini"] = pymartini
        if "quantized_mesh_encoder" not in sys.modules:
            qme = types.ModuleType("quantized_mesh_encoder")
            qme.encode = lambda *args, **kwargs: b""
            sys.modules["quantized_mesh_encoder"] = qme

    def test_resample_to_level_marks_pixels_outside_source_as_masked(self):
        self._stub_optional_terrain_deps()
        from backend.core.runner import _resample_to_level
        from backend.core.tiling import TileRange

        tr = TileRange(z=4, col_min=8, col_max=8, row_min=4, row_max=4)
        west, south, east, north = tr.mosaic_bounds()
        dx, dy = east - west, north - south
        src_bounds = (
            west + dx * 0.25,
            south + dy * 0.25,
            east - dx * 0.25,
            north - dy * 0.25,
        )
        data = np.full((3, 64, 64), 120, dtype=np.uint8)

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "source.tif"
            dst = Path(d) / "out.tif"
            with rasterio.open(
                src,
                "w",
                driver="GTiff",
                height=64,
                width=64,
                count=3,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_bounds(*src_bounds, 64, 64),
            ) as ds:
                ds.write(data)

            _resample_to_level(_Ctx(), src, dst, tr)

            with rasterio.open(dst) as ds:
                mask = ds.read_masks(1)
                pixels = ds.read()
                alpha = ds.read(4)
                colorinterp = ds.colorinterp

        self.assertEqual(pixels.shape[0], 4)
        self.assertEqual(colorinterp[-1], ColorInterp.alpha)
        self.assertEqual(int(mask[0, 0]), 0)
        self.assertEqual(int(mask[-1, -1]), 0)
        self.assertEqual(int(mask[mask.shape[0] // 2, mask.shape[1] // 2]), 255)
        np.testing.assert_array_equal(alpha, mask)
        self.assertTrue(np.all(pixels[:3, mask == 0] == 0))

    def test_resample_to_level_accepts_rgba_source_with_existing_alpha(self):
        self._stub_optional_terrain_deps()
        from backend.core.runner import _resample_to_level
        from backend.core.tiling import TileRange

        tr = TileRange(z=4, col_min=8, col_max=8, row_min=4, row_max=4)
        west, south, east, north = tr.mosaic_bounds()
        dx, dy = east - west, north - south
        src_bounds = (
            west + dx * 0.25,
            south + dy * 0.25,
            east - dx * 0.25,
            north - dy * 0.25,
        )
        rgb = np.full((3, 64, 64), 120, dtype=np.uint8)
        alpha = np.zeros((1, 64, 64), dtype=np.uint8)
        alpha[:, 16:48, 16:48] = 255
        data = np.concatenate([rgb, alpha], axis=0)

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "rgba_source.tif"
            dst = Path(d) / "out.tif"
            with rasterio.open(
                src,
                "w",
                driver="GTiff",
                height=64,
                width=64,
                count=4,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_bounds(*src_bounds, 64, 64),
            ) as ds:
                ds.write(data)
                ds.colorinterp = [
                    ColorInterp.red,
                    ColorInterp.green,
                    ColorInterp.blue,
                    ColorInterp.alpha,
                ]

            _resample_to_level(_Ctx(), src, dst, tr)

            with rasterio.open(dst) as ds:
                pixels = ds.read()
                alpha_out = ds.read(4)
                colorinterp = ds.colorinterp

        self.assertEqual(pixels.shape[0], 4)
        self.assertEqual(colorinterp[-1], ColorInterp.alpha)
        self.assertEqual(int(alpha_out[0, 0]), 0)
        self.assertEqual(int(alpha_out[-1, -1]), 0)
        self.assertEqual(int(alpha_out[alpha_out.shape[0] // 2,
                                      alpha_out.shape[1] // 2]), 255)
        self.assertTrue(np.all(pixels[:3, alpha_out == 0] == 0))


class OsmPngTransparentPixelTest(unittest.TestCase):
    def test_write_png_bleeds_rgb_into_transparent_pixels(self):
        from backend.core.osm import _write_png

        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / "tile.png"
            rgba = np.zeros((4, 256, 256), dtype=np.uint8)
            rgba[0, 96:160, 96:160] = 80
            rgba[1, 96:160, 96:160] = 120
            rgba[2, 96:160, 96:160] = 160
            rgba[3, 96:160, 96:160] = 255

            _write_png(png, rgba, from_bounds(0, 0, 1, 1, 256, 256))

            with rasterio.open(png) as ds:
                out = np.moveaxis(ds.read(), 0, -1)

        transparent_border = out[95, 96:160]
        self.assertTrue(np.all(transparent_border[:, 3] == 0))
        self.assertTrue(np.all(transparent_border[:, :3] == [80, 120, 160]))

    def test_export_osm_from_rgba_source_writes_rgba_png(self):
        from backend.core.osm import export_osm

        bounds = (0.0, 0.0, 1.0, 1.0)
        data = np.zeros((4, 64, 64), dtype=np.uint8)
        data[0] = 80
        data[1] = 120
        data[2] = 160
        data[3] = 255
        data[3, :, :32] = 0

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "rgba_source.tif"
            out_dir = Path(d) / "osm"
            with rasterio.open(
                src,
                "w",
                driver="GTiff",
                height=64,
                width=64,
                count=4,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_bounds(*bounds, 64, 64),
            ) as ds:
                ds.write(data)
                ds.colorinterp = [
                    ColorInterp.red,
                    ColorInterp.green,
                    ColorInterp.blue,
                    ColorInterp.alpha,
                ]

            export_osm(src, [2], bounds, out_dir, concurrency=1)

            pngs = sorted(out_dir.rglob("*.png"))
            self.assertTrue(pngs)
            with rasterio.open(pngs[0]) as ds:
                self.assertEqual(ds.count, 4)
                self.assertEqual(ds.colorinterp[-1], ColorInterp.alpha)
                alpha = ds.read(4)
            self.assertEqual(int(alpha.min()), 0)
            self.assertEqual(int(alpha.max()), 255)


class GeoTiffClipAlphaTest(unittest.TestCase):
    def test_clip_to_geometry_writes_explicit_alpha_band(self):
        from backend.core.postprocess import clip_to_geometry

        bounds = (0.0, 0.0, 1.0, 1.0)
        data = np.full((3, 64, 64), 120, dtype=np.uint8)
        geometry = {
            "type": "Polygon",
            "coordinates": [[[0.25, 0.25], [0.75, 0.25],
                             [0.25, 0.75], [0.25, 0.25]]],
        }

        with tempfile.TemporaryDirectory() as d:
            tif = Path(d) / "source.tif"
            with rasterio.open(
                tif,
                "w",
                driver="GTiff",
                height=64,
                width=64,
                count=3,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_bounds(*bounds, 64, 64),
            ) as ds:
                ds.write(data)

            self.assertTrue(clip_to_geometry(tif, geometry))
            with rasterio.open(tif) as ds:
                pixels = ds.read()
                alpha = ds.read(4)
                colorinterp = ds.colorinterp

        self.assertEqual(pixels.shape[0], 4)
        self.assertEqual(colorinterp[-1], ColorInterp.alpha)
        self.assertEqual(int(alpha.min()), 0)
        self.assertEqual(int(alpha.max()), 255)
        self.assertTrue(np.all(pixels[:3, alpha == 0] == 0))

    def test_clip_to_geometry_combines_existing_alpha(self):
        from backend.core.postprocess import clip_to_geometry

        bounds = (0.0, 0.0, 1.0, 1.0)
        data = np.full((4, 64, 64), 120, dtype=np.uint8)
        data[3, :, :16] = 0
        geometry = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.25], [0.75, 0.25],
                             [0.75, 0.75], [0.0, 0.75],
                             [0.0, 0.25]]],
        }

        with tempfile.TemporaryDirectory() as d:
            tif = Path(d) / "rgba.tif"
            with rasterio.open(
                tif,
                "w",
                driver="GTiff",
                height=64,
                width=64,
                count=4,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_bounds(*bounds, 64, 64),
            ) as ds:
                ds.write(data)
                ds.colorinterp = [
                    ColorInterp.red, ColorInterp.green,
                    ColorInterp.blue, ColorInterp.alpha,
                ]

            self.assertTrue(clip_to_geometry(tif, geometry))
            with rasterio.open(tif) as ds:
                alpha = ds.read(4)
                colorinterp = ds.colorinterp

        self.assertEqual(colorinterp[-1], ColorInterp.alpha)
        self.assertEqual(int(alpha.min()), 0)
        self.assertEqual(int(alpha.max()), 120)

    def test_reproject_preserves_explicit_alpha_without_mask_sidecar(self):
        from backend.core.postprocess import reproject_geotiff

        data = np.full((4, 32, 32), 120, dtype=np.uint8)
        data[3, :, :8] = 0

        with tempfile.TemporaryDirectory() as d:
            tif = Path(d) / "rgba.tif"
            with rasterio.open(
                tif,
                "w",
                driver="GTiff",
                height=32,
                width=32,
                count=4,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_bounds(0.0, 0.0, 1.0, 1.0, 32, 32),
            ) as ds:
                ds.write(data)
                ds.colorinterp = [
                    ColorInterp.red, ColorInterp.green,
                    ColorInterp.blue, ColorInterp.alpha,
                ]

            self.assertTrue(reproject_geotiff(tif, "EPSG:3857"))
            with rasterio.open(tif) as ds:
                alpha = ds.read(4)
                colorinterp = ds.colorinterp
                sidecar_exists = Path(str(tif) + ".msk").exists()

        self.assertEqual(colorinterp[-1], ColorInterp.alpha)
        self.assertEqual(int(alpha.min()), 0)
        self.assertEqual(int(alpha.max()), 120)
        self.assertFalse(sidecar_exists)


class TmsSourceLevelExpansionTest(unittest.TestCase):
    def _stub_optional_terrain_deps(self):
        if "pymartini" not in sys.modules:
            pymartini = types.ModuleType("pymartini")
            pymartini.Martini = object
            pymartini.rescale_positions = lambda *args, **kwargs: None
            sys.modules["pymartini"] = pymartini
        if "quantized_mesh_encoder" not in sys.modules:
            qme = types.ModuleType("quantized_mesh_encoder")
            qme.encode = lambda *args, **kwargs: b""
            sys.modules["quantized_mesh_encoder"] = qme

    def test_source_tms_level_plan_uses_contiguous_high_levels_only(self):
        from backend.core.tms import source_tms_level_plan

        self.assertEqual(
            source_tms_level_plan([18, 17, 16, 13]),
            [(16, list(range(1, 17))), (17, [17]), (18, [18])],
        )

    def test_source_tms_level_plan_can_preserve_each_input_source(self):
        from backend.core.tms import source_tms_level_plan_preserve_inputs

        self.assertEqual(
            source_tms_level_plan_preserve_inputs([18, 17, 16, 13]),
            [
                (18, [18]),
                (17, [17]),
                (16, [14, 15, 16]),
                (13, list(range(1, 14))),
            ],
        )

    def test_export_tms_from_source_fills_down_to_tms_zero(self):
        from backend.core.tms import export_tms_from_source

        bounds = (0.0, 0.0, 1.0, 1.0)
        data = np.full((3, 32, 32), 150, dtype=np.uint8)

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "source.tif"
            out_dir = Path(d) / "tms"
            with rasterio.open(
                src,
                "w",
                driver="GTiff",
                height=32,
                width=32,
                count=3,
                dtype="uint8",
                crs="EPSG:4326",
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

    def test_tms_from_source_file_uses_only_contiguous_high_sources(self):
        self._stub_optional_terrain_deps()
        import backend.core.runner as runner

        calls = []
        xml_levels = []

        class _Tracker:
            def update(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            for z in (18, 17, 16, 13):
                (out_dir / f"demo_z{z}.tif").write_bytes(b"stub")

            ctx = type("Ctx", (), {})()
            ctx.task = {"name": "demo", "containers": {"tms": "tiles_dir"}}
            ctx.bbox = (0.0, 0.0, 1.0, 1.0)
            ctx.levels = [18, 17, 16, 13]
            ctx.out_dir = out_dir
            ctx.provider = object()
            ctx.tracker = _Tracker()
            ctx.should_stop = lambda: False
            ctx.cur_stage = "tms"
            ctx.local_src = out_dir / "source.tif"

            old_export = runner.export_tms_from_source
            old_write = runner.write_tilemapresource
            old_mbtiles = runner._maybe_mbtiles
            try:
                def fake_export(src, bbox, levels, tms_dir, **kwargs):
                    calls.append((Path(src).name, list(levels),
                                  kwargs.get("fill_to_tms_zero")))
                    return tms_dir, list(levels), "png", False

                def fake_write(out, provider, title, bbox, levels, ext=None):
                    xml_levels.extend(levels)
                    return Path(out) / "tilemapresource.xml"

                runner.export_tms_from_source = fake_export
                runner.write_tilemapresource = fake_write
                runner._maybe_mbtiles = lambda ctx, stage_key, tiles_dir, scheme, tile_ext: [str(tiles_dir)]

                runner._tms_from_source_file(ctx, out_dir / "tms", None)
            finally:
                runner.export_tms_from_source = old_export
                runner.write_tilemapresource = old_write
                runner._maybe_mbtiles = old_mbtiles

        self.assertEqual(
            calls,
            [
                ("demo_z16.tif", list(range(1, 17)), False),
                ("demo_z17.tif", [17], False),
                ("demo_z18.tif", [18], False),
            ],
        )
        self.assertEqual(xml_levels, list(range(1, 19)))

    def test_tms_from_source_file_can_preserve_each_input_source(self):
        self._stub_optional_terrain_deps()
        import backend.core.runner as runner

        calls = []
        xml_levels = []

        class _Tracker:
            def update(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            for z in (18, 17, 16, 13):
                (out_dir / f"demo_z{z}.tif").write_bytes(b"stub")

            ctx = type("Ctx", (), {})()
            ctx.task = {
                "name": "demo",
                "containers": {"tms": "tiles_dir"},
                "tms_source_strategy": "preserve_inputs",
            }
            ctx.bbox = (0.0, 0.0, 1.0, 1.0)
            ctx.levels = [18, 17, 16, 13]
            ctx.out_dir = out_dir
            ctx.provider = object()
            ctx.tracker = _Tracker()
            ctx.should_stop = lambda: False
            ctx.cur_stage = "tms"
            ctx.local_src = out_dir / "source.tif"

            old_export = runner.export_tms_from_source
            old_write = runner.write_tilemapresource
            old_mbtiles = runner._maybe_mbtiles
            try:
                def fake_export(src, bbox, levels, tms_dir, **kwargs):
                    calls.append((Path(src).name, list(levels),
                                  kwargs.get("fill_to_tms_zero")))
                    return tms_dir, list(levels), "png", False

                def fake_write(out, provider, title, bbox, levels, ext=None):
                    xml_levels.extend(levels)
                    return Path(out) / "tilemapresource.xml"

                runner.export_tms_from_source = fake_export
                runner.write_tilemapresource = fake_write
                runner._maybe_mbtiles = lambda ctx, stage_key, tiles_dir, scheme, tile_ext: [str(tiles_dir)]

                runner._tms_from_source_file(ctx, out_dir / "tms", None)
            finally:
                runner.export_tms_from_source = old_export
                runner.write_tilemapresource = old_write
                runner._maybe_mbtiles = old_mbtiles

        self.assertEqual(
            calls,
            [
                ("demo_z18.tif", [18], False),
                ("demo_z17.tif", [17], False),
                ("demo_z16.tif", [14, 15, 16], False),
                ("demo_z13.tif", list(range(1, 14)), False),
            ],
        )
        self.assertEqual(xml_levels, list(range(1, 19)))

    def test_stage_tms_online_can_preserve_each_geotiff_source(self):
        self._stub_optional_terrain_deps()
        import backend.core.runner as runner

        calls = []
        xml_levels = []

        class _Tracker:
            def start(self, *args, **kwargs):
                pass

            def update(self, *args, **kwargs):
                pass

        with tempfile.TemporaryDirectory() as d:
            out_dir = Path(d)
            for z in (18, 17, 16, 13):
                (out_dir / f"demo_z{z}.tif").write_bytes(b"stub")

            ctx = type("Ctx", (), {})()
            ctx.task = {
                "name": "demo",
                "containers": {"tms": "tiles_dir"},
                "tms_source_strategy": "preserve_inputs",
                "clip": False,
            }
            ctx.bbox = (0.0, 0.0, 1.0, 1.0)
            ctx.levels = [18, 17, 16, 13]
            ctx.out_dir = out_dir
            ctx.provider = object()
            ctx.downloader = object()
            ctx.anno_downloader = None
            ctx.tracker = _Tracker()
            ctx.should_stop = lambda: False
            ctx.local_src = None
            ctx.is_dem = False
            ctx.geom = None

            old_export = runner.export_tms_from_source
            old_direct = runner.export_tms
            old_write = runner.write_tilemapresource
            old_mbtiles = runner._maybe_mbtiles
            try:
                def fail_direct(*args, **kwargs):
                    raise AssertionError("preserve_inputs 不应走原始瓦片直拷")

                def fake_export(src, bbox, levels, tms_dir, **kwargs):
                    calls.append((Path(src).name, list(levels),
                                  kwargs.get("fill_to_tms_zero")))
                    return tms_dir, list(levels), "png", False

                def fake_write(out, provider, title, bbox, levels, ext=None):
                    xml_levels.extend(levels)
                    return Path(out) / "tilemapresource.xml"

                runner.export_tms = fail_direct
                runner.export_tms_from_source = fake_export
                runner.write_tilemapresource = fake_write
                runner._maybe_mbtiles = lambda ctx, stage_key, tiles_dir, scheme, tile_ext: [str(tiles_dir)]

                runner._stage_tms(ctx)
            finally:
                runner.export_tms_from_source = old_export
                runner.export_tms = old_direct
                runner.write_tilemapresource = old_write
                runner._maybe_mbtiles = old_mbtiles

        self.assertEqual(
            calls,
            [
                ("demo_z18.tif", [18], False),
                ("demo_z17.tif", [17], False),
                ("demo_z16.tif", [14, 15, 16], False),
                ("demo_z13.tif", list(range(1, 14)), False),
            ],
        )
        self.assertEqual(xml_levels, list(range(1, 19)))


if __name__ == "__main__":
    unittest.main()
