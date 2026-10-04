"""Importer checks use local synthetic fixtures, never official downloaded data."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import Polygon, box

from region_model.core import METRIC_CRS, prepare_buildings, region_geometry
from scripts.import_official_gangnam import CENTER, SOURCE_NAME, SOURCE_URL, _same_crs, import_official, sha256

# Exact projection representation distributed in the 2026-09-09 official ZIP.
# Only metadata is embedded; all geometries/attributes below remain test fixtures.
OFFICIAL_5186_WKT = '''PROJCS["Korea_2000_Korea_Central_Belt_2010", GEOGCS["GCS_Korea_2000", DATUM["D_Korea_2000", SPHEROID["GRS_1980", 6378137.0, 298.257222101]], PRIMEM["Greenwich", 0.0], UNIT["degree", 0.017453292519943295], AXIS["Longitude", EAST], AXIS["Latitude", NORTH]], PROJECTION["Transverse_Mercator"], PARAMETER["central_meridian", 127.0], PARAMETER["latitude_of_origin", 38.0], PARAMETER["scale_factor", 1.0], PARAMETER["false_easting", 200000.0], PARAMETER["false_northing", 600000.0], UNIT["m", 1.0], AXIS["x", EAST], AXIS["y", NORTH], AUTHORITY["EPSG","5186"]]'''


class OfficialImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.origin, self.aoi = region_geometry(*CENTER, 400)

    def fixture(self, crs=5186):
        x, y = self.origin
        source = self.root / "raw"
        source.mkdir(exist_ok=True)
        frame = gpd.GeoDataFrame({
            "A1": ["0000000000000000000000000001", "duplicate", "duplicate", None, "outside"],
            "A16": [20.5, None, 0.0, 31.0, 15.0],
            "A24": ["테스트동", "중복동1", "중복동2", "식별자없음", "영역밖"],
            "A23": ["11680", "11680", "11650", "11650", "11680"],
        }, geometry=[box(x+190, y, x+220, y+20), box(x, y, x+10, y+10),
                     box(x-40, y, x-30, y+10), box(x-100, y, x-90, y+10),
                     box(x+500, y, x+510, y+10)], crs=METRIC_CRS)
        path = source / "AL_D010_test.shp"
        frame.to_crs(crs).to_file(path, driver="ESRI Shapefile", encoding="UTF-8", index=False)
        return path

    def background_fixture(self, output):
        output.mkdir(exist_ok=True)
        background = output / "background.gpkg"
        background.write_bytes(b"TEST BACKGROUND HASH FIXTURE")
        metadata = {"source": "OpenStreetMap via OSMnx", "status": "ok", "synthetic": False,
                    "buildings_used": False, "center_lonlat": CENTER, "size_m": 400,
                    "file": background.name, "sha256": sha256(background)}
        path = output / "background.provenance.json"
        path.write_text(json.dumps(metadata), encoding="utf-8")
        return background, path, metadata

    def test_full_footprints_ids_missing_height_and_duplicate_records_are_preserved(self):
        source = self.fixture()
        output = self.root / "curated"
        result = import_official(source, "2026-09-09", output)
        actual = gpd.read_file(output / "buildings.gpkg", layer="buildings")
        self.assertEqual(len(actual), 4)
        first = actual.loc[actual.A1.str.startswith("000", na=False)].iloc[0]
        self.assertEqual(first.A1, "0000000000000000000000000001")
        self.assertEqual(first.A24, "테스트동")
        self.assertAlmostEqual(first.geometry.area, 600, places=3)
        self.assertGreater(first.geometry.bounds[2], self.aoi.bounds[2])
        self.assertEqual(result["duplicate_id_rows"], 2)
        self.assertEqual(result["missing_id_rows"], 1)
        self.assertEqual(result["missing_or_invalid_height_rows"], 2)
        self.assertEqual(result["district_codes"], ["11650", "11680"])
        self.assertEqual(result["original_file_sha256"], sha256(source))
        self.assertEqual(result["sha256"], sha256(output / "buildings.gpkg"))
        self.assertEqual(actual.crs.to_epsg(), 5179)
        self.assertFalse((output / "provenance.json").exists())
        self.assertNotIn(str(self.root), (output / "buildings.provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(actual.A16.eq(0).sum(), 1)
        schema = json.loads((output / "schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["source_name"], SOURCE_NAME)
        self.assertEqual(schema["source_url"], SOURCE_URL)
        self.assertEqual(schema["snapshot_date"], "2026-09-09")
        self.assertEqual(result["source_parts"][0]["original_crs"], "EPSG:5186")
        self.assertIn(source.with_suffix(".cpg").name, result["source_parts"][0]["component_sha256"])

    def test_zip_import_and_existing_background_form_checked_portable_bundle(self):
        source = self.fixture()
        archive = self.root / "official-fixture.zip"
        with zipfile.ZipFile(archive, "w") as writer:
            for companion in source.parent.iterdir():
                writer.write(companion, "nested/" + companion.name)
        output = self.root / "curated"
        # Only hashing/merging is in this importer's scope; the background
        # builder separately validates background GPKG geometry and provenance.
        background, _, _ = self.background_fixture(output)
        import_official(archive, "2026-09-09", output, layer="AL_D010_test")
        bundle = json.loads((output / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(bundle["dataset_id"], "gangnam_actual_400")
        self.assertEqual(bundle["center_lonlat"], CENTER)
        self.assertEqual(bundle["size_m"], 400)
        self.assertEqual(bundle["background"]["sha256"], sha256(background))
        self.assertEqual(bundle["buildings"]["source_parts"][0]["archive_member"], "nested/AL_D010_test.shp")
        self.assertEqual(bundle["buildings"]["original_file_sha256"], sha256(archive))

    def test_crs_mismatch_refuses_to_relabel_source(self):
        source = self.fixture(crs=5179)
        with self.assertRaisesRegex(ValueError, "CRS"):
            import_official(source, "2026-09-09", self.root / "curated")
        self.assertFalse((self.root / "curated" / "buildings.gpkg").exists())

    def test_existing_output_and_bad_background_hash_are_not_overwritten(self):
        source = self.fixture()
        output = self.root / "curated"
        output.mkdir()
        sentinel = output / "schema.json"
        sentinel.write_text("KEEP", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "덮어쓰지"):
            import_official(source, "2026-09-09", output)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "KEEP")
        other = self.root / "other"
        other.mkdir()
        (other / "background.gpkg").write_bytes(b"CHANGED")
        (other / "background.provenance.json").write_text('{"sha256":"wrong"}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "해시"):
            import_official(source, "2026-09-09", other)
        self.assertFalse((other / "buildings.gpkg").exists())

    def test_missing_projection_is_not_guessed(self):
        source = self.fixture()
        source.with_suffix(".prj").unlink()
        with self.assertRaisesRegex(ValueError, "부속 파일"):
            import_official(source, "2026-09-09", self.root / "curated")

    def test_background_scope_and_missing_hash_cannot_become_actual_bundle(self):
        source = self.fixture()
        for key, value, message in (("sha256", None, "해시"), ("center_lonlat", [127.1, 37.5], "중심점"),
                                    ("synthetic", True, "실제 OSM"), ("buildings_used", True, "실제 OSM")):
            with self.subTest(key=key):
                output = self.root / key
                _, metadata_path, metadata = self.background_fixture(output)
                metadata[key] = value
                metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    import_official(source, "2026-09-09", output)
                self.assertFalse((output / "provenance.json").exists())
                self.assertFalse((output / "buildings.gpkg").exists())

    def test_invalid_geometry_and_nonpositive_or_unparseable_heights_remain_reviewable(self):
        x, y = self.origin
        # Text A16 allows checking preservation without DBF converting infinities
        # or malformed numeric values on write. No parser cleanup is permitted.
        values = ["-1", "0", "NaN", "inf", "unknown", "21.75"]
        invalid = Polygon([(x, y), (x+10, y+10), (x, y+10), (x+10, y), (x, y)])
        frame = gpd.GeoDataFrame({"A1": [f"id-{index}" for index in range(6)], "A16": values},
            geometry=[invalid] + [box(x+20*index, y+20, x+20*index+10, y+30) for index in range(1, 6)],
            crs=METRIC_CRS)
        source = self.root / "invalids.shp"
        frame.to_crs(5186).to_file(source, driver="ESRI Shapefile", index=False)
        output = self.root / "curated"
        result = import_official(source, "2026-09-09", output)
        actual = gpd.read_file(output / "buildings.gpkg", layer="buildings")
        self.assertEqual(actual.A16.tolist(), values)
        self.assertEqual(result["missing_or_invalid_height_rows"], 5)
        self.assertFalse(actual.geometry.iloc[0].is_valid)
        self.assertIn("invalid_geometry_requires_core_repair", actual.import_quality_flags.iloc[0])
        schema = json.loads((output / "schema.json").read_text(encoding="utf-8"))
        blocks, quality = prepare_buildings(actual, schema, self.aoi)
        self.assertEqual(len(blocks), 6)
        self.assertEqual(int(blocks.height_m.isna().sum()), 5)
        self.assertTrue(blocks.geometry.is_valid.all())
        self.assertTrue(any("geometry_repaired" in row["quality_flags"] for row in quality))

    def test_numeric_building_id_is_rejected_instead_of_stringifying_rounded_id(self):
        source = self.root / "numeric-id.shp"
        frame = gpd.GeoDataFrame({"A1": [12345], "A16": [12.0]},
                                geometry=[self.aoi.buffer(-100)], crs=METRIC_CRS)
        frame.to_crs(5186).to_file(source, driver="ESRI Shapefile", index=False)
        with self.assertRaisesRegex(ValueError, "문자열"):
            import_official(source, "2026-09-09", self.root / "curated")

    def test_official_xy_m_unit_wkt_matches_but_false_authority_label_does_not(self):
        expected = CRS.from_epsg(5186)
        actual_prj = CRS.from_wkt(OFFICIAL_5186_WKT)
        self.assertTrue(_same_crs(actual_prj, expected))
        conflicting_offset = CRS.from_wkt(OFFICIAL_5186_WKT.replace('200000.0', '200100.0'))
        conflicting_datum = CRS.from_wkt(OFFICIAL_5186_WKT.replace('D_Korea_2000', 'Unknown_Datum'))
        self.assertFalse(_same_crs(conflicting_offset, expected))
        self.assertFalse(_same_crs(conflicting_datum, expected))
        source = self.fixture()
        source.with_suffix(".prj").write_text(OFFICIAL_5186_WKT, encoding="utf-8")
        result = import_official(source, "2026-09-09", self.root / "curated")
        self.assertEqual(result["row_count"], 4)


if __name__ == "__main__":
    unittest.main()
