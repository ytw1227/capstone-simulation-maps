"""Offline background validation uses synthetic cases and the public OSM snapshot."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import geopandas as gpd
import pandas as pd
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon, box

from region_model.background import OSM_TAGS, fetch_background, load_background
from region_model.core import localize, region_geometry


CRS = "EPSG:5179"
X, Y = 950000, 1950000


def located(geometry):
    return translate(geometry, X, Y)


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.aoi = located(box(0, 0, 100, 100))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fake_osmnx(self, *, features=None, error=None):
        return SimpleNamespace(
            __version__="2.1.1",
            settings=SimpleNamespace(
                cache_folder="original-cache",
                use_cache=False,
                cache_only_mode=True,
                requests_timeout=12,
                log_file=True,
            ),
            features_from_polygon=Mock(return_value=features, side_effect=error),
        )

    def fetch_with(self, fake):
        with patch.dict("sys.modules", {"osmnx": fake}):
            return fetch_background(self.aoi, CRS, self.directory / "osm-cache")

    def test_fetch_filters_buildings_points_and_road_areas_then_clips_and_repairs(self):
        source = gpd.GeoDataFrame(
            [
                {"geometry": LineString([(-20, 50), (120, 50)]), "highway": "residential",
                 "name": "검증 도로", "name:en": "Fixture road", "tunnel": "yes", "layer": "-1", "bridge": "no"},
                {"geometry": box(-20, -20, 20, 20), "leisure": "park"},
                {"geometry": box(60, 60, 130, 130), "landuse": "residential"},
                {"geometry": LineString([(40, -20), (40, 120)]), "waterway": "stream"},
                {"geometry": box(70, 0, 120, 20), "natural": "water"},
                {"geometry": box(1, 1, 5, 5), "landuse": "retail", "building": "yes"},
                {"geometry": Point(30, 30), "highway": "bus_stop"},
                {"geometry": box(20, 20, 25, 25), "highway": "pedestrian"},
                {"geometry": box(200, 200, 250, 250), "landuse": "residential"},
                {"geometry": Polygon([(10, 60), (30, 80), (10, 80), (30, 60), (10, 60)]),
                 "leisure": "garden"},
            ],
            geometry="geometry",
            crs=CRS,
        )
        source.geometry = source.geometry.apply(located)
        source.index = pd.MultiIndex.from_tuples(
            [("way", number) for number in range(1, 11)], names=["element", "id"]
        )
        fake = self.fake_osmnx(features=source)
        original_settings = vars(fake.settings).copy()
        result, metadata = self.fetch_with(fake)

        self.assertEqual(metadata["status"], "ok")
        self.assertEqual(metadata["counts_by_kind"], {"park": 3, "water": 2, "road": 1, "landuse": 1})
        self.assertEqual(metadata["repaired_features"], 1)
        self.assertEqual(metadata["discarded_features"], 4)
        self.assertFalse(metadata["buildings_used"])
        self.assertEqual(result.crs.to_epsg(), 5179)
        self.assertTrue(result.geometry.is_valid.all())
        self.assertTrue(result.geometry.apply(self.aoi.covers).all())
        self.assertTrue((result.loc[result.kind == "road"].geom_type == "LineString").all())
        self.assertEqual(result.loc[result.kind == "road"].length.iloc[0], 100)
        road = result.loc[result.kind == "road"].iloc[0]
        self.assertEqual(road["name"], "검증 도로")
        self.assertEqual(road["name_en"], "Fixture road")
        self.assertEqual(road["highway"], "residential")
        self.assertEqual((road["tunnel"], road["layer"], road["osm_layer"], road["bridge"]), ("yes", "-1", "-1", "no"))
        self.assertNotIn("way/6", result.osm_id.values)
        self.assertEqual(vars(fake.settings), original_settings)

        query = fake.features_from_polygon.call_args
        expected_wgs84 = gpd.GeoSeries([self.aoi], crs=CRS).to_crs(4326).iloc[0]
        self.assertTrue(query.args[0].equals_exact(expected_wgs84, 1e-10))
        self.assertEqual(query.kwargs["tags"], OSM_TAGS)
        self.assertNotIn("building", query.kwargs["tags"])
        self.assertTrue((self.directory / "osm-cache").is_dir())

    def test_fetch_reports_no_results_separately_from_network_and_malformed_errors(self):
        insufficient_response = type("InsufficientResponseError", (ValueError,), {})
        cases = [
            (insufficient_response("No matching features. Check query location, tags, and log."), "no_results"),
            (insufficient_response("Overpass API did not return a dict of results."), "error"),
            (TimeoutError("test timeout"), "error"),
        ]
        for error, expected_status in cases:
            with self.subTest(error=str(error)):
                fake = self.fake_osmnx(error=error)
                original_settings = vars(fake.settings).copy()
                result, metadata = self.fetch_with(fake)
                self.assertTrue(result.empty)
                self.assertEqual(set(result.columns), {"geometry", "kind", "osm_id"})
                self.assertEqual(result.crs.to_epsg(), 5179)
                self.assertEqual(metadata["status"], expected_status)
                self.assertEqual(metadata["error_type"], type(error).__name__)
                self.assertEqual(vars(fake.settings), original_settings)

    def test_load_cached_geopackage_reprojects_and_clips_preserving_holes(self):
        polygon = Polygon(
            [(-10, -10), (110, -10), (110, 110), (-10, 110), (-10, -10)],
            [[(30, 30), (30, 50), (50, 50), (50, 30), (30, 30)]],
        )
        source = gpd.GeoDataFrame(
            {"kind": ["park"], "osm_id": ["way/123"]},
            geometry=[located(polygon)], crs=CRS,
        )
        path = self.directory / "background.gpkg"
        source.to_crs(4326).to_file(path, driver="GPKG")

        result, metadata = load_background(path, CRS, self.aoi)

        self.assertEqual(metadata["status"], "ok")
        self.assertEqual(result.crs.to_epsg(), 5179)
        self.assertTrue(result.geometry.apply(self.aoi.covers).all())
        self.assertAlmostEqual(result.area.sum(), 9600, places=3)
        self.assertEqual(len(result.geometry.iloc[0].interiors), 1)
        self.assertEqual(result.osm_id.tolist(), ["way/123"])

    def test_load_requires_kind_and_does_not_guess_missing_osm_id(self):
        source = gpd.GeoDataFrame(geometry=[located(box(10, 10, 20, 20))], crs=CRS)
        missing_kind = self.directory / "missing-kind.gpkg"
        source.to_file(missing_kind, driver="GPKG")
        result, metadata = load_background(missing_kind, CRS, self.aoi)
        self.assertTrue(result.empty)
        self.assertEqual(metadata["status"], "error")
        self.assertIn("kind", metadata["error"])

        source["kind"] = "landuse"
        without_id = self.directory / "without-id.gpkg"
        source.to_file(without_id, driver="GPKG")
        result, metadata = load_background(without_id, CRS, self.aoi)
        self.assertEqual(metadata["status"], "ok")
        self.assertFalse(metadata["osm_ids_present"])
        self.assertTrue(result.osm_id.isna().all())

    def test_load_rejects_missing_crs_and_building_kind(self):
        cases = [
            gpd.GeoDataFrame({"kind": ["park"]}, geometry=[located(box(1, 1, 5, 5))]),
            gpd.GeoDataFrame({"kind": ["building"]}, geometry=[located(box(1, 1, 5, 5))], crs=CRS),
        ]
        for source in cases:
            with self.subTest(crs=str(source.crs), kind=source.kind.iloc[0]):
                with patch("region_model.background.gpd.read_file", return_value=source):
                    result, metadata = load_background(self.directory / "input.gpkg", CRS, self.aoi)
                self.assertTrue(result.empty)
                self.assertEqual(metadata["status"], "error")

    def test_empty_intersection_is_no_results_and_nonmetric_aoi_is_rejected(self):
        source = gpd.GeoDataFrame(
            {"kind": ["park"]}, geometry=[located(box(200, 200, 210, 210))], crs=CRS
        )
        with patch("region_model.background.gpd.read_file", return_value=source):
            result, metadata = load_background(self.directory / "outside.gpkg", CRS, self.aoi)
        self.assertTrue(result.empty)
        self.assertEqual(metadata["status"], "no_results")
        with self.assertRaisesRegex(ValueError, "metre units"):
            fetch_background(box(126, 37, 127, 38), "EPSG:4326", self.directory)

    def test_public_snapshot_keeps_road_names_and_underground_water_tags_when_localized(self):
        path = Path(__file__).resolve().parents[1] / "data/gangnam_400/background.gpkg"
        origin, aoi = region_geometry(127.0276, 37.4979, 400)
        result, metadata = load_background(path, CRS, aoi)
        self.assertEqual(metadata["status"], "ok")
        self.assertEqual(metadata["path"], "background.gpkg")
        self.assertEqual(len(result), 126)
        self.assertEqual(metadata["counts_by_kind"], {"road": 114, "landuse": 11, "water": 1})
        self.assertEqual(int((result.kind.eq("road") & result["name"].notna()).sum()), 48)
        self.assertIn("강남대로", result["name"].values)
        water = result.loc[result.osm_id.eq("way/361039491")].iloc[0]
        self.assertEqual((water["name"], water["waterway"], water["tunnel"], water["osm_layer"]),
                         ("반포천", "stream", "yes", "-1"))
        localized = localize(result, origin)
        pd.testing.assert_frame_equal(result.drop(columns="geometry"), localized.drop(columns="geometry"))
        self.assertTrue(localized.geometry.apply(box(-200, -200, 200, 200).covers).all())


if __name__ == "__main__":
    unittest.main()
