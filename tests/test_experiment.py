"""Experimental assumptions must remain separate from observed GIS heights."""

import math
import unittest

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, Polygon, box

from region_model.core import METRIC_CRS
from region_model.experiment import apply_height_policy, derive_no_fly_zones


def sample_buildings(heights, sources=None):
    sources = sources or ["missing" if height is None else "gis" for height in heights]
    return gpd.GeoDataFrame(
        {
            "building_id": [f"building-{index:03}" for index in range(len(heights))],
            "source_building_id": [f"building-{index:03}" for index in range(len(heights))],
            "height_m": heights,
            "height_source": sources,
            "quality_flags": ["unresolved_height;no_verified_match" if source == "missing" else "" for source in sources],
        },
        geometry=[box(index * 20, 0, index * 20 + 10, 10) for index in range(len(heights))],
        crs=METRIC_CRS,
    )


def completed_audit(frame):
    return {
        row.building_id: {"status": "title_height_missing"}
        for _, row in frame.loc[frame.height_source.eq("missing")].iterrows()
    }


def quality_rows(frame):
    return [
        {"building_id": row.building_id, "height_m": row.height_m,
         "height_source": row.height_source, "quality_flags": row.quality_flags, "included": True}
        for _, row in frame.iterrows()
    ]


class HeightPolicyTests(unittest.TestCase):
    def apply(self, frame, *, seed=731, ledger_audit=None):
        return apply_height_policy(frame, quality_rows(frame), seed=seed,
                                   ledger_audit=completed_audit(frame) if ledger_audit is None else ledger_audit)

    def test_mean_preserved_with_even_and_odd_missing_count(self):
        for missing_count in (1, 2, 3, 8, 11):
            with self.subTest(missing_count=missing_count):
                confirmed = [35.0, 70.0, 22.0] * 4
                frame = sample_buildings(confirmed + [None] * missing_count)
                result, _, policy = self.apply(frame)
                expected_mean = math.fsum(confirmed) / len(confirmed)
                estimated = result.loc[result.height_source.eq("imputed")]
                self.assertEqual(policy["status"], "ready")
                self.assertEqual(len(estimated), missing_count)
                self.assertAlmostEqual(math.fsum(estimated.imputation_delta_m), 0.0, places=12)
                self.assertAlmostEqual(math.fsum(estimated.height_m) / missing_count, expected_mean, places=12)
                self.assertTrue(estimated.height_m.between(expected_mean - 5, expected_mean + 5).all())
                self.assertTrue(estimated.observed_height_m.isna().all())
                self.assertTrue(estimated.height_source_original.eq("missing").all())
                self.assertEqual(policy["known_count"], len(confirmed))
                self.assertEqual(policy["imputed_count"], missing_count)

    def test_seed_is_repeatable_and_row_order_does_not_change_assignment(self):
        frame = sample_buildings([20.0, 40.0, 60.0, 80.0, None, None, None, None])
        result, _, _ = self.apply(frame)
        same, _, _ = self.apply(frame)
        reordered, _, _ = self.apply(frame.iloc[[5, 7, 0, 3, 4, 1, 2, 6]].reset_index(drop=True))
        changed, _, _ = self.apply(frame, seed=732)
        self.assertTrue(result.equals(same))
        actual_by_id = result.set_index("building_id").height_m.sort_index()
        pd.testing.assert_series_equal(actual_by_id, reordered.set_index("building_id").height_m.sort_index())
        self.assertFalse(result.height_m.equals(changed.height_m))

    def test_original_gis_ledger_geometry_and_quality_inputs_are_unchanged(self):
        frame = sample_buildings([33.75, 66.25, None, None], ["gis", "ledger", "missing", "missing"])
        before = frame.copy(deep=True)
        quality = quality_rows(frame) + [{"building_id": "outside", "quality_flags": "empty_geometry", "included": False}]
        original_quality = [dict(row) for row in quality]
        result, updated, policy = apply_height_policy(frame, quality, seed=17, ledger_audit=completed_audit(frame))
        self.assertTrue(frame.equals(before))
        self.assertEqual(quality, original_quality)
        self.assertTrue(result.geometry.equals(frame.geometry))
        self.assertEqual(result.loc[:1, "height_m"].tolist(), [33.75, 66.25])
        self.assertEqual(result.loc[:1, "height_source"].tolist(), ["gis", "ledger"])
        self.assertEqual(result.loc[:1, "observed_height_m"].tolist(), [33.75, 66.25])
        self.assertEqual(policy["known_count"], 2, "Verified ledger heights count toward readiness and mean.")
        self.assertEqual(policy["mean_known_height_m"], 50.0)
        self.assertEqual(updated[-1], quality[-1])
        self.assertEqual(updated[2]["height_source"], "imputed")
        self.assertTrue(pd.isna(updated[2]["observed_height_m"]))

    def test_more_missing_than_gis_plus_ledger_is_held_after_completed_lookup(self):
        frame = sample_buildings([10.0, 50.0, None, None, None], ["gis", "ledger", "missing", "missing", "missing"])
        result, _, policy = self.apply(frame)
        self.assertEqual(policy["status"], "held")
        self.assertEqual(policy["known_count"], 2)
        self.assertEqual(policy["missing_count_before"], 3)
        self.assertEqual(policy["imputed_count"], 0)
        self.assertEqual(int(result.height_m.isna().sum()), 3)

    def test_equal_missing_and_confirmed_count_is_allowed(self):
        frame = sample_buildings([10.0, 50.0, None, None], ["gis", "ledger", "missing", "missing"])
        result, _, policy = self.apply(frame)
        self.assertEqual(policy["status"], "ready")
        self.assertEqual(result.height_source.tolist(), ["gis", "ledger", "imputed", "imputed"])
        self.assertEqual(policy["mean_known_height_m"], 30.0)

    def test_pending_failed_and_absent_ledger_checks_do_not_allow_imputation(self):
        frame = sample_buildings([30.0, 50.0, None])
        missing_id = frame.iloc[-1].building_id
        for status in (None, "pending", "authentication_required", "network_error", "request_failed"):
            with self.subTest(status=status):
                audit = {} if status is None else {missing_id: {"status": status}}
                result, _, policy = self.apply(frame, ledger_audit=audit)
                self.assertEqual(policy["status"], "pending_ledger")
                self.assertEqual(policy["ledger_pending_count"], 1)
                self.assertEqual(policy["imputed_count"], 0)
                self.assertEqual(policy["pending_building_ids"], [missing_id])
                self.assertTrue(pd.isna(result.iloc[-1].height_m))
                self.assertEqual(result.iloc[-1].height_source, "missing")

    def test_no_confirmed_height_is_held_and_no_mean_is_invented(self):
        frame = sample_buildings([None, None])
        result, _, policy = self.apply(frame)
        self.assertEqual(policy["status"], "held")
        self.assertIsNone(policy["mean_known_height_m"])
        self.assertEqual(policy["imputed_count"], 0)
        self.assertTrue(result.height_m.isna().all())

    def test_small_positive_mean_never_creates_negative_height(self):
        frame = sample_buildings([0.25, 0.25, None, None])
        result, _, policy = self.apply(frame)
        self.assertTrue(result.height_m.gt(0).all())
        self.assertEqual(policy["mean_imputed_height_m"], 0.25)
        self.assertLess(policy["effective_deviation_limit_m"], 0.25)

    def test_imputed_values_cannot_be_reused_as_observed_input(self):
        original = sample_buildings([30.0, 50.0, None, None])
        once, _, _ = self.apply(original)
        with self.assertRaisesRegex(ValueError, "이미 추정한 높이"):
            self.apply(once)

    def test_height_source_must_agree_with_available_numeric_value(self):
        for height, source in ((99.0, "missing"), (None, "gis"), (0.0, "ledger"), (float("inf"), "gis")):
            with self.subTest(height=height, source=source), self.assertRaises(ValueError):
                self.apply(sample_buildings([height], [source]))

    def test_verified_ledger_height_must_be_connected_before_imputation(self):
        frame = sample_buildings([30.0, 50.0, None])
        audit = {"building-002": {"status": "verified_height", "height_m": 75.0}}
        with self.assertRaisesRegex(ValueError, "연결한 뒤"):
            self.apply(frame, ledger_audit=audit)


