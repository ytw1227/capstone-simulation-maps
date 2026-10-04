import argparse
import hashlib
import json
import math
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import geopandas as gpd
import pyogrio

from .core import (METRIC_CRS, LedgerResolver, export_model, prepare_buildings,
                   read_buildings, read_csv_strings, read_json, region_geometry)

ROOT = Path(__file__).resolve().parent.parent


def _verify_portable_provenance(value):
    """Reject machine paths or credential fields before embedding a shared record."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"api_key", "access_token", "token", "password", "secret", "authorization"}:
                raise ValueError("출처 기록에는 인증정보를 저장할 수 없습니다.")
            _verify_portable_provenance(item)
    elif isinstance(value, list):
        for item in value:
            _verify_portable_provenance(item)
    elif isinstance(value, str):
        if PureWindowsPath(value).is_absolute() or PurePosixPath(value).is_absolute() or value.startswith("file://"):
            raise ValueError("출처 기록에는 절대 로컬 경로 대신 파일명·상대 경로를 사용하세요.")


def _verify_recorded_extent(record, center, size_m, label):
    try:
        recorded_center = record["center_lonlat"]
        center_matches = len(recorded_center) == 2 and all(
            math.isfinite(float(recorded_center[index]))
            and math.isclose(float(recorded_center[index]), float(center[index]), rel_tol=0, abs_tol=1e-9)
            for index in range(2)
        )
    except (KeyError, TypeError, ValueError):
        center_matches = False
    if not center_matches or record.get("size_m") != size_m or size_m != 400:
        raise ValueError(f"{label} 출처 기록의 중심점·400m 범위가 요청한 지도와 일치하지 않습니다.")


def read_verified_provenance(path, center, size_m, buildings_path, background_path):
    """Bind the fixed actual-data bundle to its extent and exact input bytes."""
    provenance = read_json(path)
    if not isinstance(provenance, dict):
        raise ValueError("출처 기록은 JSON 객체여야 합니다.")
    if provenance.get("dataset_id") != "gangnam_actual_400" or provenance.get("is_synthetic") is not False:
        raise ValueError("실제 강남역 400m 자료의 출처 기록이 아닙니다.")
    _verify_recorded_extent(provenance, center, size_m, "패키지")
    if background_path is None:
        raise ValueError("출처 기록 검증에는 함께 저장된 --background 파일이 필요합니다. --osm과 함께 사용할 수 없습니다.")
    for key, file_path in (("buildings", buildings_path), ("background", background_path)):
        record = provenance.get(key)
        file_path = Path(file_path)
        if not isinstance(record, dict) or record.get("file") != file_path.name:
            raise ValueError(f"출처 기록의 {key} 파일명이 입력 파일과 일치하지 않습니다.")
        _verify_recorded_extent(record, center, size_m, key)
        synthetic_field = "is_synthetic" if key == "buildings" else "synthetic"
        if record.get(synthetic_field) is not False:
            raise ValueError(f"{key} 자료가 실제 데이터임을 명시한 출처 기록이 필요합니다.")
        expected = str(record.get("sha256", "")).lower()
        if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
            raise ValueError(f"출처 기록의 {key} SHA256 값이 유효하지 않습니다.")
        digest = hashlib.sha256()
        with file_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise ValueError(f"{key} 파일의 SHA256이 출처 기록과 다릅니다. 자료와 출처 기록을 함께 확인하세요.")
    _verify_portable_provenance(provenance)
    return provenance


def bind_schema_provenance(schema, provenance):
    """Use the checked source identity for both loading and the preview header."""
    source = provenance["buildings"]
    bound = dict(schema)
    for field in ("source_name", "source_url", "snapshot_date"):
        verified_value = source.get(field)
        if not isinstance(verified_value, str) or not verified_value.strip():
            raise ValueError(f"검증된 건물 출처 기록에 {field} 값이 없습니다.")
        if bound.get(field) and bound[field] != verified_value:
            raise ValueError(f"schema와 검증된 출처 기록의 {field} 값이 다릅니다. 같은 자료의 설정을 사용하세요.")
        bound[field] = verified_value
    return bound


def build_one(args):
    """Build one requested extent; real-data builds are limited to 400 m."""
    region = read_json(ROOT/"config/regions.json")[args.region]
    center = args.center or region["center_lonlat"]
    origin, aoi = region_geometry(*center, args.size)
    metadata = {"region_name": region["name"], "region_key": args.region, "environment": region["environment"],
                "center_lonlat": center, "size_m": args.size, "is_demo": args.command == "demo",
                "center_status": "user_specified" if args.center else "provisional_unverified_candidate",
                "candidate_note": region["candidate"]}
    if args.command == "demo":
        from .demo import make_demo
        frame, background, schema, resolver = make_demo(origin)
        background.geometry = background.geometry.intersection(aoi)
        background = background[~background.geometry.is_empty].copy()
        metadata.update({"region_name": "합성 검증용 도형 (실제 지역 아님)", "background_source": "synthetic"})
    else:
        if bool(args.ledger) != bool(args.matches):
            raise ValueError("대장 보완은 --ledger와 --matches를 함께 지정해야 합니다.")
        schema = read_json(args.schema)
        provenance = None
        if args.provenance:
            provenance = read_verified_provenance(args.provenance, center, args.size, args.buildings, args.background)
            schema = bind_schema_provenance(schema, provenance)
            metadata["dataset_provenance"] = provenance
        frame, source_info = read_buildings(args.buildings, schema, aoi)
        metadata["source_file_info"] = source_info
        resolver = LedgerResolver(read_csv_strings(args.ledger), read_csv_strings(args.matches)) if args.ledger else LedgerResolver()
        metadata["ledger_inputs"] = {"ledger": args.ledger.name, "matches": args.matches.name} if args.ledger else None
        background = gpd.GeoDataFrame({"kind": [], "osm_id": []}, geometry=[], crs=METRIC_CRS)
        metadata["background_source"] = "not_requested"
        if args.osm or args.background:
            from .background import fetch_background, load_background
            background, bg_meta = (fetch_background(aoi, METRIC_CRS, ROOT/".cache/osmnx") if args.osm
                                   else load_background(args.background, METRIC_CRS, aoi))
            metadata["background_details"] = bg_meta
            metadata["background_source"] = "osm" if args.osm else "local_file"
            if provenance:
                snapshot = provenance["background"]
                metadata["background_snapshot"] = snapshot
                if (snapshot.get("source") == "OpenStreetMap via OSMnx"
                        and snapshot.get("synthetic") is False
                        and snapshot.get("buildings_used") is False
                        and snapshot.get("status") == "ok"):
                    metadata["background_source"] = "osm"
            if bg_meta["status"] == "error":
                raise ValueError(f"배경 수집 실패: {bg_meta}. 배경 없이 먼저 만들려면 --osm을 빼세요.")
    buildings, quality = prepare_buildings(frame, schema, aoi, resolver)
    if buildings.empty:
        raise ValueError("영역 안에 건물 폴리곤이 없습니다. 중심점, 원본 범위, CRS를 확인하세요.")
    metadata["building_source"] = schema
    manifest = export_model(args.out, buildings, background, quality, metadata, origin, aoi)
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))
    print(f"Preview ({args.size} m): {(args.out/'preview.html').resolve()}")
    if metadata["is_demo"]:
        print("SYNTHETIC DEMO: 실제 지역/건물이 아닙니다.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="실제 건물 외곽선 + 높이의 평면 지반 3D 블록 시제품")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("regions", help="임시 지역 중심점 보기")
    inspect = commands.add_parser("inspect", help="GIS 파일 CRS·필드·샘플 확인")
    inspect.add_argument("file", type=Path)
    inspect.add_argument("--layer")
    inspect.add_argument("--encoding")
    for name in ("demo", "build"):
        sub = commands.add_parser(name, help="개발 검증용 합성 도형 (실제 지역 아님)" if name == "demo" else "공식 건물 파일로 400m 지도 생성")
        sub.add_argument("--region", default="gangnam", choices=list(read_json(ROOT/"config/regions.json")))
        sub.add_argument("--center", nargs=2, type=float, metavar=("LON", "LAT"), help="직접 지정할 중심 경도 위도")
        sub.add_argument(
            "--size", type=int, choices=(400,) if name == "build" else (400, 1000), default=400,
            help="실제 모델 한 변: 400m (기본값)" if name == "build" else "검증용 한 변: 400 또는 1000m (기본값: 400)",
        )
        sub.add_argument("--out", type=Path, required=True, help="비어 있는 출력 폴더")
        if name == "build":
            sub.add_argument("--buildings", type=Path, required=True)
            sub.add_argument("--schema", type=Path, required=True)
            sub.add_argument("--provenance", type=Path, help="실제 자료 패키지 출처 기록: 범위·파일명·SHA256 검증")
            sub.add_argument("--ledger", type=Path)
            sub.add_argument("--matches", type=Path)
            bg = sub.add_mutually_exclusive_group()
            bg.add_argument("--osm", action="store_true", help="OSMnx로 도로·공원·토지이용 다운로드")
            bg.add_argument("--background", type=Path, help="기존 배경 GPKG/GeoJSON (geometry, kind 필요)")
    args = parser.parse_args(argv)
    try:
        if args.command == "regions":
            print(json.dumps(read_json(ROOT/"config/regions.json"), ensure_ascii=False, indent=2))
            return 0
        if args.command == "inspect":
            options = {k: getattr(args, k) for k in ("layer", "encoding") if getattr(args, k)}
            info = pyogrio.read_info(args.file, **options)
            print(json.dumps({k: str(info.get(k)) for k in ("crs", "fields", "dtypes", "features", "geometry_type", "total_bounds")}, ensure_ascii=False, indent=2))
            print(gpd.read_file(args.file, rows=3, **options).drop(columns="geometry").to_string(index=False))
            return 0
        if args.out.exists() and any(args.out.iterdir()):
            raise ValueError("출력 폴더가 비어 있지 않습니다. --out에 새 경로를 지정하세요.")
        build_one(args)
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
