"""Readiness gates and provenance binding for the offline five-region runner."""
import csv
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import LineString, box

from region_model.core import METRIC_CRS, region_geometry, write_json
from region_model.suite import build_region, file_sha256, region_seed
from run_five_maps import main as run_main


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.region = {"name": "검증 영역", "environment": "테스트용 작은 입력",
                       "center_lonlat": [127.027, 37.4968], "center_note": "Unit test fixture",
                       "data_dir": "data/regions_400/gangnam"}
        self.directory = self.root / self.region["data_dir"]
        self.directory.mkdir(parents=True)
        self.origin, self.aoi = region_geometry(*self.region["center_lonlat"], 400)

    def make_bundle(self, heights):
        x, y = self.origin
        buildings = gpd.GeoDataFrame({"A1": [f"b{i}" for i in range(len(heights))],
                                     "A16": heights, "A24": [""] * len(heights)},
            geometry=[box(x + i * 15, y, x + i * 15 + 10, y + 10) for i in range(len(heights))], crs=METRIC_CRS)
        buildings.to_file(self.directory / "buildings.gpkg", layer="buildings", driver="GPKG", index=False)
        background = gpd.GeoDataFrame({"kind": ["road"], "osm_id": ["way/1"]},
            geometry=[LineString([(x - 100, y - 20), (x + 100, y - 20)])], crs=METRIC_CRS)
        background.to_file(self.directory / "background.gpkg", layer="background", driver="GPKG", index=False)
        building_source = {"source_name": "국토교통부 GIS건물통합정보 (브이월드 AL_D010)",
            "source_url": "https://www.vworld.kr/dtmk/dtmk_ntads_s002.do?svcCde=NA&dsId=18",
            "snapshot_date": "2026-09-09", "record_reference_dates": ["2026-09-06"],
            "original_file_sha256": "0" * 64, "file": "buildings.gpkg", "layer": "buildings",
            "sha256": file_sha256(self.directory / "buildings.gpkg"), "is_synthetic": False,
            "center_lonlat": self.region["center_lonlat"], "size_m": 400, "region_key": "gangnam",
            "aoi_bounds_projected_m": list(self.aoi.bounds), "row_count": len(buildings)}
        write_json(self.directory / "buildings.provenance.json", building_source)
        write_json(self.directory / "background.provenance.json", {
            "source": "OpenStreetMap via OSMnx", "status": "ok", "buildings_used": False,
            "file": "background.gpkg", "sha256": file_sha256(self.directory / "background.gpkg"),
            "synthetic": False, "center_lonlat": self.region["center_lonlat"], "size_m": 400,
            "region_key": "gangnam", "aoi_bounds_metric": list(self.aoi.bounds), "feature_count": 1,
            "counts_by_kind": {"road": 1, "park": 0, "landuse": 0, "water": 0}})
        write_json(self.directory / "schema.json", {
            **{name: building_source[name] for name in ("source_name", "source_url", "snapshot_date", "record_reference_dates")},
            "id_field": "A1", "height_field": "A16", "name_field": "A24", "layer": "buildings",
            "source_crs": METRIC_CRS, "height_unit": "m", "height_semantics": "above_ground"})
        for name in ("LICENSE-BUILDINGS.md", "LICENSE-OSM.md"):
            (self.directory / name).write_text("Unit test fixture; not actual published data.", encoding="utf-8")

    def audit(self, statuses):
        write_json(self.directory / "ledger_audit.json", {
            "buildings": {key: {"status": status} for key, status in statuses.items()},
            "region_key": "gangnam", "building_source_sha256": file_sha256(self.directory / "buildings.gpkg")})

    def write_ledger(self):
        for name, rows in (
            ("ledger.csv", [{"namespace": "test", "ledger_id": "title1", "record_type": "title", "height_m": "55"}]),
            ("verified_matches.csv", [{"building_id": "b1", "namespace": "test", "ledger_id": "title1",
                "verified": "true", "identity_method": "official_crosswalk", "identity_evidence": "Fixture exact title ID match"}]),
        ):
            with (self.directory / name).open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)

    def build(self):
        with patch("region_model.background.fetch_background", side_effect=AssertionError("Build must be offline")):
            return build_region("gangnam", self.region, self.root / "output", project_root=self.root, seed=17)

    def test_unchecked_height_stays_pending_without_3d_or_estimates(self):
        self.make_bundle([60.0, 40.0, None])
        result = self.build()
        self.assertEqual(result["status"], "pending_ledger")
        self.assertEqual(result["counts"]["imputed_height"], 0)
        self.assertEqual(result["height_policy"]["ledger_pending_count"], 1)
        for name in ("model.gpkg", "scene.local.json", "buildings_model_heights.obj"):
            self.assertFalse((self.root / "output" / name).exists())
        self.assertIn("대장 확인 대기", (self.root / "output/preview.html").read_text(encoding="utf-8"))

    def test_checked_equal_missing_and_known_heights_build_balanced_estimates(self):
        self.make_bundle([60.0, 40.0, None, None])
        original_hash = file_sha256(self.directory / "buildings.gpkg")
        self.audit({"b2": "title_height_missing", "b3": "identity_ambiguous"})
        result = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["counts"]["imputed_height"], 2)
        self.assertEqual(result["height_policy"]["mean_known_height_m"], 50.0)
        self.assertAlmostEqual(result["height_policy"]["mean_imputed_height_m"], 50.0)
        self.assertEqual(result["height_policy"]["seed"], region_seed(17, "gangnam"))
        scene = json.loads((self.root / "output/scene.local.json").read_text(encoding="utf-8"))
        by_id = {building["building_id"]: building for building in scene["buildings"]}
        self.assertEqual(by_id["b0"]["height_m"], 60.0)
        self.assertEqual(by_id["b1"]["height_m"], 40.0)
        self.assertIsNone(by_id["b2"]["observed_height_m"])
        self.assertEqual(by_id["b2"]["height_source"], "imputed")
        self.assertEqual(original_hash, file_sha256(self.directory / "buildings.gpkg"))
        self.assertFalse(result["all_heights_verified"])
        self.assertEqual(result["no_fly_counts"]["confirmed_height"], 1)
        self.assertGreaterEqual(result["no_fly_counts"]["estimated_height"], 1)

    def test_checked_unknown_majority_holds_without_a_model(self):
        self.make_bundle([60.0, None, None])
        self.audit({"b1": "title_height_missing", "b2": "no_title_found"})
        result = self.build()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["counts"]["imputed_height"], 0)
        self.assertFalse((self.root / "output/model.gpkg").exists())

    def test_verified_ledger_resolution_precedes_majority_gate(self):
        self.make_bundle([60.0, None, None])
        self.audit({"b1": "verified_height", "b2": "title_height_missing"})
        self.write_ledger()
        result = self.build()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["counts"]["ledger_height"], 1)
        self.assertEqual(result["counts"]["imputed_height"], 1)
        self.assertEqual(result["height_policy"]["mean_known_height_m"], 57.5)

    def test_recorded_ledger_and_match_checksums_reject_changed_bytes(self):
        self.make_bundle([60.0, None])
        self.audit({"b1": "verified_height"})
        self.write_ledger()
        audit_path = self.directory / "ledger_audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["ledger_sha256"] = file_sha256(self.directory / "ledger.csv")
        audit["verified_matches_sha256"] = file_sha256(self.directory / "verified_matches.csv")
        write_json(audit_path, audit)
        for name in ("ledger.csv", "verified_matches.csv"):
            with self.subTest(file=name):
                path = self.directory / name
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(ValueError, "CSV SHA256"):
                    self.build()
                path.write_bytes(original)

    def test_adopted_ledger_height_requires_matching_review_status_and_identity(self):
        self.make_bundle([60.0, None])
        self.audit({"b1": "identity_unconfirmed"})
        self.write_ledger()
        with self.assertRaisesRegex(ValueError, "verified_height"):
            self.build()
        self.audit({"b1": "verified_height"})
        audit_path = self.directory / "ledger_audit.json"
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        audit["buildings"]["b1"]["matched_ledger_id"] = "a-different-title"
        write_json(audit_path, audit)
        with self.assertRaisesRegex(ValueError, "ledger_id"):
            self.build()

    def test_wrong_center_and_changed_file_are_rejected(self):
        self.make_bundle([60.0])
        record_path = self.directory / "buildings.provenance.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["center_lonlat"] = [127.1, 37.5]
        write_json(record_path, record)
        with self.assertRaisesRegex(ValueError, "중심점"):
            self.build()
        record["center_lonlat"] = self.region["center_lonlat"]
        record["sha256"] = "a" * 64
        write_json(record_path, record)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.build()

    def test_floors_cannot_be_relabelled_as_gis_height(self):
        self.make_bundle([60.0])
        schema_path = self.directory / "schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema["height_field"] = "A26"
        write_json(schema_path, schema)
        with self.assertRaisesRegex(ValueError, "height_field"):
            self.build()

    def test_runner_no_browser_option_and_parameters(self):
        with patch("run_five_maps.build_suite", return_value={"regions": []}) as build, \
             patch("run_five_maps.webbrowser.open") as browser, redirect_stdout(StringIO()):
            result = run_main(["--no-browser", "--out", str(self.root / "run"),
                               "--safety-margin", "8", "--seed", "31"])
        self.assertEqual(result, 0)
        self.assertEqual(build.call_args.kwargs["safety_margin_m"], 8.0)
        self.assertEqual(build.call_args.kwargs["seed"], 31)
        browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
