"""Identity safeguards for public HUB title data; fixtures are wholly synthetic."""
import json
from pathlib import Path
import tempfile
import unittest

import geopandas as gpd
from shapely.geometry import box

from scripts.import_official_gangnam import sha256
from scripts.reconcile_five_ledgers import FIELDS, SOURCE_URL, identity_comparison, reconcile_region, title_pnu


def fixtures():
    gis = {"A1": "gis-1", "A2": "1156011000100240000", "A3": "1156011000",
           "A12": 100.0, "A14": 1000.0, "A13": "2001-02-03", "A16": 0.0,
           "A19": "7759", "A24": "시험건물", "A25": "101동", "A26": 10}
    title = {"SGG_CD": "11560", "STDG_CD": "11000", "PLOT_SE_CD": "0", "MNO": "24", "SNO": "0",
             "BDRG_SN": "new-title-1", "LDGR_KIND_CD": "3", "HG": 37.25,
             "BDAR": 100.0, "GFA": 1000.0, "USE_APRV_DAY": "20010203", "GRND_NOFL": 10,
             "BLDG_NM": "시험건물", "DNG_NM": "101동"}
    return gis, title


class LedgerIdentityTests(unittest.TestCase):
    def test_exact_parcel_requires_correct_ordinary_mountain_mapping_and_digits(self):
        _, title = fixtures()
        self.assertEqual(title_pnu(title), "1156011000100240000")
        title["PLOT_SE_CD"] = "1"
        self.assertEqual(title_pnu(title), "1156011000200240000")
        title["PLOT_SE_CD"] = "2"
        self.assertIsNone(title_pnu(title))

    def test_parcel_or_same_name_alone_does_not_confirm_identity(self):
        gis, title = fixtures()
        gis.update(A12=0, A14=0, A26=0, A13=None)
        self.assertFalse(identity_comparison(gis, title, [gis], [title])["accepted"])

    def test_full_source_extra_building_prevents_false_one_to_one_after_aoi_clipping(self):
        gis, title = fixtures()
        gis.update(A24=None, A25=None)
        outside = {**gis, "A1": "outside-aoi"}
        self.assertTrue(identity_comparison(gis, title, [gis], [title])["accepted"])
        self.assertFalse(identity_comparison(gis, title, [gis, outside], [title])["accepted"])

    def test_unique_exact_dong_and_independent_attributes_can_disambiguate_same_parcel(self):
        gis, title = fixtures()
        other_gis = {**gis, "A1": "gis-2", "A25": "102동"}
        other_title = {**title, "BDRG_SN": "new-title-2", "DNG_NM": "102동"}
        result = identity_comparison(gis, title, [gis, other_gis], [title, other_title])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["a19_to_hub_pk_mapping_used"])
        other_gis["A25"] = "101동"
        self.assertFalse(identity_comparison(gis, title, [gis, other_gis], [title, other_title])["accepted"])

    def test_conflicting_area_or_approval_date_prevents_transfer(self):
        gis, title = fixtures()
        for field, value in [("BDAR", 115), ("USE_APRV_DAY", "20200101"), ("GRND_NOFL", 15)]:
            with self.subTest(field=field):
                changed = {**title, field: value}
                self.assertFalse(identity_comparison(gis, changed, [gis], [changed])["accepted"])

    def test_building_id_prefix_coincidence_does_not_add_evidence(self):
        gis, title = fixtures()
        title["BDRG_SN"] = "11560-7759"
        gis.update(A12=0, A14=0, A26=0, A13=None, A24=None, A25=None)
        result = identity_comparison(gis, title, [gis], [title])
        self.assertFalse(result["accepted"])


class LedgerReconciliationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "data/regions_400/test"
        self.directory.mkdir(parents=True)
        gis, self.title = fixtures()
        known = {**gis, "A1": "known", "A2": "1156011000100250000", "A16": 60.0}
        frame = gpd.GeoDataFrame([gis, known], geometry=[box(0, 0, 10, 10), box(20, 0, 30, 10)], crs=5179)
        frame.to_file(self.directory / "buildings.gpkg", layer="buildings", driver="GPKG", index=False)
        source = {"sha256": sha256(self.directory / "buildings.gpkg"), "original_file_sha256": "synthetic-original"}
        (self.directory / "buildings.provenance.json").write_text(json.dumps(source), encoding="utf-8")
        context = {"original_file_sha256": "synthetic-original", "records_by_pnu": {gis["A2"]: [gis], known["A2"]: [known]},
                   "parcel_building_counts": {gis["A2"]: 1, known["A2"]: 1}}
        (self.directory / "ledger_identity_context.json").write_text(json.dumps(context), encoding="utf-8")
        self.config = {"data_dir": "data/regions_400/test", "center_lonlat": [126.9266, 37.526]}
        self.title_path = self.root / "data/raw/building_hub/11560_11000_202608.json"
        self.title_path.parent.mkdir(parents=True)

    def write_title(self, height):
        title = {**self.title, "HG": height}
        self.title_path.write_text(json.dumps({"Description": FIELDS, "Data": [title]}), encoding="utf-8")
        acquisition = {"files": {self.title_path.name: {
            "file": self.title_path.name, "sha256": sha256(self.title_path), "source_url": SOURCE_URL,
            "source_month": "2026-08", "sigungu_cd": "11560", "bjdong_cd": "11000",
            "ui_search_total": 1, "export_row_count": 1, "full_export_count_verified": True,
        }}}
        (self.title_path.parent / "acquisition.json").write_text(json.dumps(acquisition), encoding="utf-8")

    def test_absent_file_is_pending_then_verified_height_keeps_gis_original_unchanged(self):
        original = (self.directory / "buildings.gpkg").read_bytes()
        pending = reconcile_region("test", self.config, root=self.root)
        self.assertFalse(pending["lookup_complete"])
        self.assertEqual(pending["counts"], {"pending_lookup": 1})
        self.write_title(37.25)
        audited = reconcile_region("test", self.config, root=self.root)
        self.assertEqual(audited["counts"], {"verified_height": 1})
        self.assertNotIn("known", audited["buildings"])
        self.assertEqual(audited["buildings"]["gis-1"]["height_m"], 37.25)
        self.assertEqual((self.directory / "buildings.gpkg").read_bytes(), original)

    def test_verified_same_building_zero_height_is_not_transferred_as_zero_metres(self):
        self.write_title(0)
        audited = reconcile_region("test", self.config, root=self.root)
        self.assertEqual(audited["counts"], {"title_height_missing": 1})
        self.assertEqual(len((self.directory / "verified_matches.csv").read_text(encoding="utf-8-sig").splitlines()), 1)

    def test_partial_export_cannot_count_as_a_completed_ledger_lookup(self):
        self.write_title(37.25)
        acquisition_path = self.title_path.parent / "acquisition.json"
        acquisition = json.loads(acquisition_path.read_text(encoding="utf-8"))
        acquisition["files"][self.title_path.name]["ui_search_total"] = 2
        acquisition_path.write_text(json.dumps(acquisition), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "full-export"):
            reconcile_region("test", self.config, root=self.root)


if __name__ == "__main__":
    unittest.main()
