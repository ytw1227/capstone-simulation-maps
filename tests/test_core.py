import csv
import json
import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
from shapely.geometry import Polygon, box

from region_model.core import (LedgerResolver, METRIC_CRS, export_model, localize, prepare_buildings,
                               read_buildings, region_geometry, valid_height)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.origin, self.aoi = region_geometry(127.0276, 37.4979, 400)
        self.schema = dict(id_field="id", height_field="height", source_name="Test fixture",
                           source_url="synthetic", snapshot_date="synthetic", height_unit="m", height_semantics="above_ground")
        self.ledger = [dict(ledger_id="01234567890123456789", height_m="55", record_type="title", namespace="HUB-test")]
        self.match = dict(building_id="a", ledger_id="01234567890123456789", namespace="HUB-test", verified="true",
                          identity_method="official_crosswalk", identity_evidence="test building-level crosswalk")

    def frame(self, ids, heights):
        x, y = self.origin
        return gpd.GeoDataFrame({"id": ids, "height": heights}, geometry=[box(x+i*20, y, x+i*20+10, y+10) for i in range(len(ids))], crs=METRIC_CRS)

    def test_metric_square_and_local_origin(self):
        self.assertAlmostEqual(self.aoi.area, 160000)
        local = localize(self.frame(["a"], [10]), self.origin)
        self.assertIsNone(local.crs)
        self.assertEqual(local.geometry.iloc[0].bounds, (0, 0, 10, 10))
        _, large = region_geometry(127.0276, 37.4979, 1000)
        self.assertAlmostEqual(large.area, 1000000)

    def test_gis_first_and_verified_fallback(self):
        resolver = LedgerResolver(self.ledger, [self.match])
        frame, _ = prepare_buildings(self.frame(["a"], [15]), self.schema, self.aoi, resolver)
        self.assertEqual(frame.iloc[0].height_m, 15)
        self.assertEqual(frame.iloc[0].height_source, "gis")
        frame, _ = prepare_buildings(self.frame(["a"], [None]), self.schema, self.aoi, resolver)
        self.assertEqual(frame.iloc[0].height_m, 55)
        self.assertEqual(frame.iloc[0].height_source, "ledger")

    def test_address_only_and_aggregate_rejected(self):
        match = {**self.match, "identity_method": "address_similarity"}
        self.assertIsNone(LedgerResolver(self.ledger, [match]).resolve("a")[0])
        ledger = [{**self.ledger[0], "record_type": "aggregate_title"}]
        self.assertIsNone(LedgerResolver(ledger, [self.match]).resolve("a")[0])

    def test_namespace_and_duplicate_ledger_are_not_joined(self):
        self.assertIsNone(LedgerResolver(self.ledger*2, [self.match]).resolve("a")[0])
        self.assertIsNone(LedgerResolver(self.ledger, [self.match, {**self.match, "building_id": "b"}]).resolve("a")[0])
        self.assertIsNone(LedgerResolver(self.ledger, [{**self.match, "namespace": "legacy"}]).resolve("a")[0])

    def test_missing_height_keeps_footprint_and_duplicate_ids_fail_fallback(self):
        frame, quality = prepare_buildings(self.frame(["a", "a"], [None, 0]), self.schema, self.aoi, LedgerResolver(self.ledger, [self.match]))
        self.assertEqual(len(frame), 2)
        self.assertTrue(frame.height_source.eq("missing").all())
        self.assertEqual(frame.building_id.nunique(), 2)
        self.assertTrue(all("unresolved_height" in row["quality_flags"] for row in quality))

    def test_invalid_heights(self):
        for value in [None, "", "nan", float("inf"), -1, 0]:
            self.assertIsNone(valid_height(value))

    def test_building_names_preserved_and_missing_names_not_invented(self):
        frame = self.frame(["a", "b", "c"], [15, 20, None])
        frame["name"] = ["  테스트 <별관>  ", None, ""]
        schema = {**self.schema, "name_field": "name"}
        buildings, quality = prepare_buildings(frame, schema, self.aoi)
        self.assertEqual(buildings.building_name.tolist(), ["테스트 <별관>", "", ""])
        self.assertEqual(buildings.name_source.tolist(), ["gis", "missing", "missing"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "model"
            background = gpd.GeoDataFrame({"kind": [], "osm_id": []}, geometry=[], crs=METRIC_CRS)
            export_model(output, buildings, background, quality,
                         {"region_name": "이름 테스트", "size_m": 400, "center_lonlat": [127.0276, 37.4979], "is_demo": False},
                         self.origin, self.aoi)
            scene = json.loads((output / "scene.local.json").read_text(encoding="utf-8"))
            self.assertEqual(scene["buildings"][0]["building_name"], "테스트 <별관>")
            self.assertEqual(scene["buildings"][1]["name_source"], "missing")
            saved = gpd.read_file(output / "model.gpkg", layer="buildings")
            self.assertEqual(saved.building_name.iloc[0], "테스트 <별관>")
            with (output / "quality.csv").open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(next(csv.DictReader(handle))["building_name"], "테스트 <별관>")

    def test_optional_name_field_is_checked_and_omission_stays_compatible(self):
        frame = self.frame(["a"], [10])
        with self.assertRaisesRegex(ValueError, "이름 열"):
            prepare_buildings(frame, {**self.schema, "name_field": "absent"}, self.aoi)
        prepared, _ = prepare_buildings(frame, self.schema, self.aoi)
        self.assertEqual(prepared.building_name.iloc[0], "")
        self.assertEqual(prepared.name_source.iloc[0], "missing")

    def test_real_file_loading_uses_full_selected_extent(self):
        x, y = self.origin
        frame = gpd.GeoDataFrame({"id": ["center", "outer"], "height": [20, 30], "name": ["중앙 검증동", "외곽 검증동"]},
                                geometry=[box(x, y, x+10, y+10), box(x+440, y+440, x+480, y+480)], crs=METRIC_CRS)
        schema = {**self.schema, "name_field": "name"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.gpkg"
            frame.to_crs(4326).to_file(path, driver="GPKG")
            _, large = region_geometry(127.0276, 37.4979, 1000)
            small_frame, _ = read_buildings(path, schema, self.aoi)
            large_frame, _ = read_buildings(path, schema, large)
            small, _ = prepare_buildings(small_frame, schema, self.aoi)
            big, _ = prepare_buildings(large_frame, schema, large)
            self.assertEqual(set(small.building_id), {"center"})
            self.assertEqual(set(big.building_id), {"center", "outer"})
            self.assertEqual(big.set_index("building_id").loc["outer", "building_name"], "외곽 검증동")

    def test_clip_repair_and_geo_file_read(self):
        frame = self.frame(["a", "b"], [10, None])
        x, y = self.origin
        frame.loc[0, "geometry"] = box(x+190, y, x+210, y+10)
        frame.loc[1, "geometry"] = Polygon([(x,y),(x+10,y+10),(x,y+10),(x+10,y),(x,y)])
        out, _ = prepare_buildings(frame, self.schema, self.aoi)
        self.assertAlmostEqual(out.iloc[0].area_m2, 100)
        self.assertIn("clipped_at_aoi_boundary", out.iloc[0].quality_flags)
        self.assertTrue(out.is_valid.all())
        self.assertIn("geometry_repaired", out.iloc[1].quality_flags)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"test.gpkg"
            frame.to_crs(4326).to_file(path, driver="GPKG")
            loaded, info = read_buildings(path, self.schema, self.aoi)
            self.assertEqual(loaded.crs.to_epsg(), 5179)
            self.assertEqual(len(loaded), 2)


if __name__ == "__main__":
    unittest.main()