class FlightZoneTests(unittest.TestCase):
    def test_threshold_includes_exact_50_and_estimates_are_flagged(self):
        frame = sample_buildings([49.999, 50.0, 90.0, 51.0, None], ["gis", "gis", "ledger", "imputed", "missing"])
        original = frame.copy(deep=True)
        zones = derive_no_fly_zones(frame, box(-10, -10, 200, 30), safety_margin_m=5)
        self.assertEqual(zones.building_id.tolist(), ["building-001", "building-002", "building-003"])
        self.assertEqual(zones.is_estimated.tolist(), [False, False, True])
        self.assertTrue(zones.threshold_m.eq(50).all())
        self.assertTrue(zones.flight_altitude_m.eq(50).all())
        self.assertTrue(zones.safety_margin_m.eq(5).all())
        self.assertTrue(frame.equals(original), "Flight constraints must not edit footprints or heights.")
        self.assertEqual(tuple(zones.iloc[0].geometry.bounds), (15.0, -5.0, 35.0, 15.0))

    def test_buffer_uses_full_footprint_before_study_boundary_clip(self):
        aoi = box(0, 0, 100, 100)
        full_footprint = Polygon([(95, 20), (120, 20), (120, 95), (102, 95), (102, 40), (95, 40)])
        frame = sample_buildings([60.0])
        frame.geometry = [full_footprint.intersection(aoi)]
        original = frame.copy(deep=True)
        zones = derive_no_fly_zones(frame, aoi, safety_margin_m=5,
                                    original_footprints={"building-000": full_footprint})
        expected = full_footprint.buffer(5).intersection(aoi)
        self.assertLess(zones.iloc[0].geometry.symmetric_difference(expected).area, 1e-10)
        self.assertTrue(zones.iloc[0].geometry.covers(Point(99, 80)))
        self.assertFalse(frame.iloc[0].geometry.buffer(5).intersection(aoi).covers(Point(99, 80)),
                         "Fixture must distinguish buffer-then-clip from clip-then-buffer.")
        self.assertTrue(aoi.covers(zones.iloc[0].geometry))
        self.assertTrue(frame.equals(original))

    def test_zero_margin_preserves_polygon_and_courtyard(self):
        footprint = Polygon([(0, 0), (30, 0), (30, 30), (0, 30)], holes=[[(10, 10), (10, 20), (20, 20), (20, 10)]])
        frame = sample_buildings([70.0])
        frame.geometry = [footprint]
        zones = derive_no_fly_zones(frame, box(-10, -10, 40, 40), safety_margin_m=0)
        self.assertTrue(zones.iloc[0].geometry.equals(footprint))
        self.assertFalse(zones.iloc[0].geometry.covers(Point(15, 15)))

    def test_invalid_policy_parameters_are_rejected(self):
        frame = sample_buildings([55.0])
        aoi = box(-10, -10, 40, 40)
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(altitude=value), self.assertRaises(ValueError):
                derive_no_fly_zones(frame, aoi, flight_altitude_m=value)
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(margin=value), self.assertRaises(ValueError):
                derive_no_fly_zones(frame, aoi, safety_margin_m=value)

    def test_unknown_height_source_cannot_create_a_flight_constraint(self):
        frame = sample_buildings([55.0], ["unknown_web_estimate"])
        with self.assertRaises(ValueError):
            derive_no_fly_zones(frame, box(-10, -10, 40, 40))


if __name__ == "__main__":
    unittest.main()
