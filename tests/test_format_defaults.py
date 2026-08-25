import unittest


class FormatDefaultTest(unittest.TestCase):
    def test_geotiff_stages_default_to_cog_container(self):
        from backend.core.containers import container_of

        self.assertEqual(container_of({"containers": {}}, "geotiff"), "cog")
        self.assertEqual(container_of({"containers": {}}, "dem"), "cog")


if __name__ == "__main__":
    unittest.main()
