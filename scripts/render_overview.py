"""Render a reproducible plan view of the verified actual Gangnam 400 m bundle.

This is an offline SVG renderer. It reads the curated source files, verifies
their provenance and uses the same building height/geometry QA as the model.
It never substitutes synthetic fixtures or infers building heights/road widths.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from region_model.__main__ import bind_schema_provenance, read_verified_provenance
from region_model.background import load_background
from region_model.core import METRIC_CRS, localize, prepare_buildings, read_buildings, read_json, region_geometry
from region_model.preview import SOURCE_COLORS, is_tunnel_background

NS = "http://www.w3.org/2000/svg"
CENTER = [127.0276, 37.4979]
SIZE = 400
ET.register_namespace("", NS)


def _element(parent, tag, attributes=None, text=None):
    result = ET.SubElement(parent, f"{{{NS}}}{tag}", {str(k): str(v) for k, v in (attributes or {}).items()})
    if text is not None:
        result.text = str(text)
    return result


def _text(parent, x, y, text, size=14, **attributes):
    return _element(parent, "text", {"x": x, "y": y, "font-size": size, **attributes}, text)


def _parts(geometry, kind):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == kind:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _parts(part, kind)


def render_overview(data_dir: Path, output: Path) -> dict:
    origin, aoi = region_geometry(*CENTER, SIZE)
    buildings_path, background_path = data_dir / "buildings.gpkg", data_dir / "background.gpkg"
    provenance = read_verified_provenance(data_dir / "provenance.json", CENTER, SIZE, buildings_path, background_path)
    schema = bind_schema_provenance(read_json(data_dir / "schema.json"), provenance)
    source, _ = read_buildings(buildings_path, schema, aoi)
    buildings, quality = prepare_buildings(source, schema, aoi)
    if buildings.empty:
        raise ValueError("The verified actual-data bundle contains no buildings in the AOI.")
    background, background_meta = load_background(background_path, METRIC_CRS, aoi)
    if background_meta["status"] != "ok":
        raise ValueError(f"Could not load the verified background: {background_meta['status']}")
    buildings = localize(buildings, origin)
    background = localize(background, origin)
    underground = background.apply(is_tunnel_background, axis=1)
    surface = background.loc[~underground].copy()
    height_counts = {source: int(buildings.height_source.eq(source).sum()) for source in SOURCE_COLORS}
    counts = {"buildings": len(buildings), "known_height": height_counts["gis"] + height_counts["ledger"],
              "missing_height": height_counts["missing"], "height_sources": height_counts,
              "background_total": len(background), "background_tunnels_omitted_from_surface": int(underground.sum()),
              "surface_road_centerlines": int(surface.kind.eq("road").sum())}
    snapshot_date = str(provenance["buildings"]["snapshot_date"])
    record_dates = sorted(schema.get("record_reference_dates", []))
    osm_stamps = provenance["background"].get("osm_database_timestamps_utc", [])
    osm_date = ", ".join(osm_stamps) if osm_stamps else "not recorded"

    root = ET.Element(f"{{{NS}}}svg", {"width": "1160", "height": "960", "viewBox": "0 0 1160 960",
        "role": "img", "aria-labelledby": "overview-title overview-description",
        "font-family": "Arial, Helvetica, Malgun Gothic, sans-serif", "fill": "#243447"})
    _element(root, "title", {"id": "overview-title"}, "Gangnam Station: actual 400 m square building and road overview")
    _element(root, "desc", {"id": "overview-description"},
        f"Actual MOLIT GIS Building Integration footprints and OpenStreetMap background. {len(buildings)} buildings, "
        f"{counts['known_height']} with source-reported positive heights and {counts['missing_height']} without usable heights. "
        "North is up; x and y are local metres. Orange footprints retain buildings with unknown height. "
        "Building names and explicitly tagged tunnels are not drawn. Road marks are centerlines, not road surfaces.")
    _element(root, "rect", {"width": 1160, "height": 960, "fill": "white"})
    _text(root, 40, 47, "Gangnam Station · 400 m × 400 m", 30, **{"font-weight": 700})
    _text(root, 40, 80, "Official building footprints and source heights · OpenStreetMap road centerlines", 16, fill="#607184")
    _element(root, "rect", {"x": 965, "y": 28, "width": 153, "height": 31, "rx": 7,
                           "fill": "#e9f4ef", "stroke": "#b5d6c6"})
    _text(root, 1041.5, 49, "ACTUAL DATA", 13, **{"text-anchor": "middle", "font-weight": 700, "fill": "#2d6850"})

    left, top, side, half = 82, 160, 640, SIZE / 2

    def point(x, y):
        return left + (x + half) * side / SIZE, top + (half - y) * side / SIZE

    def polygon_path(geometry):
        pieces = []
        for polygon in _parts(geometry, "Polygon"):
            for ring in (polygon.exterior, *polygon.interiors):
                coordinates = [point(x, y) for x, y, *_ in ring.coords]
                pieces.append("M " + " L ".join(f"{x:.5f},{y:.5f}" for x, y in coordinates) + " Z")
        return " ".join(pieces)

    plot = _element(root, "g", {"id": "actual-400m-map", "data-local-bounds-m": "-200,-200,200,200"})
    _element(plot, "rect", {"x": left, "y": top, "width": side, "height": side,
                           "fill": "#f8fafb", "stroke": "#b8c6d3"})
    for value in (-100, 0, 100):
        x, y = point(value, value)
        _element(plot, "line", {"x1": x, "y1": top, "x2": x, "y2": top+side, "stroke": "#e8edf1"})
        _element(plot, "line", {"x1": left, "y1": y, "x2": left+side, "y2": y, "stroke": "#e8edf1"})
    background_group = _element(plot, "g", {"id": "surface-background", "data-omitted-tunnels": int(underground.sum())})
    background_colors = {"road": "#a8b8c8", "park": "#e2eddf", "landuse": "#edf0f3", "water": "#c2e0eb"}
    for kind in ("landuse", "park", "water", "road"):
        for row in surface.loc[surface.kind.eq(kind)].itertuples():
            path = polygon_path(row.geometry)
            attributes = {"data-osm-id": row.osm_id, "data-kind": kind}
            if path:
                _element(background_group, "path", {"d": path, "fill-rule": "evenodd", "fill": background_colors[kind], **attributes})
            for line in _parts(row.geometry, "LineString"):
                coordinates = [point(x, y) for x, y, *_ in line.coords]
                _element(background_group, "polyline", {"points": " ".join(f"{x:.5f},{y:.5f}" for x, y in coordinates),
                    "fill": "none", "stroke": background_colors[kind], "stroke-width": 2.1,
                    "stroke-linecap": "round", **attributes})

    building_group = _element(plot, "g", {"id": "official-building-footprints", "data-building-count": len(buildings)})
    for row in buildings.itertuples():
        shape = _element(building_group, "path", {"d": polygon_path(row.geometry), "fill-rule": "evenodd",
            "fill": SOURCE_COLORS[row.height_source], "stroke": "#b27020" if row.height_source == "missing" else "#526b83",
            "stroke-width": 1.0 if row.height_source == "missing" else 0.65,
            "data-building-id": row.building_id, "data-height-source": row.height_source,
            "data-interior-rings": sum(len(p.interiors) for p in _parts(row.geometry, "Polygon"))})
        height = "unknown height" if row.height_source == "missing" else f"{row.height_m:g} m ({row.height_source})"
        _element(shape, "title", text=f"Building {row.building_id}: {height}")
    cx, cy = point(0, 0)
    _element(plot, "circle", {"id": "selected-center", "cx": cx, "cy": cy, "r": 5,
                              "fill": "#ca5f55", "stroke": "white", "stroke-width": 2})
    _element(plot, "rect", {"x": left, "y": top, "width": side, "height": side,
                           "fill": "none", "stroke": "#9dabb9", "stroke-width": 1})
    for value in (-200, -100, 0, 100, 200):
        x, y = point(value, value)
        tick = f"{value:+g}" if value else "0"
        _text(root, x, top+side+24, tick, 13, **{"text-anchor": "middle", "fill": "#607184"})
        _text(root, left-12, y+4, tick, 13, **{"text-anchor": "end", "fill": "#607184"})
    _text(root, left+side/2, 850, "East x (m)", 14, **{"text-anchor": "middle", "fill": "#607184"})
    _text(root, 27, top+side/2, "North y (m)", 14,
          **{"text-anchor": "middle", "transform": f"rotate(-90 27 {top+side/2})", "fill": "#607184"})
    _element(root, "line", {"x1": 700, "y1": 141, "x2": 700, "y2": 119, "stroke": "#344e66", "stroke-width": 2})
    _element(root, "path", {"d": "M 695,125 L 700,115 L 705,125 Z", "fill": "#344e66"})
    _text(root, 700, 108, "N", 13, **{"text-anchor": "middle", "font-weight": 700})
    # True map scale: 100 metres occupies exactly 100 * side / SIZE pixels.
    scale_length = 100 * side / SIZE
    _element(root, "path", {"id": "scale-bar-100m", "d": f"M {left},877 V 885 H {left+scale_length} V 877",
                           "fill": "none", "stroke": "#344e66", "stroke-width": 2, "data-length-m": 100})
    _text(root, left+scale_length/2, 873, "100 m", 12, **{"text-anchor": "middle", "fill": "#607184"})
    _text(root, 385, 884, "Flat ground · z = 0 m", 13, fill="#607184")

    _element(root, "rect", {"x": 765, "y": 160, "width": 353, "height": 383, "rx": 11,
                           "fill": "#fbfcfd", "stroke": "#dfe6ed"})
    _text(root, 785, 192, "Footprint and height QA", 19, **{"font-weight": 700})
    _text(root, 785, 251, len(buildings), 43, **{"font-weight": 700})
    _text(root, 785, 280, "official building footprints", 15, fill="#607184")
    for y, source, label in ((322, "gis", "GIS height available"), (361, "ledger", "Verified ledger height"),
                              (400, "missing", "Missing / invalid height")):
        _element(root, "rect", {"x": 787, "y": y-13, "width": 14, "height": 14, "rx": 2,
                               "fill": SOURCE_COLORS[source]})
        _text(root, 812, y, label, 14)
        _text(root, 1095, y, height_counts[source], 17, **{"text-anchor": "end", "font-weight": 700})
    _text(root, 785, 443, "Unknown heights keep their footprints.", 13, fill="#68798b")
    _text(root, 785, 464, "No floor-count or address-based estimate.", 13, fill="#68798b")
    _element(root, "line", {"x1": 787, "y1": 495, "x2": 817, "y2": 495, "stroke": "#a8b8c8", "stroke-width": 2.1})
    _text(root, 827, 500, "OSM road centerline", 13)
    _element(root, "circle", {"cx": 793, "cy": 522, "r": 4, "fill": "#ca5f55"})
    _text(root, 812, 527, "Selected map center", 13)

    _text(root, 785, 584, "Sources and extent", 19, **{"font-weight": 700})
    source_link = _element(root, "a", {"href": provenance["buildings"]["source_url"]})
    _text(source_link, 785, 615, "Buildings: MOLIT / VWorld AL_D010", 14, fill="#315f82")
    _text(root, 785, 639, f"건물 배포본 기준일: {snapshot_date}", 13, fill="#607184")
    if record_dates:
        date_label = record_dates[0] if len(record_dates) == 1 else f"{record_dates[0]} – {record_dates[-1]}"
        _text(root, 785, 660, f"원본 속성 기준일(A22): {date_label}", 12, fill="#607184")
    osm_link = _element(root, "a", {"href": "https://www.openstreetmap.org/copyright"})
    _text(osm_link, 785, 677, "Background: OpenStreetMap", 14, fill="#315f82")
    _text(root, 785, 701, "OSM database timestamp (UTC):", 13, fill="#607184")
    _text(root, 785, 723, osm_date, 13, fill="#607184")
    _text(root, 785, 757, f"{counts['surface_road_centerlines']} surface road centerlines shown", 13)
    _text(root, 785, 780, f"{int(underground.sum())} tagged tunnels omitted from view", 13)
    _text(root, 785, 817, "Center (longitude, latitude)", 13, fill="#607184")
    _text(root, 785, 841, f"{CENTER[0]:.4f}, {CENTER[1]:.4f}", 17)
    _text(root, 785, 874, "Area: 160,000 m² · local units: metres", 13, fill="#607184")

    _text(root, 40, 917, "Buildings: MOLIT GIS Building Integration, provided by VWorld. Footprints projected and clipped to the 400 m AOI.",
          12, fill="#607184")
    osm_credit = _element(root, "a", {"href": "https://www.openstreetmap.org/copyright"})
    _text(osm_credit, 40, 941, "© OpenStreetMap contributors", 12, fill="#315f82")
    osm_license = _element(root, "a", {"href": provenance["background"]["license_uri"]})
    _text(osm_license, 224, 941, "ODbL 1.0", 12, fill="#315f82")
    _text(root, 386, 941, "Local coordinates = EPSG:5179 coordinates minus the selected center.", 12, fill="#607184")
    figure_metadata = {"dataset_id": provenance["dataset_id"], "is_synthetic": False,
        "pipeline": "verified provenance -> core.read_buildings -> core.prepare_buildings -> core.localize",
        "center_lonlat": CENTER, "size_m": SIZE, "local_bounds_m": [-200, -200, 200, 200],
        "origin_epsg5179_m": list(origin), "source_crs": METRIC_CRS, "local_crs": None,
        "buildings_file_sha256": provenance["buildings"]["sha256"],
        "background_file_sha256": provenance["background"]["sha256"],
        "building_snapshot_date": snapshot_date, "osm_database_timestamps_utc": osm_stamps,
        "building_record_reference_dates": record_dates,
        "counts": counts, "names_displayed": False, "road_widths_inferred": False,
        "quality_record_count": len(quality), "underground_rule": "same is_tunnel_background predicate as interactive preview"}
    _element(root, "metadata", text=json.dumps(figure_metadata, ensure_ascii=False, sort_keys=True))
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)

    # Verify the generated file represents all retained buildings and only the
    # intended surface features, without relying solely on in-memory counters.
    saved = ET.parse(output)
    building_paths = saved.findall(f".//{{{NS}}}g[@id='official-building-footprints']/{{{NS}}}path")
    assert len(building_paths) == len(buildings)
    assert Counter(path.get("data-height-source") for path in building_paths) == Counter(buildings.height_source)
    assert all(path.get("fill-rule") == "evenodd" and path.get("d") for path in building_paths)
    surface_elements = saved.findall(f".//{{{NS}}}g[@id='surface-background']/*")
    omitted_ids = set(background.loc[underground, "osm_id"])
    assert not any(element.get("data-osm-id") in omitted_ids for element in surface_elements)
    assert saved.find(f".//{{{NS}}}circle[@id='selected-center']").get("cx") == str(cx)
    return {"output": output.as_posix(), "counts": counts, "validation": "passed"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data" / "gangnam_400")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "assets" / "gangnam-400m.svg")
    args = parser.parse_args(argv)
    try:
        result = render_overview(args.data_dir, args.out)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"Overview failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
