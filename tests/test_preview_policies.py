"""Distinguish observed geometry, assumed heights, and planning overlays."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import geopandas as gpd
from shapely.geometry import box

from region_model.preview import SOURCE_COLORS, export_obj, write_preview


class PreviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.buildings = gpd.GeoDataFrame(
            {
                "building_id": ["official", "ledger", "estimated"],
                "building_name": ["Do not display name"] * 3,
                "height_m": [61.0, 45.0, 53.0],
                "observed_height_m": [61.0, 45.0, None],
                "height_source": ["gis", "ledger", "imputed"],
                "quality_flags": ["", "", "experimental_height_imputation"],
            },
            geometry=[box(-50, -10, -30, 10), box(0, -10, 20, 10), box(50, -10, 70, 10)],
        )
        selected = self.buildings.loc[[0, 2]]
        self.zones = gpd.GeoDataFrame(
            {
                "building_id": ["official", "estimated"],
                "height_m": [61.0, 53.0],
                "height_source": ["gis", "imputed"],
                "is_estimated": [False, True],
                "threshold_m": [50, 50],
                "flight_altitude_m": [50, 50],
                "safety_margin_m": [5, 5],
            },
            geometry=list(selected.geometry.buffer(5)),
        )
        self.metadata = {
            "size_m": 400,
            "region_name": "검증 지역",
            "center_lonlat": [127.0276, 37.4979],
            "suite": True,
            "concept": "시험 환경",
            "flight_policy": {"flight_altitude_m": 50, "threshold_m": 50, "safety_margin_m": 5},
            "height_policy": {
                "status": "ready", "mean_known_height_m": 53.0,
                "mean_imputed_height_m": 53.0, "seed": 77,
            },
        }

    def preview(self, buildings=None, metadata=None, zones=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preview.html"
            with patch("region_model.preview.pio.to_html", return_value='<div id="region-map"></div>') as render:
                write_preview(
                    self.buildings if buildings is None else buildings, None,
                    self.metadata if metadata is None else metadata, path,
                    no_fly_zones=self.zones if zones is None else zones,
                )
            return render.call_args.args[0], path.read_text(encoding="utf-8")

    def test_assumed_height_is_volume_with_explicit_labels_not_observed_coverage(self):
        figure, html = self.preview()
        estimated = next(trace for trace in figure.data if trace.name == "실험용 추정 높이")
        self.assertEqual(estimated.type, "mesh3d")
        self.assertEqual(set(estimated.z), {0.0, 53.0})
        self.assertEqual(estimated.color, SOURCE_COLORS["imputed"])
        self.assertTrue(all("실제 높이 미확인" in item for item in estimated.text))
        self.assertIn("66.7%", html, "Imputed heights must not inflate observed coverage.")
        self.assertIn("GIS 1 · 대장 연결 1", html)
        self.assertIn("실험용 추정 높이", html)
        self.assertIn("확인 건물 평균 53.000 m · 추정 건물 평균 53.000 m", html)
        self.assertIn("난수 시드: 77", html)
        self.assertNotIn("높이 임의 추정 없음", html)
        self.assertNotIn("모든 입력 건물에 유효한 높이가 있습니다", html)
        self.assertNotIn("Do not display name", html)
        self.assertIn('href="../index.html"', html)

    def test_zone_overlay_does_not_change_actual_volume_and_estimates_are_dashed(self):
        buildings_before, zones_before = self.buildings.copy(), self.zones.copy()
        figure, html = self.preview()
        self.assertTrue(self.buildings.equals(buildings_before))
        self.assertTrue(self.zones.equals(zones_before))
        official = next(trace for trace in figure.data if trace.name == "GIS 속성 높이")
        self.assertEqual((min(official.x), max(official.x)), (-50.0, -30.0))
        self.assertEqual(set(official.z), {0.0, 61.0})
        overlays = [trace for trace in figure.data if trace.meta and trace.meta.get("layer") == "no_fly"]
        self.assertEqual(len(overlays), 4)
        self.assertTrue(all(max(z for z in trace.z if z is not None) < 1 for trace in overlays))
        estimated_outline = next(trace for trace in overlays if trace.type == "scatter3d" and trace.meta["is_estimated"])
        observed_outline = next(trace for trace in overlays if trace.type == "scatter3d" and not trace.meta["is_estimated"])
        self.assertEqual(estimated_outline.line.dash, "dash")
        self.assertEqual(observed_outline.line.dash, "solid")
        self.assertIn("zone-toggle", html)
        self.assertIn("법정 비행 금지구역이 아닙니다", html)

    def test_obj_exports_imputed_model_height_with_source_warning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.obj"
            export_obj(self.buildings, path)
            content = path.read_text(encoding="utf-8")
        self.assertIn("EXPERIMENTAL: 1 buildings use imputed heights", content)
        self.assertIn("height_m=53 height_source=imputed", content)
        self.assertIn("observed_height_m=null", content)
        self.assertIn("height_m=61 height_source=gis", content)
        self.assertIn("height_m=45 height_source=ledger", content)

    def test_observed_only_obj_records_excluded_imputed_buildings(self):
        observed = self.buildings.loc[self.buildings.height_source.isin(["gis", "ledger"])].copy()
        observed.attrs["excluded_imputed_count"] = 1
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "buildings_known_heights.obj"
            export_obj(observed, path)
            content = path.read_text(encoding="utf-8")
        self.assertIn("OBSERVED-ONLY EXPORT: 1 buildings with experimentally imputed heights were excluded", content)
        self.assertIn("not the complete scene", content)
        self.assertIn("excluded buildings must not be treated as free space", content)
        self.assertIn("buildings_model_heights.obj", content)
        self.assertNotIn("height_source=imputed", content)

    def test_held_map_is_clearly_flagged_with_unresolved_footprint(self):
        buildings = self.buildings.copy()
        buildings.loc[2, "height_m"] = None
        buildings.loc[2, "height_source"] = "missing"
        metadata = {**self.metadata, "suite": False, "height_policy": {"status": "held"}}
        figure, html = self.preview(buildings, metadata, self.zones.iloc[:0])
        self.assertIn("보류 상태", html)
        self.assertIn("/ HELD", html)
        self.assertNotIn('href="../index.html"', html)
        missing = next(trace for trace in figure.data if trace.name == "높이 미확인")
        self.assertTrue(all(z < 1 for z in missing.z))


if __name__ == "__main__":
    unittest.main()
