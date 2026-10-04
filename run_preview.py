"""VS Code F5: 강남역 중심 설정의 합성 400m·1km 모델링 예제를 실행합니다.

실제 GIS 건물이나 전파 시뮬레이션이 아닙니다. 기존 결과는 덮어쓰지 않습니다.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import webbrowser

from region_model.__main__ import main


def run():
    root = Path(__file__).resolve().parent
    stamp = datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d_%H%M%S_%f")
    output = root / "outputs" / f"gangnam_vscode_{stamp}"
    print("합성 모델링 예제: 같은 중심의 400 m × 400 m / 1 km × 1 km")
    print("실제 GIS 건물 데이터와 통신 계산은 포함되어 있지 않습니다.")
    result = main(["demo", "--out", str(output)])
    if result:
        return result
    for size in (400, 1000):
        preview = output / f"{size}m" / "preview.html"
        print(f"{size} m 미리보기: {preview}")
        webbrowser.open(preview.as_uri(), new=2)
    print("완료: 두 크기의 모델 생성 및 브라우저 열기 요청. 기존 결과는 보존했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
