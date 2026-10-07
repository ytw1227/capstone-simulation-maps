"""Extract five actual 400 m building subsets from user-obtained AL_D010 ZIPs.

The original A16 height and full intersecting footprints are preserved. Height
imputation and flight-exclusion generation belong to the downstream model.
Existing artifacts are never overwritten; OSM files in each directory are left
alone. Run from the repository: python scripts/import_five_regions.py.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd
import pandas as pd
import pyogrio
from pyproj import CRS
from shapely import make_valid

from region_model.core import METRIC_CRS, read_buildings, region_geometry, valid_height
from scripts.import_official_gangnam import (
    CATALOG_URL, SELECTED_FIELDS, SOURCE_NAME, SOURCE_URL,
    _id_key, _prj_crs, _same_crs, _sources, _write_json, sha256,
)

SIZE = 400
OWN_FILES = ("buildings.gpkg", "schema.json", "buildings.provenance.json",
             "import_quality.json", "LICENSE-BUILDINGS.md")


def import_region(key, config, selection_source, *, root=ROOT):
    """Preserve official fields and geometry for this explicitly configured AOI."""
    root = Path(root)
    source = root / config["source_archive"]
    output = root / config["data_dir"]
    center = config["center_lonlat"]
    selection_source = config.get("selection_source", selection_source)
    snapshot_date = config["source_snapshot_date"]
    snapshot = date.fromisoformat(snapshot_date)
    if not source.is_file():
        raise ValueError(f"공식 원본 파일이 없습니다: {source.name}")
    existing = [name for name in OWN_FILES if (output / name).exists()]
    if existing:
        raise ValueError(f"기존 {key} 건물 결과를 덮어쓰지 않습니다: {existing}")
    if snapshot >= date(2023, 8, 8):
        expected_crs = CRS.from_epsg(5186)
    elif snapshot <= date(2023, 8, 5):
        expected_crs = CRS.from_epsg(5174)
    else:
        raise ValueError("전환일 자료의 CRS를 별도로 확인해야 합니다.")
    origin, aoi = region_geometry(*center, SIZE)
    frames, source_parts = [], []
    for read_path, prj_bytes, metadata in _sources(source):
        # AL_D010 has no .cpg; the supplied official archive uses CP949 DBF text.
        info = pyogrio.read_info(read_path, encoding="cp949")
        if not info.get("crs"):
            raise ValueError("원본 SHP에 CRS가 없습니다.")
        actual_crs = CRS.from_user_input(info["crs"])
        if not _same_crs(actual_crs, _prj_crs(prj_bytes)):
            raise ValueError("GDAL CRS와 원본 .prj가 다릅니다.")
        if not _same_crs(actual_crs, expected_crs):
            raise ValueError("자료 기준일의 공식 배포 CRS와 다릅니다.")
        fields = set(info["fields"])
        if not {"A1", "A16"}.issubset(fields):
            raise ValueError("AL_D010 필수 열 A1/A16이 없습니다.")
        schema = {
            "source_name": SOURCE_NAME, "source_url": SOURCE_URL,
            "snapshot_date": snapshot_date, "id_field": "A1",
            "name_field": "A24" if "A24" in fields else None,
            "height_field": "A16", "height_unit": "m",
            "height_semantics": "above_ground", "source_crs": actual_crs.to_string(),
            "encoding": "cp949",
        }
        frame, read_metadata = read_buildings(read_path, schema, aoi)
        selected_fields = [field for field in SELECTED_FIELDS if field in frame.columns]
        intersects = frame.geometry.map(lambda geometry: bool(
            geometry is not None and not geometry.is_empty
            and (geometry if geometry.is_valid else make_valid(geometry)).intersection(aoi).area > 0))
        frame = frame.loc[intersects, selected_fields + ["geometry"]].copy()
        if not frame.empty:
            if frame.A1.map(lambda value: value is not None and not pd.isna(value)
                            and not isinstance(value, str)).any():
                raise ValueError("A1 건물 ID는 문자열이어야 합니다.")
            frames.append(frame)
        source_parts.append({
            **metadata, "original_crs": actual_crs.to_string(),
            "original_crs_wkt": actual_crs.to_wkt(),
            "source_total_rows": int(info["features"]),
            "requested_encoding": "cp949", "driver_reported_encoding": info.get("encoding"),
            "bbox_filter_rows": read_metadata["bbox_filter_rows"],
            "intersecting_rows": len(frame), "selected_fields": selected_fields,
        })
    if not frames:
        raise ValueError(f"{key} 400m 영역에 교차하는 공식 건물이 없습니다.")
    curated = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=METRIC_CRS)
    keys = [_id_key(value) for value in curated.A1]
    id_counts = Counter(keys)
    issues, all_flags = [], []
    for number, ((_, row), building_id) in enumerate(zip(curated.iterrows(), keys)):
        flags = []
        if not building_id:
            flags.append("missing_building_id")
        elif id_counts[building_id] > 1:
            flags.append("duplicate_building_id")
        if not row.geometry.is_valid:
            flags.append("invalid_geometry_requires_core_repair")
        if row.geometry.geom_type not in ("Polygon", "MultiPolygon"):
            flags.append("non_polygon_geometry_requires_review")
        if valid_height(row.A16) is None:
            flags.append("missing_or_invalid_gis_height")
        all_flags.append(";".join(flags))
        if flags:
            issues.append({"curated_row": number, "building_id": building_id or None, "flags": flags})
    curated["import_quality_flags"] = all_flags
    missing = sum(valid_height(value) is None for value in curated.A16)
    known = len(curated) - missing
    record_dates = sorted({pd.Timestamp(value).date().isoformat()
                           for value in curated.get("A22", pd.Series(dtype=object)).dropna()})
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"{key}-official-", dir=output.parent) as temporary:
        stage = Path(temporary)
        curated.to_file(stage / "buildings.gpkg", layer="buildings", driver="GPKG", index=False)
        schema = {
            "source_name": SOURCE_NAME, "source_url": SOURCE_URL,
            "snapshot_date": snapshot_date, "record_reference_dates": record_dates,
            "id_field": "A1", "name_field": "A24" if "A24" in curated.columns else None,
            "height_field": "A16", "height_unit": "m", "height_semantics": "above_ground",
            "source_crs": METRIC_CRS, "layer": "buildings", "encoding": None,
        }
        _write_json(stage / "schema.json", schema)
        quality = {
            "row_count": len(curated), "known_height_rows": known,
            "missing_height_rows": missing, "flagged_rows": len(issues), "issues": issues,
            "hold_recommended": missing > known,
            "hold_recommendation_scope": "GIS_only_preliminary",
            "ledger_check_status": "not_performed_by_importer",
            "hold_rule": "GIS-only preliminary flag. Final hold decision must use GIS plus verified same-building title-register heights, after the ledger check.",
            "scope": "Only coordinate-bearing source records with positive-area intersection with the exact AOI; source rows without coordinates cannot be spatially attributed.",
        }
        _write_json(stage / "import_quality.json", quality)
        provenance = {
            "source_name": SOURCE_NAME, "source_url": SOURCE_URL, "catalog_url": CATALOG_URL,
            "source_file": source.name, "original_file_sha256": sha256(source),
            "snapshot_date": snapshot_date, "record_reference_dates": record_dates,
            "imported_at_utc": datetime.now(timezone.utc).isoformat(),
            "file": "buildings.gpkg", "sha256": sha256(stage / "buildings.gpkg"), "layer": "buildings",
            "curated_crs": METRIC_CRS, "center_lonlat": center, "size_m": SIZE,
            "region_key": key, "region_name": config["name"], "environment": config["environment"],
            "center_note": config["center_note"], "selection_source": selection_source,
            "aoi_bounds_projected_m": list(aoi.bounds), "local_origin_projected_m": list(origin),
            "source_parts": source_parts, "row_count": len(curated), "flagged_rows": len(issues),
            "missing_id_rows": sum(not value for value in keys),
            "duplicate_id_rows": sum(bool(value) and id_counts[value] > 1 for value in keys),
            "known_height_rows": known, "missing_or_invalid_height_rows": missing,
            "hold_recommended": missing > known,
            "hold_recommendation_scope": "GIS_only_preliminary",
            "ledger_check_status": "not_performed_by_importer",
            "district_codes": sorted(str(value) for value in curated.get("A23", pd.Series(dtype=str)).dropna().unique()),
            "declared_origin": "User-obtained official VWorld AL_D010 source file; no independent building survey.",
            "transformation": "Source-CRS bbox filter, EPSG:5179 reprojection, positive-area exact AOI intersection selection. Full intersecting footprints and original A16 values retained; no clipping, imputation, deduplication, or geometry repair in curated source.",
            "license_notices": [
                {"source": SOURCE_URL, "label": "CC BY", "url": "https://creativecommons.org/licenses/by/2.0/kr/"},
                {"source": CATALOG_URL, "label": "공공저작물 : 출처표시 (제 1유형)"},
            ],
            "attribution": f"국토교통부 GIS건물통합정보, 브이월드 제공. {config['name']} 연구용 400m 관심영역 추출 및 좌표 변환.",
            "is_synthetic": False,
        }
        _write_json(stage / "buildings.provenance.json", provenance)
        license_text = f"""# Official building subset — {config['name']}

