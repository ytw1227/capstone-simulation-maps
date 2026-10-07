"""Metric geometry, auditable height resolution, and prototype exports."""
from __future__ import annotations

import csv
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio
from pyproj import CRS, Transformer
from shapely import force_2d, make_valid
from shapely.affinity import translate
from shapely.geometry import MultiPolygon, Polygon, box, mapping
from shapely.ops import transform, unary_union

METRIC_CRS = "EPSG:5179"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def identifier(value):
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer() or abs(value) > 2**53:
            return ""
        return str(int(value))
    return str(value).strip()


def valid_height(value):
    """No floor-count guess, zero, negative, NaN or infinity accepted as a height."""
    try:
        height = float(value)
    except (TypeError, ValueError):
        return None
    return height if math.isfinite(height) and height > 0 else None


def region_geometry(lon, lat, size_m):
    if not (124 <= lon <= 132 and 33 <= lat <= 39):
        raise ValueError("이 시제품의 좌표 범위는 대한민국입니다. 경도, 위도 순서를 확인하세요.")
    if size_m not in (400, 1000):
        raise ValueError("지도 한 변은 400 또는 1000 m여야 합니다.")
    x, y = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform(lon, lat)
    half = size_m / 2
    return (x, y), box(x-half, y-half, x+half, y+half)


def polygonal(geometry):
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, Polygon):
        return geometry if geometry.area > 0 else None
    if hasattr(geometry, "geoms"):
        pieces = [polygonal(part) for part in geometry.geoms]
        pieces = [part for part in pieces if part is not None]
        return unary_union(pieces) if pieces else None
    return None


def validate_schema(schema):
    for key in ("source_name", "source_url", "snapshot_date", "id_field"):
        if not schema.get(key) or "REPLACE_" in str(schema[key]):
            raise ValueError(f"schema의 {key}를 실제 자료 정보로 지정하세요.")
    if schema.get("height_unit") != "m" or schema.get("height_semantics") != "above_ground":
        raise ValueError("건물 높이는 미터 단위의 지면 기준 높이여야 합니다. 해발고도를 사용하지 마세요.")
    if "height_field" not in schema or "REPLACE_" in str(schema["height_field"]):
        raise ValueError("height_field를 실제 높이 열 또는 null로 지정하세요.")


def read_buildings(path, schema, aoi):
    """Filter in source CRS before loading a city/province-wide GIS file."""
    validate_schema(schema)
    options = {k: schema[k] for k in ("layer", "encoding") if schema.get(k)}
    info = pyogrio.read_info(path, **options)
    source_crs = info.get("crs") or schema.get("source_crs")
    if not source_crs:
        raise ValueError("원본 CRS가 없습니다. 제공기관 메타데이터를 확인하고 schema.source_crs를 지정하세요.")
    if info.get("crs") and schema.get("source_crs"):
        if CRS.from_user_input(info["crs"]) != CRS.from_user_input(schema["source_crs"]):
            raise ValueError("파일 CRS와 schema.source_crs가 충돌합니다. 임의 재지정하지 않습니다.")
    fields = set(info["fields"])
    for field in (schema["id_field"], schema.get("height_field"), schema.get("name_field")):
        if field and field not in fields:
            raise ValueError(f"원본에 {field!r} 열이 없습니다. inspect 명령으로 확인하세요.")
    to_source = Transformer.from_crs(METRIC_CRS, source_crs, always_xy=True)
    # Densification accounts for curved projected bounds; source-CRS bbox is a coarse filter.
    bbox = to_source.transform_bounds(*aoi.bounds, densify_pts=21)
    frame = gpd.read_file(path, bbox=bbox, engine="pyogrio", **options)
    if frame.crs is None:
        frame = frame.set_crs(source_crs)
    return frame.to_crs(METRIC_CRS), {
        "source_crs": str(source_crs), "file": Path(path).name,
        "bbox_filter_rows": len(frame), "null_geometry_scope": "원본 공간 필터 밖/좌표 없는 원본 행의 지역 소속은 판단하지 않음",
    }


