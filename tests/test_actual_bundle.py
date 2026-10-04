"""CI gate: the committed official 400 m bundle must build offline as delivered."""

from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd

from region_model.__main__ import main


ROOT = Path(__file__).resolve().parent.parent
BUNDLE = ROOT / "data" / "gangnam_400"


class ActualBundleTests(unittest.TestCase):
    def test_committed_actual_bundle_builds_offline_without_losing_buildings(self):
        provenance = json.loads((BUNDLE / "provenance.json").read_text(encoding="utf-8"))
        for layer in ("buildings", "background"):
            source = BUNDLE / provenance[layer]["file"]
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), provenance[layer]["sha256"])

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "actual400"
            with patch("region_model.background.fetch_background", side_effect=AssertionError("Offline build attempted an OSM download")), \
                 redirect_stdout(StringIO()):
                result = main([
                    "build", "--buildings", str(BUNDLE / "buildings.gpkg"),
                    "--schema", str(BUNDLE / "schema.json"),
                    "--provenance", str(BUNDLE / "provenance.json"),
                    "--background", str(BUNDLE / "background.gpkg"),
                    "--out", str(output),
                ])
            self.assertEqual(result, 0)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            scene = json.loads((output / "scene.local.json").read_text(encoding="utf-8"))
            buildings = gpd.read_file(output / "model.gpkg", layer="buildings")
            aoi = gpd.read_file(output / "model.gpkg", layer="aoi").geometry.iloc[0]
            original = gpd.read_file(BUNDLE / "buildings.gpkg", layer="buildings")

            self.assertFalse(manifest["is_demo"])
            self.assertFalse(manifest["simulation_ready"])
            self.assertEqual(manifest["size_m"], 400)
            self.assertAlmostEqual(aoi.area, 160000)
            self.assertEqual(buildings.crs.to_epsg(), 5179)
            self.assertEqual((len(buildings), manifest["counts"]["gis_height"], manifest["counts"]["missing_height"]), (126, 83, 43))
            self.assertEqual(manifest["counts"]["ledger_height"], 0)
            self.assertEqual(manifest["dataset_provenance"], provenance)
            self.assertEqual(manifest["background_source"], "osm")
            self.assertEqual(manifest["building_source"]["snapshot_date"], "2026-09-09")
            self.assertEqual(manifest["building_source"]["record_reference_dates"], ["2026-09-06"])

            self.assertTrue(original.A1.is_unique)
            self.assertEqual(set(buildings.source_building_id), set(original.A1))
            source_by_id = original.set_index("A1")
            expected = source_by_id.loc[buildings.source_building_id, "geometry"].reset_index(drop=True).intersection(aoi)
            differences = buildings.geometry.reset_index(drop=True).symmetric_difference(expected).area
            self.assertLessEqual(float(differences.max()), 1e-8, "A delivered official footprint changed beyond AOI clipping.")
            unknown = buildings.loc[buildings.height_source.eq("missing")]
            self.assertEqual(len(unknown), 43)
            self.assertTrue(unknown.height_m.isna().all())

            obj = (output / "buildings_known_heights.obj").read_text(encoding="utf-8")
            object_building_numbers = {int(number) for number in re.findall(r"^o building_(\d+)_.*_part_\d+$", obj, flags=re.MULTILINE)}
            known_building_numbers = {index for index, building in enumerate(scene["buildings"], start=1) if building["height_m"] is not None}
            self.assertEqual(object_building_numbers, known_building_numbers)
            self.assertEqual(len(object_building_numbers), 83)
            self.assertIn("43 buildings have unresolved height", obj)
            self.assertNotIn("SYNTHETIC", obj)

            html = (output / "preview.html").read_text(encoding="utf-8")
            self.assertIn("OpenStreetMap contributors", html)
            self.assertIn("배포본 기준일 2026-09-09", html)
            self.assertIn("건물 속성 기준일 2026-09-06", html)
            self.assertNotIn("SYNTHETIC · 합성 데이터 예시", html)
            self.assertNotIn("건물 이름표", html)
            self.assertNotIn('id="show-building-names"', html)
            self.assertNotIn("<script src=", html)


if __name__ == "__main__":
    unittest.main()
