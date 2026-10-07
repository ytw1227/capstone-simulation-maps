"""Offline, source-verified 400 m map suite with explicit height readiness gates."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
from html import escape
import json
import math
from pathlib import Path
import re

import geopandas as gpd
from pyproj import CRS

from .__main__ import (_verify_portable_provenance, _verify_recorded_extent,
                       bind_schema_provenance)
from .background import load_background
from .core import (METRIC_CRS, LedgerResolver, export_model, identifier,
                   prepare_buildings, read_buildings, read_csv_strings,
                   read_json, region_geometry, valid_height, write_json)
from .experiment import apply_height_policy, derive_no_fly_zones

ROOT = Path(__file__).resolve().parents[1]
REGION_KEYS = {"gangnam", "yeouido", "hongdae", "pangyo", "bundang"}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def region_seed(seed, key):
    """Stable across processes and independent of dictionary/region order."""
    return (int(seed) + int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big")) % 2**32


def _check_bytes(record, path, label):
    if record.get("file") != path.name:
        raise ValueError(f"{label} 출처 기록의 파일명이 입력 파일과 다릅니다.")
    expected = record.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", str(expected)) or file_sha256(path) != expected:
        raise ValueError(f"{label} 파일 SHA256이 출처 기록과 다릅니다.")


def _check_bounds(record, field, aoi, label):
    recorded = record.get(field)
    if not isinstance(recorded, list) or len(recorded) != 4 or any(
        not math.isfinite(float(value)) or not math.isclose(float(value), expected, rel_tol=0, abs_tol=1e-6)
        for value, expected in zip(recorded, aoi.bounds)
    ):
        raise ValueError(f"{label} 투영 경계가 요청한 400 m 정사각형과 다릅니다.")


def read_bundle(key, region, *, project_root=ROOT):
    """Load only the exact official building and OSM snapshots bound to this AOI."""
    root = Path(project_root).resolve()
    directory = (root / region["data_dir"]).resolve()
    if not directory.is_relative_to(root):
        raise ValueError("지역 데이터 경로는 프로젝트 내부 상대 경로여야 합니다.")
    center = region["center_lonlat"]
    origin, aoi = region_geometry(*center, 400)
    buildings_record = read_json(directory / "buildings.provenance.json")
    background_record = read_json(directory / "background.provenance.json")
    for label, record, file_name, flag, bounds in (
        ("건물", buildings_record, "buildings.gpkg", "is_synthetic", "aoi_bounds_projected_m"),
        ("배경", background_record, "background.gpkg", "synthetic", "aoi_bounds_metric"),
    ):
        _verify_portable_provenance(record)
        _verify_recorded_extent(record, center, 400, label)
        _check_bounds(record, bounds, aoi, label)
        _check_bytes(record, directory / file_name, label)
        if record.get(flag) is not False or record.get("region_key") != key:
            raise ValueError(f"{label} 실제 자료 여부 또는 지역 식별자가 다릅니다.")
    if (buildings_record.get("source_name") != "국토교통부 GIS건물통합정보 (브이월드 AL_D010)"
            or not str(buildings_record.get("source_url", "")).startswith("https://www.vworld.kr/")
            or not re.fullmatch(r"[0-9a-f]{64}", str(buildings_record.get("original_file_sha256", "")))):
        raise ValueError("공식 GIS건물통합정보 원본 식별과 체크섬이 필요합니다.")
    if (background_record.get("source") != "OpenStreetMap via OSMnx"
            or background_record.get("buildings_used") is not False
            or background_record.get("status") != "ok"):
        raise ValueError("배경은 성공적으로 수집한 실제 OSM 배경이어야 합니다.")
    for notice in ("LICENSE-BUILDINGS.md", "LICENSE-OSM.md"):
        if not (directory / notice).is_file():
            raise ValueError(f"자료 출처·이용조건 파일이 없습니다: {notice}")
    provenance = {
        "dataset_id": f"{key}_actual_400", "is_synthetic": False,
        "center_lonlat": list(center), "size_m": 400,
        "buildings": buildings_record, "background": background_record,
        "schema_sha256": file_sha256(directory / "schema.json"),
    }
    schema = bind_schema_provenance(read_json(directory / "schema.json"), provenance)
    _verify_portable_provenance(schema)
    if schema.get("layer") != buildings_record.get("layer"):
        raise ValueError("건물 schema의 layer와 출처 기록이 다릅니다.")
    for field, expected in (("id_field", "A1"), ("height_field", "A16"), ("name_field", "A24"),
                            ("source_crs", METRIC_CRS), ("height_unit", "m"),
                            ("height_semantics", "above_ground")):
        if schema.get(field) != expected:
            raise ValueError(f"공식 AL_D010 묶음의 schema {field} 값이 검증된 필드 정의와 다릅니다.")
    if schema.get("record_reference_dates") != buildings_record.get("record_reference_dates"):
        raise ValueError("schema와 출처 기록의 건물 속성 기준일이 다릅니다.")
    frame, source_info = read_buildings(directory / "buildings.gpkg", schema, aoi)
    if len(frame) != buildings_record.get("row_count") or frame.empty:
        raise ValueError("건물 원본 개수 또는 400 m 공간 범위가 출처 기록과 다릅니다.")
    ids = frame[schema["id_field"]].map(identifier)
    if ids.eq("").any() or not ids.is_unique:
        raise ValueError("지역 묶음의 건물 식별번호가 누락되거나 중복됐습니다.")
    raw_background = gpd.read_file(directory / "background.gpkg", layer="background")
    if (not CRS(raw_background.crs).equals(CRS(METRIC_CRS))
            or not raw_background.geometry.is_valid.all()
            or raw_background.geometry.is_empty.any()
            or not raw_background.geometry.map(aoi.buffer(1e-7).covers).all()):
        raise ValueError("원본 배경 좌표계·기하·400 m 경계가 유효하지 않습니다.")
    background, bg_info = load_background(directory / "background.gpkg", METRIC_CRS, aoi)
    if bg_info["status"] != "ok" or len(background) != background_record.get("feature_count"):
        raise ValueError("배경 파일의 내용 또는 범위가 출처 기록과 다릅니다.")
    if not CRS(background.crs).equals(CRS(METRIC_CRS)):
        raise ValueError("배경은 미터 단위 EPSG:5179여야 합니다.")
    counts = {kind: int(background.kind.eq(kind).sum()) for kind in ("road", "park", "landuse", "water")}
    if counts != background_record.get("counts_by_kind"):
        raise ValueError("배경 요소 분류별 개수가 출처 기록과 다릅니다.")
    return directory, origin, aoi, frame, background, schema, provenance, source_info, bg_info


def _read_ledger_inputs(directory, key, region, building_hash):
    ledger_path, matches_path = directory / "ledger.csv", directory / "verified_matches.csv"
    audit_path = directory / "ledger_audit.json"
    if ledger_path.exists() != matches_path.exists():
        raise ValueError("대장 보완에는 ledger.csv와 verified_matches.csv가 모두 필요합니다.")
    audit = read_json(audit_path) if audit_path.is_file() else {"buildings": {}}
    if not isinstance(audit, dict) or not isinstance(audit.get("buildings"), dict):
        raise ValueError("ledger_audit.json에는 buildings 객체가 필요합니다.")
    _verify_portable_provenance(audit)
    for field, expected in (("region_key", key), ("center_lonlat", region["center_lonlat"]),
                            ("size_m", 400), ("building_source_sha256", building_hash)):
        if field in audit and audit[field] != expected:
            raise ValueError(f"대장 확인 기록의 {field} 값이 현재 지도와 다릅니다.")
    provenance = {"audit": audit,
                  "audit_file": audit_path.name if audit_path.exists() else None,
                  "audit_sha256": file_sha256(audit_path) if audit_path.exists() else None,
                  "ledger": None, "matches": None}
    for field, path in (("ledger_sha256", ledger_path), ("verified_matches_sha256", matches_path)):
        if field in audit:
            recorded_hash = audit[field]
            if (not path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", str(recorded_hash))
                    or file_sha256(path) != recorded_hash):
                raise ValueError(f"대장 확인 기록의 {field} 값이 현재 CSV SHA256과 다릅니다.")
    resolver = LedgerResolver()
    if ledger_path.is_file():
        resolver = LedgerResolver(read_csv_strings(ledger_path), read_csv_strings(matches_path))
        for kind, path in (("ledger", ledger_path), ("matches", matches_path)):
            provenance[kind] = {"file": path.name, "sha256": file_sha256(path)}
    return resolver, audit["buildings"], provenance


def _validate_adopted_ledger(buildings, audit):
    """Do not let an accepted CSV match bypass the recorded title review."""
    for _, building in buildings.loc[buildings.height_source.eq("ledger")].iterrows():
        record = audit.get(str(building.building_id), {})
        if record.get("status") != "verified_height":
            raise ValueError("채택한 대장 높이에 대응하는 verified_height 확인 기록이 필요합니다.")
        evidence = json.loads(building.identity_evidence)
        if "matched_ledger_id" in record and record["matched_ledger_id"] != evidence["ledger_id"]:
            raise ValueError("대장 확인 기록과 채택한 표제부 ledger_id가 다릅니다.")
        if "height_m" in record:
            recorded_height = valid_height(record["height_m"])
            if recorded_height is None or not math.isclose(recorded_height, float(building.height_m),
                                                           rel_tol=0, abs_tol=1e-8):
                raise ValueError("대장 확인 기록과 채택한 높이 값이 다릅니다.")


def _page(title, body):
    return f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eef2f7;color:#182436;font:16px/1.65 system-ui,"Malgun Gothic",sans-serif}}
main{{max-width:1140px;margin:52px auto;padding:0 24px}}h1{{font-size:32px;line-height:1.3}}h2{{margin:10px 0}}
.intro{{max-width:850px;color:#536174}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:20px;margin:32px 0}}
article,.notice{{background:white;border:1px solid #dce3ed;border-radius:16px;padding:24px}}.badge{{display:inline-block;border-radius:6px;padding:3px 9px;font-size:13px;background:#fff0d7;color:#7e5000}}
.ready{{background:#e1f3eb;color:#176542}}.held{{background:#ffe5e4;color:#9b3030}}.concept{{color:#536174}}a{{color:#2556a9}}.button{{display:inline-block;margin-top:12px;text-decoration:none;background:#2556a9;color:white;border-radius:8px;padding:9px 16px}}
dl{{display:grid;grid-template-columns:1fr auto;gap:4px 16px}}dt{{color:#657287}}dd{{margin:0}}small{{color:#657287}}footer{{padding:18px 0;font-size:13px;color:#657287}}
</style><main>{body}</main></html>'''


def _write_pending(output, buildings, quality, metadata, origin, aoi):
    output.mkdir(parents=True, exist_ok=False)
    counts = {
        "buildings": len(buildings),
        "gis_height": int(buildings.height_source.eq("gis").sum()),
        "ledger_height": int(buildings.height_source.eq("ledger").sum()),
        "missing_height": int(buildings.height_source.eq("missing").sum()),
        "imputed_height": 0,
    }
    manifest = {**metadata, "counts": counts, "created_utc": datetime.now(timezone.utc).isoformat(),
                "metric_crs": METRIC_CRS, "local_origin_projected_m": list(origin),
                "aoi_bounds_projected_m": list(aoi.bounds), "local_bounds": [-200, -200, 200, 200],
                "ground_z_m": 0, "mesh_complete": False, "simulation_ready": False,
                "purpose": "대장 확인 대기 또는 높이 확보 기준 미충족으로 모델 생성 보류"}
    write_json(output / "manifest.json", manifest)
    with (output / "quality.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        columns = sorted({key for row in quality for key in row})
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(quality)
    status = "대장 확인 대기" if metadata["height_policy"]["status"] == "pending_ledger" else "모델 생성 보류"
    reason = escape(metadata["height_policy"].get("reason", ""))
    explanation = ("대장 확인이 끝나기 전에는 추정 높이와 3D 장면을 생성하지 않습니다."
                   if metadata["height_policy"]["status"] == "pending_ledger"
                   else "높이 확보 기준을 충족하지 않아 추정 높이와 3D 장면을 생성하지 않습니다.")
    body = f'''<p><a href="../index.html">← 5개 지역 목록</a></p><h1>{escape(metadata['region_name'])} · {status}</h1>
<div class="notice"><p>{reason}</p><p>건물 {len(buildings)}개 · 확인 높이 {counts['gis_height'] + counts['ledger_height']}개 · 미확인 {counts['missing_height']}개</p>
<p>{explanation} 원본 GIS는 입력 폴더에 보존되어 있습니다.</p>
<a href="manifest.json">처리 기록</a> · <a href="quality.csv">건물별 품질 기록</a></div>'''
    (output / "preview.html").write_text(_page(f"{metadata['region_name']} · {status}", body), encoding="utf-8")
    return manifest


def build_region(key, region, output, *, project_root=ROOT, seed=20261007,
                 safety_margin_m=5.0, selection_source=None):
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", key):
        raise ValueError("지역 식별자는 안전한 영문 파일명이어야 합니다.")
    if not math.isfinite(safety_margin_m) or safety_margin_m < 0:
        raise ValueError("안전 여유는 유한한 0 이상의 미터 값이어야 합니다.")
    output = Path(output)
    if output.exists():
        raise ValueError("지역 출력 경로는 새 경로여야 합니다.")
    directory, origin, aoi, frame, background, schema, provenance, source_info, bg_info = read_bundle(
        key, region, project_root=project_root)
    resolver, audit, ledger_provenance = _read_ledger_inputs(directory, key, region, provenance["buildings"]["sha256"])
    buildings, quality = prepare_buildings(frame, schema, aoi, resolver)
    if buildings.empty:
        raise ValueError("지정한 400 m 영역 안에 유효한 건물 폴리곤이 없습니다.")
    _validate_adopted_ledger(buildings, audit)
    buildings, quality, policy = apply_height_policy(buildings, quality,
        seed=region_seed(seed, key), ledger_audit=audit, max_deviation_m=5.0)
    metadata = {
        "suite": True, "region_name": region["name"], "region_key": key,
        "environment": region["environment"], "center_lonlat": region["center_lonlat"],
        "center_status": "shared_conversation_candidate", "candidate_note": region.get("center_note", ""),
        "selection_source": selection_source, "size_m": 400, "is_demo": False,
        "dataset_provenance": provenance, "building_source": schema,
        "source_file_info": source_info, "background_source": "osm",
        "background_details": bg_info, "background_snapshot": provenance["background"],
        "ledger_inputs": ledger_provenance, "height_policy": policy, "base_seed": int(seed),
        "status": policy["status"],
        "flight_policy": {"flight_altitude_m": 50.0, "threshold_m": 50.0,
                          "safety_margin_m": float(safety_margin_m), "comparison": ">=",
                          "buffer": "horizontal", "ground_reference": "flat_ground_z_0_m",
                          "geometry_policy": "Buffer original full building footprints, then clip zones to AOI; building geometry stays unchanged.",
                          "meaning": "Experimental collision constraints, not regulated airspace."},
    }
    _verify_portable_provenance(metadata)
    if policy["status"] != "ready":
        return _write_pending(output, buildings, quality, metadata, origin, aoi)
    originals = {identifier(row[schema["id_field"]]): row.geometry for _, row in frame.iterrows()}
    zones = derive_no_fly_zones(buildings, aoi, flight_altitude_m=50.0,
                               safety_margin_m=safety_margin_m, original_footprints=originals)
    return export_model(output, buildings, background, quality, metadata, origin, aoi, no_fly_zones=zones)


def write_gallery(output, manifests, *, safety_margin_m):
    cards = []
    for manifest in manifests:
        policy, counts = manifest["height_policy"], manifest["counts"]
        status = policy["status"]
        label = {"ready": "맵 생성 완료", "held": "모델 생성 보류", "pending_ledger": "대장 확인 대기"}[status]
        reason = "" if status == "ready" else f"<p>{escape(policy.get('reason', ''))}</p>"
        confirmed = counts["gis_height"] + counts["ledger_height"]
        cards.append(f'''<article><span class="badge {status}">{label}</span><h2>{escape(manifest['region_name'])}</h2>
<p class="concept">{escape(manifest['environment'])} · 400 × 400 m</p><dl><dt>전체 건물</dt><dd>{counts['buildings']}개</dd>
<dt>확인된 높이</dt><dd>{confirmed}개</dd><dt>실험용 추정 높이</dt><dd>{counts.get('imputed_height', 0)}개</dd>
<dt>높이값 미배정</dt><dd>{counts['missing_height']}개</dd></dl>{reason}
<a class="button" href="{escape(manifest['region_key'])}/preview.html">{'지도 열기' if status == 'ready' else '처리 상태 보기'}</a></article>''')
    body = f'''<small>실제 GIS 건물 · OpenStreetMap 배경</small><h1>5개 지역 · 400 m × 400 m</h1>
<p class="intro">실제 건물 외곽선과 확인된 높이를 사용합니다. 대장 확인을 마친 미확인 높이는 지역 평균 ±5 m의 실험값으로 표시하며, 추정값 전체 평균은 확인 높이 평균과 같습니다. 미확인이 더 많으면 해당 지역은 보류합니다.</p>
<p class="intro">고정 비행 고도 50 m · 높이 50 m 이상 건물의 수평 안전 여유 {safety_margin_m:g} m. 비행 제한 영역은 실험용 충돌 제약이며, 통신 계산과 드론 경로 실행은 아직 포함하지 않습니다.</p>
<div class="grid">{''.join(cards)}</div><footer>건물: 국토교통부 GIS건물통합정보 / 브이월드 · 배경: © OpenStreetMap contributors (ODbL). 원본 높이와 추정 높이는 각각 기록합니다.</footer>'''
    (Path(output) / "index.html").write_text(_page("5개 지역 · 400 m 지도", body), encoding="utf-8")


def build_suite(config_path, output, *, project_root=ROOT, seed=20261007, safety_margin_m=5.0):
    config = read_json(config_path)
    _verify_portable_provenance(config)
    if config.get("size_m") != 400 or set(config.get("regions", {})) != REGION_KEYS:
        raise ValueError("5개 지정 지역과 400 m 크기가 포함된 설정이 필요합니다.")
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("출력 폴더는 새 경로여야 합니다. 기존 결과를 덮어쓰지 않습니다.")
    manifests = []
    for key, region in config["regions"].items():
        manifest = build_region(key, region, output / key, project_root=project_root,
                                seed=seed, safety_margin_m=safety_margin_m,
                                selection_source=config.get("selection_source"))
        manifests.append(manifest)
    summary = {"schema_version": 1, "size_m": 400, "base_seed": int(seed),
               "safety_margin_m": safety_margin_m, "config_sha256": file_sha256(config_path),
               "regions": [{key: manifest[key] for key in
                            ("region_key", "region_name", "environment", "center_lonlat", "status", "counts", "height_policy")}
                           for manifest in manifests]}
    write_json(output / "suite.manifest.json", summary)
    write_gallery(output, manifests, safety_margin_m=safety_margin_m)
    return summary
