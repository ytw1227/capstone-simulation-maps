"""The two demo extents must show subsets of one stable synthetic scene."""

import unittest

import pandas as pd
from shapely.geometry import box

from region_model.core import localize, prepare_buildings, region_geometry
from region_model.demo import make_demo


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.origin, self.small_aoi = region_geometry(127.0276, 37.4979, 400)
        _, self.large_aoi = region_geometry(127.0276, 37.4979, 1000)
        self.source, self.background, self.schema, self.resolver = make_demo(self.origin)

    def test_scene_covers_outer_bands_and_all_four_corners(self):
        scene = localize(self.source, self.origin)
        self.assertTrue(scene.geometry.apply(box(-500, -500, 500, 500).covers).all())
        regions = {
            "north": box(-500, 400, 500, 500),
            "south": box(-500, -500, 500, -400),
            "east": box(400, -500, 500, 500),
            "west": box(-500, -500, -400, 500),
            "northwest": box(-500, 250, -250, 500),
            "northeast": box(250, 250, 500, 500),
            "southwest": box(-500, -500, -250, -250),
            "southeast": box(250, -500, 500, -250),
        }
        for name, extent in regions.items():
            with self.subTest(region=name):
                self.assertGreaterEqual(int(scene.intersects(extent).sum()), 4)
        xmin, ymin, xmax, ymax = scene.total_bounds
        self.assertLess(xmin, -450)
        self.assertLess(ymin, -450)
        self.assertGreater(xmax, 450)
        self.assertGreater(ymax, 450)

    def test_added_buildings_do_not_overlap_existing_buildings_roads_or_park(self):
        added = self.source.iloc[20:]
        self.assertGreater(len(added), 40)
        for index, row in added.iterrows():
            with self.subTest(building=row.gis_id):
                self.assertFalse(row.geometry.intersects(self.small_aoi))
                self.assertFalse(self.background.intersects(row.geometry).any())
                others = self.source.drop(index)
                self.assertFalse(others.intersects(row.geometry).any())

    def test_both_sizes_clip_same_ids_heights_geometries_and_synthetic_names(self):
        small, _ = prepare_buildings(self.source, self.schema, self.small_aoi, self.resolver)
        large, _ = prepare_buildings(self.source, self.schema, self.large_aoi, self.resolver)
        self.assertEqual(len(small), 20)
        self.assertEqual(len(large), len(self.source))
        self.assertGreater(len(large), len(small))
        self.assertEqual(set(small.building_id), {f"DEMO-{i:02d}" for i in range(20)})
        common_large = large.set_index("building_id").loc[small.building_id]
        common_small = small.set_index("building_id")
        pd.testing.assert_series_equal(common_small.height_m, common_large.height_m)
        pd.testing.assert_series_equal(common_small.height_source, common_large.height_source)
        pd.testing.assert_series_equal(common_small.building_name, common_large.building_name)
        pd.testing.assert_series_equal(common_small.name_source, common_large.name_source)
        self.assertTrue(large.name_source.eq("synthetic").all())
        for building_id in common_small.index:
            self.assertTrue(common_small.loc[building_id].geometry.equals(common_large.loc[building_id].geometry))
        source_by_id = self.source.set_index("gis_id")
        names_small = source_by_id.loc[small.building_id, self.schema["name_field"]]
        names_large = source_by_id.loc[common_large.index, self.schema["name_field"]]
        pd.testing.assert_series_equal(names_small.rename_axis("building_id"), names_large.rename_axis("building_id"))
        self.assertEqual(common_small.building_name.to_dict(), names_small.to_dict())
        self.assertTrue(names_small.str.startswith("합성 ").all())
        self.assertEqual(self.schema["name_source"], "synthetic")
        self.assertEqual(common_small.loc["DEMO-03"].height_source, "ledger")
        self.assertEqual(common_small.loc["DEMO-03"].height_m, 47.5)
        self.assertEqual(int(small.height_source.eq("missing").sum()), 3)

    def test_repeated_generation_is_deterministic_and_keeps_original_special_shapes(self):
        repeated, background, schema, _ = make_demo(self.origin)
        pd.testing.assert_frame_equal(self.source, repeated)
        pd.testing.assert_frame_equal(self.background, background)
        self.assertEqual(self.schema, schema)
        self.assertTrue(self.source.gis_id.is_unique)
        self.assertTrue(self.source.gis_name.is_unique)
        self.assertTrue(self.source.gis_name.str.startswith("합성 ").all())
        self.assertEqual(len(self.source.geometry.iloc[1].interiors), 1)
        self.assertEqual(self.source.geometry.iloc[2].geom_type, "MultiPolygon")
        self.assertTrue(pd.isna(self.source.iloc[18].gis_height))


if __name__ == "__main__":
    unittest.main()
