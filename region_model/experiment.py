"""Explicit experimental heights and separate fixed-altitude flight constraints.

Source GIS and verified ledger heights stay untouched. Estimated heights are
model assumptions, never observations. Ledger lookup must be accounted for
before missing heights may be estimated.
"""
from __future__ import annotations

import math
import random

import geopandas as gpd
from shapely import force_2d, make_valid

from .core import METRIC_CRS, polygonal, valid_height


CHECKED_LEDGER_STATUSES = {
    "no_title_found", "title_height_missing", "identity_ambiguous",
    "identity_unconfirmed", "verified_height",
}


def apply_height_policy(buildings, quality, *, seed, ledger_audit, max_deviation_m=5.0):
    """Return a new model frame, quality rows and an auditable readiness decision.

    A lookup failure/pending authentication is not a completed ledger check.
    Pairwise opposite random offsets have zero total, including an odd final
    zero. Sorted IDs make the result independent of input row order.
    """
    if not math.isfinite(max_deviation_m) or max_deviation_m < 0:
        raise ValueError("높이 편차는 유한한 0 이상의 값이어야 합니다.")
    if not buildings.height_source.isin(["gis", "ledger", "missing"]).all():
        raise ValueError("원본 GIS 또는 검증 대장 입력만 허용합니다. 이미 추정한 높이를 확인 높이로 재사용할 수 없습니다.")
    for _, row in buildings.iterrows():
        has_height = valid_height(row.height_m) is not None
        if has_height != (row.height_source in {"gis", "ledger"}):
            raise ValueError("높이 값과 확인 출처가 일치하지 않습니다.")
        if not has_height and ledger_audit.get(str(row.building_id), {}).get("status") == "verified_height":
            raise ValueError("대장 높이 확인 결과를 건물에 연결한 뒤 추정 정책을 적용하세요.")
    result = buildings.copy()
    result["observed_height_m"] = result.height_m
    result["height_source_original"] = result.height_source
    result["imputation_delta_m"] = float("nan")
    result["ledger_check_status"] = result.building_id.map(
        lambda key: ledger_audit.get(str(key), {}).get("status", "not_required")
    )
    missing = result.height_m.map(lambda value: valid_height(value) is None)
    known = ~missing
    known_count, missing_count = int(known.sum()), int(missing.sum())
    mean = math.fsum(float(value) for value in result.loc[known, "height_m"]) / known_count if known_count else None
    pending = [str(key) for key in result.loc[missing, "building_id"]
               if ledger_audit.get(str(key), {}).get("status") not in CHECKED_LEDGER_STATUSES]
    policy = {
        "status": "ready", "known_count": known_count,
        "missing_count_before": missing_count, "imputed_count": 0,
        "mean_known_height_m": mean, "mean_imputed_height_m": None,
        "max_deviation_m": max_deviation_m, "seed": int(seed),
        "method": "balanced_uniform_pairs_sorted_building_ids_v1",
        "ledger_pending_count": len(pending),
        "hold_rule": "After GIS and ledger checks: unresolved count > verified count; no verified heights also holds.",
        "note": "Estimated heights are user-defined experimental assumptions, not measured building heights.",
    }
    if pending:
        policy.update(status="pending_ledger", reason="건축물대장 확인 미완료", pending_building_ids=pending)
    elif not known_count:
        policy.update(status="held", reason="확인된 높이가 없어 평균을 계산할 수 없음")
    elif missing_count > known_count:
        policy.update(status="held", reason="대장 확인 후에도 미확인 건물이 확인 건물보다 많음")
    elif missing_count:
        rng = random.Random(int(seed))
        # A very small positive observed mean must not produce a negative height.
        amplitude = min(float(max_deviation_m), mean * (1 - 1e-9))
        offsets = []
        for _ in range(missing_count // 2):
            offset = rng.uniform(0.0, amplitude)
            offsets.extend((offset, -offset))
        if missing_count % 2:
            offsets.append(0.0)
        rng.shuffle(offsets)
        rows = sorted(result.index[missing], key=lambda index: str(result.at[index, "building_id"]))
        for index, offset in zip(rows, offsets):
            result.at[index, "height_m"] = mean + offset
            result.at[index, "height_source"] = "imputed"
            result.at[index, "imputation_delta_m"] = offset
            flags = [flag for flag in str(result.at[index, "quality_flags"]).split(";")
                     if flag and flag != "unresolved_height"]
            result.at[index, "quality_flags"] = ";".join(flags + ["experimental_height_imputation"])
        estimated_mean = math.fsum(float(value) for value in result.loc[missing, "height_m"]) / missing_count
        if not math.isclose(estimated_mean, mean, rel_tol=0, abs_tol=1e-10):
            raise ValueError("추정 높이 평균 보존 검증 실패")
        policy.update(imputed_count=missing_count, mean_imputed_height_m=estimated_mean,
                      effective_deviation_limit_m=amplitude)
    indexed = result.set_index("building_id")
    updated_quality = []
    for row in quality:
        record = dict(row)
        if row.get("included") and row["building_id"] in indexed.index:
            current = indexed.loc[row["building_id"]]
            for field in ("height_m", "height_source", "quality_flags", "observed_height_m",
                          "height_source_original", "imputation_delta_m", "ledger_check_status"):
                record[field] = current[field]
        updated_quality.append(record)
    return result, updated_quality, policy


def derive_no_fly_zones(buildings, aoi, *, flight_altitude_m=50.0, safety_margin_m=5.0,
                        original_footprints=None):
    """Buffer footprints horizontally, then clip derived zones to the study AOI.

    These are experiment collision constraints, not regulated airspace. Use
    full source footprints where supplied so AOI clipping cannot shrink the
    buffer along a cut building. The original building geometry is not edited.
    """
    if not math.isfinite(flight_altitude_m) or flight_altitude_m <= 0:
        raise ValueError("비행 고도는 유한한 양수여야 합니다.")
    if not math.isfinite(safety_margin_m) or safety_margin_m < 0:
        raise ValueError("수평 안전 여유는 유한한 0 이상의 값이어야 합니다.")
    records = []
    for _, building in buildings.iterrows():
        if building.height_source not in {"gis", "ledger", "imputed", "missing"}:
            raise ValueError("비행 제한 영역을 생성할 높이 출처가 유효하지 않습니다.")
        height = valid_height(building.height_m)
        if (height is not None) != (building.height_source in {"gis", "ledger", "imputed"}):
            raise ValueError("비행 제한 영역의 높이와 출처가 일치하지 않습니다.")
        if height is None or height < flight_altitude_m:
            continue
        footprint = (original_footprints or {}).get(str(building.source_building_id), building.geometry)
        footprint = force_2d(footprint)
        if not footprint.is_valid:
            footprint = make_valid(footprint)
        footprint = polygonal(footprint)
        zone = polygonal(footprint.buffer(safety_margin_m).intersection(aoi)) if footprint is not None else None
        if zone is None:
            continue
        records.append({"building_id": building.building_id, "height_m": height,
                        "height_source": building.height_source,
                        "is_estimated": building.height_source == "imputed",
                        "threshold_m": flight_altitude_m, "flight_altitude_m": flight_altitude_m,
                        "safety_margin_m": safety_margin_m, "area_m2": float(zone.area),
                        "geometry": zone})
    columns = ["building_id", "height_m", "height_source", "is_estimated", "threshold_m",
               "flight_altitude_m", "safety_margin_m", "area_m2", "geometry"]
    return gpd.GeoDataFrame(records, columns=columns, geometry="geometry", crs=METRIC_CRS)