def read_csv_strings(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records")


class LedgerResolver:
    """Only explicit, evidenced, one-building-to-one-title-record crosswalks."""
    def __init__(self, ledger_rows=(), matches=()):
        self.ledger = {}
        self.matches = {}
        self.target_counts = Counter()
        for row in ledger_rows:
            self.ledger.setdefault((row.get("namespace", ""), row.get("ledger_id", "")), []).append(row)
        for row in matches:
            self.matches.setdefault(row.get("building_id", ""), []).append(row)
            self.target_counts[(row.get("namespace", ""), row.get("ledger_id", ""))] += 1

    def resolve(self, building_id):
        candidates = self.matches.get(building_id, [])
        if not candidates:
            return None, "no_verified_match", ""
        if len(candidates) != 1:
            return None, "ambiguous_crosswalk", ""
        match = candidates[0]
        key = (match.get("namespace", ""), match.get("ledger_id", ""))
        if not all(key) or self.target_counts[key] != 1:
            return None, "ambiguous_or_missing_ledger_identity", ""
        if str(match.get("verified", "")).lower() != "true":
            return None, "unverified_match", ""
        if match.get("identity_method") not in {"official_crosswalk", "manual_building_confirmation"}:
            return None, "unsupported_identity_method", ""
        if not match.get("identity_evidence", "").strip() or "REPLACE_" in match["identity_evidence"]:
            return None, "missing_identity_evidence", ""
        records = self.ledger.get(key, [])
        if len(records) != 1:
            return None, "missing_or_ambiguous_ledger_record", ""
        record = records[0]
        if record.get("record_type") != "title":
            return None, "not_building_title_record", ""
        height = valid_height(record.get("height_m"))
        if height is None:
            return None, "invalid_ledger_height", ""
        evidence = json.dumps({k: match[k] for k in ("ledger_id", "namespace", "identity_method", "identity_evidence")}, ensure_ascii=False)
        return height, "verified_ledger", evidence


def prepare_buildings(frame, schema, aoi, resolver=None):
    resolver = resolver or LedgerResolver()
    if frame.crs is None:
        raise ValueError("건물 GeoDataFrame에는 CRS가 필요합니다.")
    name_field = schema.get("name_field")
    if name_field and name_field not in frame.columns:
        raise ValueError(f"건물 이름 열 {name_field!r}이 원본에 없습니다.")
    frame = frame.to_crs(METRIC_CRS)
    ids = [identifier(v) for v in frame[schema["id_field"]]]
    counts = Counter(ids)
    rows, quality = [], []
    for index, ((_, raw), original_id) in enumerate(zip(frame.iterrows(), ids)):
        flags = []
        usable_id = bool(original_id) and counts[original_id] == 1
        building_id = original_id if usable_id else f"unresolved-row-{index}"
        if not original_id:
            flags.append("missing_or_unsafe_building_id")
        elif counts[original_id] != 1:
            flags.append("duplicate_building_id")
        raw_name = raw[name_field] if name_field else None
        building_name = "" if raw_name is None or pd.isna(raw_name) else str(raw_name).strip()
        name_source = ("synthetic" if schema.get("name_source") == "synthetic" else "gis") if building_name else "missing"
        base = {"building_id": building_id, "source_building_id": original_id,
                "building_name": building_name, "name_source": name_source}
        geom = raw.geometry
        if geom is None or geom.is_empty:
            quality.append({**base, "quality_flags": "empty_geometry", "included": False})
            continue
        geom = force_2d(geom)
        if not geom.is_valid:
            geom = make_valid(geom)
            flags.append("geometry_repaired")
        geom = polygonal(geom)
        if geom is None:
            quality.append({**base, "quality_flags": "non_polygon_geometry", "included": False})
            continue
        if not geom.intersects(aoi):
            continue
        if not aoi.covers(geom):
            flags.append("clipped_at_aoi_boundary")
        geom = polygonal(geom.intersection(aoi))
        if geom is None or geom.area == 0:
            continue
        raw_height = raw[schema["height_field"]] if schema.get("height_field") else None
        height = valid_height(raw_height)
        source, match_status, evidence = "gis", "not_needed", ""
        if height is None:
            flags.append("missing_or_invalid_gis_height")
            height, match_status, evidence = resolver.resolve(original_id) if usable_id else (None, "unsafe_building_identity", "")
            source = "ledger" if height is not None else "missing"
        if height is None:
            flags.extend(["unresolved_height", match_status])
        if height is not None and height > 1000:
            flags.append("height_above_1000m_review")
        record = {**base, "gis_height_raw": "" if raw_height is None or pd.isna(raw_height) else str(raw_height), "height_m": height, "height_source": source, "match_status": match_status,
                  "identity_evidence": evidence, "quality_flags": ";".join(flags), "area_m2": float(geom.area)}
        rows.append({**record, "geometry": geom})
        quality.append({**record, "included": True})
    columns = ["building_id", "source_building_id", "building_name", "name_source", "gis_height_raw", "height_m", "height_source", "match_status", "identity_evidence", "quality_flags", "area_m2", "geometry"]
    return gpd.GeoDataFrame(rows, columns=columns, geometry="geometry", crs=METRIC_CRS), quality


def localize(frame, origin):
    result = frame.copy()
    result.geometry = result.geometry.map(lambda g: translate(g, xoff=-origin[0], yoff=-origin[1]))
    # Local metres are not EPSG:5179 coordinates. Never attach a false EPSG to them.
    return result.set_crs(None, allow_override=True)


def export_model(output, buildings, background, quality, metadata, origin, aoi, *, no_fly_zones=None):
    from .preview import export_obj, write_preview, is_tunnel_background
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"출력 폴더가 비어 있지 않습니다: {output}. 새 출력 경로를 지정하세요.")
    output.mkdir(parents=True, exist_ok=True)
    unknown = buildings.height_source.eq("missing")
    imputed = buildings.height_source.eq("imputed")
    total_area = float(buildings.area.sum())
    counts = {"buildings": len(buildings), "gis_height": int(buildings.height_source.eq("gis").sum()),
              "ledger_height": int(buildings.height_source.eq("ledger").sum()), "missing_height": int(unknown.sum()),
              "imputed_height": int(imputed.sum()),
              "named_buildings": int(buildings.building_name.ne("").sum()),
              "missing_name": int(buildings.building_name.eq("").sum()),
              "flagged_buildings": int(buildings.quality_flags.ne("").sum()),
              "rejected_geometry_rows_in_loaded_subset": sum(not row.get("included", False) for row in quality),
              "unknown_height_area_fraction": float(buildings.loc[unknown].area.sum()/total_area) if total_area else None}
    metadata = {**metadata, "counts": counts, "created_utc": datetime.now(timezone.utc).isoformat(),
                "metric_crs": METRIC_CRS, "local_origin_projected_m": list(origin),
                "local_axes": {"x": "east_m", "y": "north_m", "z": "above_flat_ground_m"},
                "local_transform": "local_x = EPSG5179_easting - origin[0]; local_y = EPSG5179_northing - origin[1]",
                "ground_z_m": 0, "aoi_bounds_projected_m": list(aoi.bounds),
                "local_bounds": [-metadata["size_m"]/2]*2 + [metadata["size_m"]/2]*2,
                "simulation_ready": False, "purpose": "모델링 검토용 시제품; 전파 계산/재료/지형 없음",
                "mesh_complete": not bool(unknown.any()) and len(buildings) > 0 and not counts["rejected_geometry_rows_in_loaded_subset"],
                "unknown_height_policy": "원본 외곽선과 null 높이 보존. OBJ에는 미포함, 화면에는 주황 외곽선. 자유공간으로 해석 금지.",
                "background_surface_display": {"tunnel_features_hidden": sum(is_tunnel_background(row) for _, row in background.iterrows()),
                                               "policy": "원본 배경 보존. tunnel 태그가 있는 요소는 지표면 표시에서 제외. 도로 폭과 layer 고도는 추정하지 않음."},
                "boundary_policy": "AOI와 교차하는 건물을 경계에서 자름. 실험 범위 밖 건물과 외부 신호 영향은 제외하는 설정."}
    metadata["all_heights_verified"] = not bool(unknown.any() or imputed.any())
    if imputed.any():
        metadata["height_interpretation"] = "height_m includes explicitly labelled experimental estimates; observed_height_m preserves confirmed values only."
        metadata["unknown_height_policy"] = "GIS와 표제부 확인 후에도 미확인인 높이만 지역 확인 높이 평균의 ±5m 범위로 추정. 추정 평균은 확인 평균과 같으며 원본 결측과 출처는 보존. 추정 3D 및 금지영역은 별도 색상."
    if no_fly_zones is not None:
        metadata["no_fly_counts"] = {
            "total": len(no_fly_zones),
            "confirmed_height": int((~no_fly_zones.is_estimated.astype(bool)).sum()),
            "estimated_height": int(no_fly_zones.is_estimated.astype(bool).sum()),
        }
    local_buildings, local_background = localize(buildings, origin), localize(background, origin)
    local_zones = localize(no_fly_zones, origin) if no_fly_zones is not None else None
    local_buildings.attrs["is_demo"] = metadata["is_demo"]
    if len(buildings):
        buildings.to_file(output/"model.gpkg", layer="buildings", driver="GPKG", index=False)
    gpd.GeoDataFrame({"name": ["AOI"]}, geometry=[aoi], crs=METRIC_CRS).to_file(output/"model.gpkg", layer="aoi", driver="GPKG", index=False)
    if len(background):
        background.to_file(output/"model.gpkg", layer="background", driver="GPKG", index=False)
    if no_fly_zones is not None and len(no_fly_zones):
        no_fly_zones.to_file(output/"model.gpkg", layer="no_fly_zones", driver="GPKG", index=False)
    features = []
    for _, row in local_buildings.iterrows():
        feature = {"building_id": row.building_id, "building_name": row.building_name,
                         "name_source": row.name_source, "height_m": valid_height(row.height_m),
                         "height_source": row.height_source, "quality_flags": row.quality_flags,
                         "geometry_local_m": mapping(row.geometry)}
        if "observed_height_m" in row:
            feature.update(observed_height_m=valid_height(row.observed_height_m),
                           height_source_original=row.height_source_original,
                           imputation_delta_m=None if pd.isna(row.imputation_delta_m) else float(row.imputation_delta_m),
                           ledger_check_status=row.ledger_check_status)
        features.append(feature)
    scene = {"format": "local-metre-scene-v1 (not geographic GeoJSON)", "metadata": metadata, "buildings": features}
    if local_zones is not None:
        scene["no_fly_zones"] = [
            {"building_id": row.building_id, "height_m": float(row.height_m),
             "height_source": row.height_source, "is_estimated": bool(row.is_estimated),
             "flight_altitude_m": float(row.flight_altitude_m), "threshold_m": float(row.threshold_m),
             "safety_margin_m": float(row.safety_margin_m), "geometry_local_m": mapping(row.geometry)}
            for _, row in local_zones.iterrows()
        ]
        write_json(output/"no_fly.local.json", {"format": "local-metre-no-fly-v1 (not geographic GeoJSON)",
                   "flight_policy": metadata.get("flight_policy", {}), "zones": scene["no_fly_zones"]})
    write_json(output/"scene.local.json", scene)
    write_json(output/"manifest.json", metadata)
    fieldnames = ["building_id", "source_building_id", "building_name", "name_source", "gis_height_raw", "height_m", "height_source", "match_status", "identity_evidence", "quality_flags", "area_m2", "included"]
    fieldnames += [name for name in ("observed_height_m", "height_source_original", "imputation_delta_m", "ledger_check_status")
                   if name in buildings.columns]
    with (output/"quality.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(quality)
    observed_buildings = local_buildings.loc[~imputed].copy() if imputed.any() else local_buildings
    observed_buildings.attrs["excluded_imputed_count"] = int(imputed.sum())
    export_obj(observed_buildings, output/"buildings_known_heights.obj")
    if "height_policy" in metadata:
        export_obj(local_buildings, output/"buildings_model_heights.obj")
    write_preview(local_buildings, local_background, metadata, output/"preview.html", no_fly_zones=local_zones)
    return metadata
