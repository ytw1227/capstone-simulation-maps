"""Curate a user-obtained official AL_D010 SHP/ZIP into the Gangnam 400 m bundle.

This script does not download data, authenticate, guess heights, or create
buildings. Obtain the official file through VWorld first. All intersecting
source footprints are retained whole; the model builder performs the AOI clip.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd
import pandas as pd
import pyogrio
from pyproj import CRS
from shapely import make_valid

from region_model.core import METRIC_CRS, read_buildings, region_geometry, valid_height

CENTER = [127.0276, 37.4979]
SIZE = 400
SOURCE_NAME = "국토교통부 GIS건물통합정보 (브이월드 AL_D010)"
SOURCE_URL = "https://www.vworld.kr/dtmk/dtmk_ntads_s002.do?svcCde=NA&dsId=18"
CATALOG_URL = "https://www.data.go.kr/data/15083092/fileData.do"
SELECTED_FIELDS = ("A0", "A1", "A2", "A3", "A4", "A5", "A16", "A19", "A22",
                   "A23", "A24", "A25", "A26", "A27", "A28")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _prj_crs(content):
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return CRS.from_wkt(content.decode(encoding))
        except UnicodeDecodeError:
            continue
    raise ValueError("원본 .prj 문자 인코딩을 해석할 수 없습니다.")


def _same_crs(first, second):
    # ESRI .prj omits some EPSG conversion/axis metadata. PROJ can recognize
    # that WKT with 100% authority confidence even when strict equality fails.
    if first.equals(second, ignore_axis_order=True):
        return True
    first_epsg = first.to_epsg(min_confidence=100)
    if first_epsg is not None and first_epsg == second.to_epsg(min_confidence=100):
        return True
    # Actual 2026 AL_D010 .prj uses x=east/y=north and the unit alias "m".
    # PROJ does not consider that WKT equal to EPSG's north/east, "metre"
    # representation, even with ignore_axis_order. Verify the mathematical
    # definition instead of trusting the .prj's AUTHORITY label or lowering
    # authority confidence. Shapefile/GDAL coordinates use traditional x/y.
    if not (first.is_projected and second.is_projected
            and first.geodetic_crs.equals(second.geodetic_crs, ignore_axis_order=True)):
        return False
    for candidate in (first, second):
        axes = candidate.axis_info
        if (len(axes) != 2 or {axis.direction for axis in axes} != {"east", "north"}
                or any(axis.unit_conversion_factor != 1 for axis in axes)):
            return False
    operations = [candidate.coordinate_operation for candidate in (first, second)]
    if any(operation is None or operation.method_auth_name != "EPSG"
           or operation.method_code != "9807" for operation in operations):
        return False
    definitions = []
    for operation in operations:
        parameters = {(parameter.auth_name, parameter.code, parameter.unit_category):
                      parameter.value * parameter.unit_conversion_factor for parameter in operation.params}
        if len(parameters) != 5 or len(operation.params) != 5:
            return False
        definitions.append(parameters)
    return (definitions[0].keys() == definitions[1].keys()
            and all(math.isclose(value, definitions[1][key], rel_tol=0, abs_tol=1e-12)
                    for key, value in definitions[0].items()))


def _sources(path, layer=None):
    """Yield GDAL source paths and the original, unmodified .prj bytes."""
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            lowered = {name.lower(): name for name in names}
            members = [name for name in names if name.lower().endswith(".shp")]
            if layer:
                members = [name for name in members if PurePosixPath(name).stem == layer or name == layer]
            if not members:
                raise ValueError("ZIP 안에 선택한 SHP가 없습니다. --layer에는 SHP 파일명(확장자 제외)을 지정하세요.")
            for member in sorted(members):
                # Do not extract archive paths; reject traversal/absolute member names.
                posix = PurePosixPath(member)
                if posix.is_absolute() or ".." in posix.parts or "\\" in member or ":" in member:
                    raise ValueError("ZIP에 안전하지 않은 SHP 경로가 있습니다.")
                stem = str(posix.with_suffix(""))
                required = [stem + suffix for suffix in (".shp", ".shx", ".dbf", ".prj")]
                missing = [name for name in required if name.lower() not in lowered]
                if missing:
                    raise ValueError(f"SHP 부속 파일이 없습니다: {missing}")
                prj_name = lowered[(stem + ".prj").lower()]
                yield f"/vsizip/{path.resolve().as_posix()}/{member}", archive.read(prj_name), {
                    "archive_member": member,
                    "prj_sha256": hashlib.sha256(archive.read(prj_name)).hexdigest(),
                }
    elif path.suffix.lower() == ".shp":
        siblings = {item.name.lower(): item for item in path.parent.iterdir() if item.is_file()}
        companions = {}
        for suffix in (".shp", ".shx", ".dbf", ".prj"):
            filename = path.stem + suffix
            companion = siblings.get(filename.lower())
            if companion is None:
                raise ValueError(f"SHP 부속 파일이 없습니다: {filename}")
            companions[companion.name] = sha256(companion)
        cpg = siblings.get((path.stem + ".cpg").lower())
        if cpg is not None:
            companions[cpg.name] = sha256(cpg)
        if layer and layer != path.stem:
            raise ValueError("--layer가 SHP 파일명과 다릅니다.")
        prj = siblings[(path.stem + ".prj").lower()]
        yield str(path.resolve()), prj.read_bytes(), {"file": path.name, "component_sha256": companions}
    else:
        raise ValueError("공식 SHP 파일 또는 해당 부속 파일을 포함한 ZIP을 지정하세요.")


def _id_key(value):
    return value.strip() if isinstance(value, str) else ""


def _verified_background(background_file, background_meta):
    """Bind the exact acquired background file to this fixed AOI before merging."""
    background = _read_json(background_meta)
    if not isinstance(background, dict):
        raise ValueError("배경 출처 기록은 JSON 객체여야 합니다.")
    actual_hash = sha256(background_file)
    if background.get("sha256") != actual_hash:
        raise ValueError("배경 파일 해시와 배경 출처 기록이 다르거나 해시가 없습니다.")
    center = background.get("center_lonlat")
    try:
        same_center = (isinstance(center, list) and len(center) == 2
                       and all(math.isclose(float(value), expected, rel_tol=0, abs_tol=1e-9)
                               for value, expected in zip(center, CENTER)))
    except (TypeError, ValueError):
        same_center = False
    if not same_center or background.get("size_m") != SIZE:
        raise ValueError("배경 출처 기록의 중심점/범위가 강남역 400m와 다릅니다.")
    if not (background.get("source") == "OpenStreetMap via OSMnx"
            and background.get("status") == "ok"
            and background.get("synthetic") is False
            and background.get("buildings_used") is False
            and background.get("file") == background_file.name):
        raise ValueError("배경 출처 기록의 실제 OSM 자료/파일명 조건을 확인하세요.")
    return background


def import_official(source, snapshot_date, output, *, layer=None, encoding=None):
    """Import the supplied official file; never overwrite curated artifacts."""
    source, output = Path(source), Path(output)
    snapshot = date.fromisoformat(snapshot_date)
    if not source.is_file():
        raise ValueError("공식 원본 파일이 없습니다. 합성 데이터로 대체하지 않습니다.")
    if snapshot >= date(2023, 8, 8):
        expected_crs = CRS.from_epsg(5186)
    elif snapshot <= date(2023, 8, 5):
        expected_crs = CRS.from_epsg(5174)
    else:
        raise ValueError("해당 전환일의 공식 좌표계를 추가 확인해야 합니다.")
    own_files = ("buildings.gpkg", "schema.json", "buildings.provenance.json", "import_quality.json", "provenance.json")
    existing = [name for name in own_files if (output / name).exists()]
    if existing:
        raise ValueError(f"기존 결과를 덮어쓰지 않습니다. 새 --out 경로를 지정하세요: {existing}")
    origin, aoi = region_geometry(*CENTER, SIZE)
    frames, sources = [], []
    for read_path, prj_bytes, source_meta in _sources(source, layer):
        options = {"encoding": encoding} if encoding else {}
        info = pyogrio.read_info(read_path, **options)
        if not info.get("crs"):
            raise ValueError("원본 SHP에서 CRS를 읽지 못했습니다.")
        actual_crs, prj_crs = CRS.from_user_input(info["crs"]), _prj_crs(prj_bytes)
        if not _same_crs(actual_crs, prj_crs):
            raise ValueError("GDAL이 읽은 CRS와 원본 .prj가 다릅니다.")
        if not _same_crs(actual_crs, expected_crs):
            raise ValueError(f"자료 기준일의 공식 배포 CRS와 다릅니다: {actual_crs.to_string()} / {expected_crs.to_string()}")
        fields = set(info["fields"])
        if not {"A1", "A16"}.issubset(fields):
            raise ValueError("공식 AL_D010 필수 입력 열 A1/A16이 없습니다.")
        schema = {"source_name": SOURCE_NAME, "source_url": SOURCE_URL, "snapshot_date": snapshot_date,
                  "id_field": "A1", "name_field": "A24" if "A24" in fields else None,
                  "height_field": "A16", "height_unit": "m", "height_semantics": "above_ground",
                  "source_crs": actual_crs.to_string(), "encoding": encoding}
        frame, read_meta = read_buildings(read_path, schema, aoi)
        # Use repaired geometry ONLY for selection. Preserve the source footprint
        # itself (including invalid geometry) for the normal core QA/repair path.
        intersects = frame.geometry.map(lambda geom: bool(
            geom is not None and not geom.is_empty
            and (geom if geom.is_valid else make_valid(geom)).intersects(aoi)))
        frame = frame.loc[intersects, [field for field in SELECTED_FIELDS if field in frame.columns] + ["geometry"]].copy()
        if not frame.empty:
            non_strings = frame.A1.map(lambda value: value is not None and not pd.isna(value) and not isinstance(value, str))
            if non_strings.any():
                raise ValueError("A1이 문자열이 아닙니다. 건물 식별번호의 반올림/선행 0 손실 여부를 확인하세요.")
            frames.append(frame)
        sources.append({**source_meta, "original_crs": actual_crs.to_string(),
                        "original_crs_wkt": actual_crs.to_wkt(), "source_total_rows": int(info["features"]),
                        "requested_encoding": encoding, "driver_reported_encoding": info.get("encoding"),
                        "bbox_filter_rows": read_meta["bbox_filter_rows"], "intersecting_rows": len(frame),
                        "selected_fields": [field for field in SELECTED_FIELDS if field in fields]})
    if not frames:
        raise ValueError("강남역 400m 영역과 교차하는 공식 건물 도형이 없습니다.")
    curated = gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs=METRIC_CRS)
    keys = [_id_key(value) for value in curated.A1]
    counts = Counter(keys)
    issues, flags = [], []
    for number, ((_, row), key) in enumerate(zip(curated.iterrows(), keys)):
        row_flags = []
        if not key:
            row_flags.append("missing_building_id")
        elif counts[key] > 1:
            row_flags.append("duplicate_building_id")
        if not row.geometry.is_valid:
            row_flags.append("invalid_geometry_requires_core_repair")
        if row.geometry.geom_type not in ("Polygon", "MultiPolygon"):
            row_flags.append("non_polygon_geometry_requires_review")
        if valid_height(row.A16) is None:
            row_flags.append("missing_or_invalid_gis_height")
        flags.append(";".join(row_flags))
        if row_flags:
            issues.append({"curated_row": number, "building_id": key or None, "flags": row_flags})
    curated["import_quality_flags"] = flags
    record_reference_dates = sorted({pd.Timestamp(value).date().isoformat()
                                    for value in curated.get("A22", pd.Series(dtype=object)).dropna()})
    source_hash = sha256(source)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="official-import-", dir=output.parent) as temporary:
        stage = Path(temporary)
        curated.to_file(stage / "buildings.gpkg", layer="buildings", driver="GPKG", index=False)
        curated_hash = sha256(stage / "buildings.gpkg")
        curated_schema = {"source_name": SOURCE_NAME, "source_url": SOURCE_URL, "snapshot_date": snapshot_date,
                          "record_reference_dates": record_reference_dates,
                          "id_field": "A1", "name_field": "A24" if "A24" in curated.columns else None,
                          "height_field": "A16", "height_unit": "m", "height_semantics": "above_ground",
                          "source_crs": METRIC_CRS, "layer": "buildings", "encoding": None}
        _write_json(stage / "schema.json", curated_schema)
        _write_json(stage / "import_quality.json", {"row_count": len(curated), "flagged_rows": len(issues), "issues": issues,
                    "scope": "Only source-CRS bbox-loaded records intersecting the AOI; source rows without coordinates are not spatially attributable."})
        provenance = {"source_name": SOURCE_NAME, "source_url": SOURCE_URL, "catalog_url": CATALOG_URL,
                      "source_file": source.name, "original_file_sha256": source_hash,
                      "snapshot_date": snapshot_date, "imported_at_utc": datetime.now(timezone.utc).isoformat(),
                      "record_reference_dates": record_reference_dates,
                      "file": "buildings.gpkg", "sha256": curated_hash, "layer": "buildings",
                      "curated_crs": METRIC_CRS, "center_lonlat": CENTER, "size_m": SIZE,
                      "aoi_bounds_projected_m": list(aoi.bounds), "local_origin_projected_m": list(origin),
                      "source_parts": sources, "row_count": len(curated), "flagged_rows": len(issues),
                      "missing_id_rows": sum(not key for key in keys),
                      "duplicate_id_rows": sum(bool(key) and counts[key] > 1 for key in keys),
                      "missing_or_invalid_height_rows": sum(valid_height(value) is None for value in curated.A16),
                      "district_codes": sorted(str(value) for value in curated.get("A23", pd.Series(dtype=str)).dropna().unique()),
                      "declared_origin": "User-obtained official VWorld AL_D010 source file; no independent building survey.",
                      "transformation": "Source-CRS bbox filter, EPSG:5179 reprojection, exact AOI intersection selection. Full intersecting footprints and original A16 values retained; no clipping, imputation, deduplication, or geometry repair in curated source.",
                      "license_notices": [
                          {"source": SOURCE_URL, "label": "CC BY", "url": "https://creativecommons.org/licenses/by/2.0/kr/"},
                          {"source": CATALOG_URL, "label": "공공저작물 : 출처표시 (제 1유형)"}],
                      "attribution": "국토교통부 GIS건물통합정보, 브이월드 제공. 연구용 400m 관심영역 추출 및 좌표 변환.",
                      "is_synthetic": False}
        _write_json(stage / "buildings.provenance.json", provenance)
        background_file, background_meta = output / "background.gpkg", output / "background.provenance.json"
        if background_file.exists() and background_meta.exists():
            background = _verified_background(background_file, background_meta)
            _write_json(stage / "provenance.json", {"dataset_id": "gangnam_actual_400", "is_synthetic": False,
                        "center_lonlat": CENTER, "size_m": SIZE, "buildings": provenance, "background": background})
        for artifact in stage.iterdir():
            # Exclusive creation preserves any existing artifact even in a race.
            with artifact.open("rb") as incoming, (output / artifact.name).open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
    return provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="공식 AL_D010 SHP 또는 ZIP")
    parser.add_argument("--snapshot-date", required=True, help="배포 자료 기준일 YYYY-MM-DD")
    parser.add_argument("--out", type=Path, default=ROOT / "data" / "gangnam_400")
    parser.add_argument("--layer", help="ZIP에서 선택할 SHP 파일명(확장자 제외); 생략 시 모든 SHP")
    parser.add_argument("--encoding", help="DBF 문자 인코딩. 원본 메타데이터 확인 후 cp949 등 지정")
    args = parser.parse_args(argv)
    try:
        result = import_official(args.source, args.snapshot_date, args.out, layer=args.layer, encoding=args.encoding)
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    print(json.dumps({key: result[key] for key in ("row_count", "flagged_rows", "missing_or_invalid_height_rows", "sha256")}, ensure_ascii=False, indent=2))
    if not (args.out / "provenance.json").exists():
        print("건물 자료만 준비되었습니다. background.gpkg와 background.provenance.json이 없어 전체 출처 묶음은 생성하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