Source: **국토교통부 GIS건물통합정보**, distributed by [브이월드]({SOURCE_URL}).
The user obtained `{source.name}` from the official download page, reference date **{snapshot_date}**.
The province/city-wide archive is preserved locally and excluded from the repository.

The VWorld download page labels this data **CC BY** and links to [Creative Commons Attribution 2.0 Korea](https://creativecommons.org/licenses/by/2.0/kr/).
The corresponding [public-data catalog]({CATALOG_URL}) separately lists **공공누리 제1유형 (출처표시)**.
Preserve attribution and identify modifications when sharing this subset or derived maps.

> 건물: 국토교통부 GIS건물통합정보, 브이월드 제공, 자료 기준일 {snapshot_date}. {config['name']} 중심 400m 관심영역 추출 및 좌표 변환.

`buildings.gpkg` retains complete source footprints intersecting the 400 m square centered at longitude {center[0]}, latitude {center[1]}, transformed to EPSG:5179. Building IDs and original A16 height values are unchanged. The subset does not fill missing heights. Downstream experiments may apply explicitly labeled height assumptions separately and record their parameters; these are not official source measurements.

Center selection: {config['center_note']}
Selection reference: {selection_source}

`buildings.provenance.json` records the configured center-selection evidence, archive/subset hashes, CRS, selected fields and processing. A source attribute is not an independent survey of present-day conditions. OSM background data has its own notice; software is separate. No source-organization endorsement is implied.
"""
        (stage / "LICENSE-BUILDINGS.md").write_text(license_text, encoding="utf-8")
        for name in OWN_FILES:
            with (stage / name).open("rb") as incoming, (output / name).open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    return provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/regions.five.json")
    parser.add_argument("--regions", nargs="+", help="Region keys to import; default all five")
    args = parser.parse_args(argv)
    configuration = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if configuration.get("size_m") != SIZE:
        parser.error("Only 400 m squares are supported by this import command.")
    keys = args.regions or list(configuration["regions"])
    for key in keys:
        if key not in configuration["regions"]:
            parser.error(f"Unknown region: {key}")
        result = import_region(key, configuration["regions"][key], configuration["selection_source"])
        print(json.dumps({"region": key, **{name: result[name] for name in (
            "row_count", "known_height_rows", "missing_or_invalid_height_rows", "hold_recommended", "sha256")}}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
