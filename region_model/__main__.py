import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import pyogrio

from .core import (METRIC_CRS, LedgerResolver, export_model, prepare_buildings,
                   read_buildings, read_csv_strings, read_json, region_geometry)

ROOT = Path(__file__).resolve().parent.parent


def build_one(args):
    """Build one extent; paired builds share the same configured centre."""
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
        frame, source_info = read_buildings(args.buildings, schema, aoi)
        metadata["source_file_info"] = source_info
        resolver = LedgerResolver(read_csv_strings(args.ledger), read_csv_strings(args.matches)) if args.ledger else LedgerResolver()
        metadata["ledger_inputs"] = {"ledger": str(args.ledger), "matches": str(args.matches)} if args.ledger else None
        background = gpd.GeoDataFrame({"kind": [], "osm_id": []}, geometry=[], crs=METRIC_CRS)
        metadata["background_source"] = "not_requested"
        if args.osm or args.background:
            from .background import fetch_background, load_background
            background, bg_meta = (fetch_background(aoi, METRIC_CRS, ROOT/".cache/osmnx") if args.osm
                                   else load_background(args.background, METRIC_CRS, aoi))
            metadata["background_details"] = bg_meta
            metadata["background_source"] = "osm" if args.osm else "local_file"
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
        sub = commands.add_parser(name, help="합성 도형으로 동작 확인" if name == "demo" else "공식 건물 파일로 지도 생성")
        sub.add_argument("--region", default="gangnam", choices=list(read_json(ROOT/"config/regions.json")))
        sub.add_argument("--center", nargs=2, type=float, metavar=("LON", "LAT"), help="직접 지정할 중심 경도 위도")
        sub.add_argument("--size", type=int, choices=(400, 1000), help="한 크기만 생성할 때 지정. 생략하면 400m·1000m 모두 생성")
        sub.add_argument("--out", type=Path, required=True, help="비어 있는 출력 폴더")
        if name == "build":
            sub.add_argument("--buildings", type=Path, required=True)
            sub.add_argument("--schema", type=Path, required=True)
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
        if args.size is None:
            for size in (400, 1000):
                extent_args = argparse.Namespace(**vars(args))
                extent_args.size = size
                extent_args.out = args.out/f"{size}m"
                build_one(extent_args)
        else:
            build_one(args)
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
