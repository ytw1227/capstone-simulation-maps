"""Check geometry fidelity that matters for later obstruction calculations."""

from collections import Counter
from pathlib import Path
import tempfile
import unittest

import geopandas as gpd
from shapely.geometry import MultiPolygon, Polygon

from region_model.preview import _background_traces, export_obj, polygon_mesh


class MeshTests(unittest.TestCase):
    def test_actual_banpo_stream_is_not_rendered_as_surface_water(self):
        path = Path(__file__).resolve().parents[1] / "data/gangnam_400/background.gpkg"
        background = gpd.read_file(path, layer="background")
        water = background.loc[background.kind.eq("water")].copy()
        before = water.copy()
        self.assertEqual(len(water), 1)
        self.assertEqual(water.iloc[0]["tunnel"], "yes")
        self.assertEqual(_background_traces(water), [])
        self.assertTrue(water.equals(before), "Display filtering must preserve the original source data.")

    def assert_valid_extrusion(self, polygon, height):
        vertices, faces = polygon_mesh(polygon, height)
        roof_area = 0.0
        signed_volume = 0.0
        edges = Counter()
        for a, b, c in faces:
            for first, second in ((a, b), (b, c), (c, a)):
                edges[tuple(sorted((first, second)))] += 1
            points = [vertices[index] for index in (a, b, c)]
            if all(point[2] == height for point in points):
                triangle = Polygon([(point[0], point[1]) for point in points])
                self.assertTrue(polygon.covers(triangle), "A roof triangle crossed a boundary or courtyard.")
                roof_area += triangle.area
            ax, ay, az = points[0]
            bx, by, bz = points[1]
            cx, cy, cz = points[2]
            signed_volume += (
                ax * (by * cz - bz * cy)
                + ay * (bz * cx - bx * cz)
                + az * (bx * cy - by * cx)
            ) / 6.0
        self.assertAlmostEqual(roof_area, polygon.area)
        self.assertAlmostEqual(signed_volume, polygon.area * height)
        self.assertTrue(all(count == 2 for count in edges.values()), "Mesh must be a closed two-manifold.")
        self.assertEqual({point[2] for point in vertices}, {0.0, height})

    def test_concave_footprint_is_not_convex_hull(self):
        polygon = Polygon([(0, 0), (30, 0), (30, 10), (10, 10), (10, 30), (0, 30)])
        self.assert_valid_extrusion(polygon, 20.0)

    def test_courtyard_hole_is_preserved(self):
        polygon = Polygon(
            [(0, 0), (40, 0), (40, 40), (0, 40)],
            holes=[[(10, 10), (10, 30), (30, 30), (30, 10)]],
        )
        self.assert_valid_extrusion(polygon, 12.0)
        self.assert_valid_extrusion(polygon.reverse(), 12.0)

    def test_nonpositive_or_nonfinite_height_cannot_create_mesh(self):
        polygon = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        for height in (0, -3, float("nan"), float("inf")):
            with self.subTest(height=height), self.assertRaises(ValueError):
                polygon_mesh(polygon, height)

    def test_obj_excludes_unresolved_heights_and_exports_all_parts(self):
        first = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])
        second = Polygon([(4, 0), (6, 0), (6, 2), (4, 2)])
        unknown = Polygon([(20, 20), (22, 20), (22, 22), (20, 22)])
        buildings = gpd.GeoDataFrame(
            {
                "building_id": ["known", "unknown", "unverified"],
                "height_m": [10.0, None, 99.0],
                "height_source": ["gis", "missing", "missing"],
            },
            geometry=[MultiPolygon([first, second]), unknown, unknown],
        )
        buildings.attrs["is_demo"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.obj"
            export_obj(buildings, path)
            content = path.read_text(encoding="utf-8")
        self.assertIn("SYNTHETIC DEMO", content)
        self.assertIn("2 buildings have unresolved height", content)
        self.assertIn("known_part_1", content)
        self.assertIn("known_part_2", content)
        self.assertNotIn("unknown_part", content)
        self.assertNotIn("unverified_part", content)
        positions = [line.split()[1:] for line in content.splitlines() if line.startswith("v ")]
        self.assertEqual({float(position[2]) for position in positions}, {0.0, 10.0})
        self.assertTrue(all(float(position[0]) < 20 for position in positions))


if __name__ == "__main__":
    unittest.main()
