"""VS Code F5: build five actual 400 m study-area maps from verified local data."""
from datetime import datetime, timedelta, timezone
import argparse
from pathlib import Path
import sys
import webbrowser

from region_model.suite import build_suite

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "regions.five.json",
                        help="지역 설정 경로. 기본값은 확정된 5개 구역이며 이전 설정도 명시적으로 실행할 수 있습니다.")
    parser.add_argument("--safety-margin", type=float, default=5.0, metavar="METRES")
    parser.add_argument("--seed", type=int, default=20261007)
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d_%H%M%S_%f")
    output = (args.out or ROOT / "outputs" / f"five_actual400_{stamp}").resolve()
    try:
        summary = build_suite(args.config.resolve(), output,
                              seed=args.seed, safety_margin_m=args.safety_margin)
    except (ValueError, OSError, RuntimeError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    for region in summary["regions"]:
        counts = region["counts"]
        print(f"{region['region_name']}: {region['status']} · 건물 {counts['buildings']}개 · 확인 {counts['gis_height'] + counts['ledger_height']}개 · 추정 {counts['imputed_height']}개")
    print(f"지역별 결과: {output / 'index.html'}")
    if not args.no_browser:
        webbrowser.open((output / "index.html").as_uri(), new=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
