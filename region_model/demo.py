"""One deterministic 1 km synthetic scene, deliberately not a real neighbourhood.

Both requested map sizes clip this same scene; no geometry is scaled to fit.
"""
import geopandas as gpd
from shapely.affinity import translate
from shapely.geometry import Polygon, MultiPolygon, LineString, box

from .core import METRIC_CRS, LedgerResolver


def make_demo(origin):
    """Return the same synthetic buildings and background at the given origin."""
    shapes, heights = [], []
    for r in range(4):
        for c in range(5):
            x, y = -170 + c*72, -160+r*85
            shapes.append(box(x, y, x+35+(c%2)*12, y+42))
            heights.append(float(12 + ((r*7+c*13) % 100)))
    shapes[0] = Polygon([(-170,-160),(-112,-160),(-112,-143),(-144,-143),(-144,-118),(-170,-118)])
    shapes[1] = Polygon([(-98,-160),(-41,-160),(-41,-105),(-98,-105)],
                        [[(-84,-146),(-84,-122),(-56,-122),(-56,-146)]])
    shapes[2] = MultiPolygon([box(-26,-160,-10,-118), box(-5,-160,13,-118)])
    heights[3] = None  # A verified title-record height can fill this one only.
    heights[8] = None  # Address-only pseudo match must be rejected.
    heights[14] = 0    # Missing heights remain visible in quality reports.
    heights[18] = None

    background_shapes = [LineString([(-500,-95),(500,-95)]),
        LineString([(30,-500),(30,500)]), LineString([(-500,75),(500,75)]), box(190,110,310,230)]
    # Keep the original central 20 cases unchanged. Extend the SAME scene into
    # the surrounding bands and corners, leaving road and park clearances.
    central_fixture_area = box(-205, -205, 205, 205)
    excluded_background = [g.buffer(6) for g in background_shapes[:3]] + [background_shapes[3].buffer(8)]
    for r, y in enumerate((-460, -360, -260, -160, -60, 140, 240, 340, 430)):
        for c, x in enumerate((-465, -365, -265, -165, -65, 65, 165, 265, 365, 435)):
            footprint = box(x, y, x + 28 + ((r + 2*c) % 4)*7, y + 30 + ((2*r + c) % 4)*6)
            if footprint.intersects(central_fixture_area):
                continue
            if any(footprint.intersects(g) for g in excluded_background):
                continue
            if any(footprint.intersects(g) for g in shapes):
                continue
            shapes.append(footprint)
            heights.append(float(14 + ((r*19 + c*23) % 112)))

    names = [f"합성 업무동 {i+1:03d}" for i in range(len(shapes))]
    names[0], names[1], names[2] = "합성 꺾임동 001", "합성 중정동 002", "합성 복합동 003"
    buildings = gpd.GeoDataFrame({"gis_id": [f"DEMO-{i:02d}" for i in range(len(shapes))],
                                 "gis_height": heights, "gis_name": names},
                                geometry=[translate(g, *origin) for g in shapes], crs=METRIC_CRS)
    background = gpd.GeoDataFrame({"kind": ["road", "road", "road", "park"], "osm_id": ["synthetic"]*4},
        geometry=[translate(g, *origin) for g in background_shapes], crs=METRIC_CRS)
    schema = {"id_field": "gis_id", "height_field": "gis_height", "source_name": "SYNTHETIC TEST FIXTURE",
              "source_url": "not-real-data", "snapshot_date": "synthetic", "height_unit": "m", "height_semantics": "above_ground",
              "name_field": "gis_name", "name_source": "synthetic"}
    ledger = [{"ledger_id": "TEST-TITLE-1", "height_m": "47.5", "record_type": "title", "namespace": "DEMO"}]
    matches = [{"building_id": "DEMO-03", "ledger_id": "TEST-TITLE-1", "namespace": "DEMO", "verified": "true",
                "identity_method": "official_crosswalk", "identity_evidence": "Synthetic crosswalk for software verification only"},
               {"building_id": "DEMO-08", "ledger_id": "TEST-TITLE-2", "namespace": "DEMO", "verified": "true",
                "identity_method": "address_similarity", "identity_evidence": "Intentionally rejected test case"}]
    return buildings, background, schema, LedgerResolver(ledger, matches)
