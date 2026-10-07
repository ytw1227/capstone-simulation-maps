"""Synthetic, explicitly labeled fixtures verify region selection and raw preservation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import box

from region_model.core import METRIC_CRS, region_geometry
from scripts.import_five_regions import import_region


class FiveRegionImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.center = [127.1216, 37.3654]
        self.origin, self.aoi = region_geometry(*self.center, 400)
        x, y = self.origin
        other, _ = region_geometry(127.027, 37.4968, 400)
        self.frame = gpd.GeoDataFrame({
            "A1": ["000001", "000002", "other-region"],
            "A16": [51.75, 0.0, 200.0],
            "A19": ["000077", None, "99"],
            "A24": ["시험한글건물", None, "다른지역"],
            "A25": ["제1동", None, None],
            "A26": [17, 0, 40],
        }, geometry=[box(x+190, y, x+230, y+20), box(x, y, x+10, y+10),
                     box(other[0], other[1], other[0]+10, other[1]+10)], crs=METRIC_CRS)
        self.source = self.root / "synthetic-test-fixture.shp"
        self.frame.to_crs(5186).to_file(self.source, driver="ESRI Shapefile", encoding="cp949", index=False)
        self.config = {
            "name": "테스트", "environment": "명시적 테스트 fixture",
            "center_lonlat": self.center, "center_note": "테스트 전용",
            "source_archive": self.source.name, "source_snapshot_date": "2026-09-09",
            "data_dir": "output",
        }

    def test_configured_region_and_raw_height_geometry_korean_identity_are_preserved(self):
        result = import_region("test", self.config, "test-fixture", root=self.root)
        actual = gpd.read_file(self.root / "output/buildings.gpkg")
        self.assertEqual(set(actual.A1), {"000001", "000002"})
        first = actual.set_index("A1").loc["000001"]
        self.assertEqual(first.A24, "시험한글건물")
        self.assertEqual(first.A25, "제1동")
        self.assertEqual(first.A19, "000077")
        self.assertEqual(first.A16, 51.75)
        self.assertAlmostEqual(first.geometry.area, 800, places=3)
        self.assertGreater(first.geometry.bounds[2], self.aoi.bounds[2])
        self.assertEqual(actual.set_index("A1").loc["000002"].A16, 0.0)
        self.assertEqual(result["known_height_rows"], 1)
        self.assertEqual(result["missing_or_invalid_height_rows"], 1)
        self.assertFalse(result["hold_recommended"])
        self.assertEqual(result["hold_recommendation_scope"], "GIS_only_preliminary")
        self.assertEqual(result["ledger_check_status"], "not_performed_by_importer")
        self.assertEqual(result["center_lonlat"], self.center)
        quality = json.loads((self.root / "output/import_quality.json").read_text(encoding="utf-8"))
        self.assertEqual(quality["missing_height_rows"], 1)
        self.assertNotIn(str(self.root), (self.root / "output/buildings.provenance.json").read_text(encoding="utf-8"))

    def test_existing_background_is_preserved_and_existing_building_artifact_is_not_replaced(self):
        output = self.root / "output"
        output.mkdir()
        background = output / "background.gpkg"
        background.write_bytes(b"EXPLICIT BACKGROUND SENTINEL")
        import_region("test", self.config, "test-fixture", root=self.root)
        self.assertEqual(background.read_bytes(), b"EXPLICIT BACKGROUND SENTINEL")
        original = (output / "buildings.gpkg").read_bytes()
        with self.assertRaisesRegex(ValueError, "덮어쓰지"):
            import_region("test", self.config, "test-fixture", root=self.root)
        self.assertEqual((output / "buildings.gpkg").read_bytes(), original)

    def test_boundary_only_contact_is_excluded_and_region_selection_evidence_is_used(self):
        # Exact metric fixture isolates the intersection rule from CRS roundoff.
        x, y = self.origin
        frame = self.frame.copy()
        frame.loc[frame.A1 == "000002", "geometry"] = box(x+200, y, x+210, y+10)
        configured = {**self.config, "selection_source": "https://example.org/official-landmark",
                      "center_note": "Official GIS landmark polygon centroid."}
        with patch("scripts.import_five_regions.read_buildings", return_value=(frame, {"bbox_filter_rows": len(frame)})):
            result = import_region("test", configured, "https://example.org/shared-chat", root=self.root)
        actual = gpd.read_file(self.root / "output/buildings.gpkg")
        self.assertEqual(actual.A1.tolist(), ["000001"])
        self.assertEqual(result["selection_source"], configured["selection_source"])
        notice = (self.root / "output/LICENSE-BUILDINGS.md").read_text(encoding="utf-8")
        self.assertIn(configured["center_note"], notice)
        self.assertIn(configured["selection_source"], notice)
        self.assertNotIn("shared-conversation recommendation", notice)


if __name__ == "__main__":
    unittest.main()
