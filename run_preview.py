"""VS Code F5: prepared official Gangnam data, fixed 400 m square only.

Missing real inputs stop execution. Synthetic fixtures are never a fallback.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import webbrowser

from region_model.__main__ import main

ROOT = Path(__file__).resolve().parent
INPUT_FILES = ("buildings.gpkg", "background.gpkg", "schema.json", "provenance.json")


def run():
    data_dir = ROOT / "data" / "gangnam_400"
    missing = [name for name in INPUT_FILES if not (data_dir / name).is_file()]
    if missing:
        print("실제 강남역 400 m × 400 m 입력 자료가 준비되지 않았습니다.", file=sys.stderr)
        print(f"필요한 위치: {data_dir}", file=sys.stderr)
        print(f"누락 파일: {', '.join(missing)}", file=sys.stderr)
        print("공식 GIS 원본을 검증하여 자료와 출처 기록을 준비한 뒤 다시 실행하세요. 합성 데이터로 대체하지 않습니다.", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d_%H%M%S_%f")
    output = ROOT / "outputs" / f"gangnam_actual400_{stamp}"
    print("공식 GIS 입력으로 강남역 400 m × 400 m 모델을 생성합니다.")
    print("확인된 높이만 입체화하며, 높이 미확인은 외곽선과 품질 기록에 남깁니다.")
    result = main([
        "build", "--region", "gangnam", "--size", "400",
        "--buildings", str(data_dir / "buildings.gpkg"),
        "--schema", str(data_dir / "schema.json"),
        "--provenance", str(data_dir / "provenance.json"),
        "--background", str(data_dir / "background.gpkg"),
        "--out", str(output),
    ])
    if result:
        return result
    preview = output / "preview.html"
    print(f"400 m 미리보기: {preview}")
    webbrowser.open(preview.as_uri(), new=2)
    print("완료: 400 m 모델 생성 및 브라우저 열기 요청. 높이 확보 상태는 화면과 quality.csv에서 확인하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
