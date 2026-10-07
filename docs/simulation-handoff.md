# 통신 시뮬레이션 담당자 인계

**지도 데이터와 재생성 코드는 인계할 수 있는 상태입니다. 통신 계산기와 드론 이동·복구 알고리즘은 아직 구현하지 않았습니다.** 이 문서는 `five_actual400_20261007_confirmed` 출력과 현재 Python 내보내기 코드를 기준으로 데이터 연결 방법을 설명합니다.

코드 저장소: [캡스톤디자인 · 시뮬레이션 지도 모델링](https://github.com/ytw1227/capstone-simulation-maps).

## 1. 인계 범위와 현재 상태

| 지역 키 | 화면 이름 | 건물 수 | GIS 확인 높이 | 실험용 추정 높이 | 비행 제한 영역: 확인 / 추정 |
|---|---|---:|---:|---:|---:|
| `gangnam` | 강남역 | 100 | 63 | 37 | 30 / 37 |
| `teheran` | 테헤란로 | 148 | 124 | 24 | 25 / 0 |
| `hongdae` | 홍대 | 513 | 341 | 172 | 0 / 0 |
| `bundang` | 분당 | 60 | 45 | 15 | 13 / 0 |
| `sangam_dmc` | 상암 DMC | 38 | 26 | 12 | 20 / 12 |

합계 859개 중 GIS 높이 599개, 실험용 추정 높이 260개입니다. 대장 검토 후 추가 채택한 높이는 0개이며, 검토 미완료 건물은 없습니다. 다섯 지역 모두 `status=ready`, `mesh_complete=true`, `all_heights_verified=false`, `simulation_ready=false`입니다.

- `ready`: 지도 생성의 자료·대장 확인 조건을 통과했습니다.
- `mesh_complete`: 이 입력 집합의 모든 건물에 모델 높이가 있어 부피를 만들었습니다. 실제 높이 확인 완료나 현장 건물의 완전 수집을 뜻하지 않습니다.
- `all_heights_verified=false`: 실험용 추정 높이를 포함합니다.
- `simulation_ready=false`: 전파·간섭·통신단절 계산기가 포함되지 않았습니다.

후속 담당자는 **건물 형상·높이를 읽고 시뮬레이션 모듈을 연결**하면 됩니다. `preview.html`은 모델 확인용 웹 화면이며, 그 화면 자체가 통신 시뮬레이터는 아닙니다.

## 2. 재생성하고 전달할 파일

Python 3.13, Windows PowerShell 기준으로 저장소 루트에서 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_five_maps.py --no-browser --out outputs/handoff_400 --seed 20261007 --safety-margin 5
```

`outputs/handoff_400`은 새 폴더여야 합니다. 기존 폴더를 덮어쓰지 않습니다. 기본 설정은 `config/regions.five.json`이며, 저장소의 지역별 입력 묶음을 검증해 오프라인으로 생성합니다. 이 빌드에는 서울·경기 전체 ZIP이나 새 건축HUB 로그인·OSM 다운로드가 필요하지 않습니다. 의존성 설치는 별도입니다.

| 파일 | 담당자가 사용할 내용 |
|---|---|
| `suite.manifest.json` | 5개 지역 키·중심·집계·처리 상태 |
| `<region>/scene.local.json` | **주 입력 권장**: 로컬 m 좌표 건물·높이·출처·비행 제한 영역·전체 메타데이터 |
| `<region>/no_fly.local.json` | 같은 지역의 비행 제한 영역만 분리한 파일. `scene.local.json`의 `no_fly_zones`와 같은 자료 |
| `<region>/model.gpkg` | EPSG:5179 건물·AOI·OSM 배경·비행 제한 영역을 GIS 도구로 확인할 때 사용 |
| `<region>/manifest.json` | 해당 지역 메타데이터. 원점·범위·자료 해시·정책·집계 |
| `<region>/quality.csv` | 원본 높이, 동일 건물 연결, 추정 편차, 경계 절단 등 건물별 품질 기록 |
| `<region>/buildings_model_heights.obj` | **모든 모델 건물**의 3D 메시. 확인 높이와 추정 높이 모두 포함 |
| `<region>/buildings_known_heights.obj` | 확인 높이 건물만 포함하는 부분 메시. 추정 건물이 빠져 있으므로 전체 차폐 장면으로 사용하지 않음 |
| `<region>/preview.html` | 인터넷 없이 여는 입체·평면 확인 화면 |

코드 인계는 저장소와 이 문서로, 산출물 인계는 새로 생성한 출력 폴더 전체로 진행합니다. `outputs/`는 Git에서 제외되므로 저장소를 복제하면 먼저 재생성해야 합니다. 실험 결과에는 사용한 Git 커밋, 설정, 난수 시드, 지역 manifest와 입력 체크섬을 함께 보관합니다.

완성 출력과 문서·이용조건·소스 커밋을 포함한 인계 ZIP을 받은 경우에는 그 안의 지역별 `scene.local.json`부터 읽으면 됩니다. 코드를 수정하거나 설정을 바꾸어 다시 만들 때 위 실행 절차를 사용합니다.

## 3. 좌표와 지도 경계

각 지역은 **서로 독립된 400 m × 400 m 지도**입니다. 면적은 160,000㎡이며 지면은 `z=0`인 평면입니다.

- `center_lonlat` 순서는 **경도, 위도**입니다.
- `model.gpkg`의 평면 좌표계는 **EPSG:5179**, 단위는 m입니다.
- 로컬 원점은 `manifest.local_origin_projected_m = [origin_x, origin_y]`입니다.
- `local_x = EPSG5179_easting - origin_x`, `local_y = EPSG5179_northing - origin_y`입니다.
- 로컬 +x는 동쪽, +y는 북쪽, +z는 평면 지면 위쪽입니다. x·y 범위는 각각 `[-200, 200]`이며 z도 m입니다.

로컬 JSON은 **경위도 GeoJSON이 아닙니다**. 형상 표기는 GeoJSON과 같은 `type`·`coordinates` 구조이지만 좌표 값은 로컬 미터입니다. 여기에 EPSG:4326이나 EPSG:5179를 그대로 붙이지 않습니다. 서로 다른 지역의 `(0, 0)`도 같은 지리적 위치가 아닙니다.

원본과 겹치는 건물을 포함하고, 모델 형상은 AOI 경계에서 자릅니다. 입력 `data/regions_400/<region>/buildings.gpkg`에는 전체 외곽선이 남아 있습니다. **지도 밖 건물·외부 간섭원의 영향은 현재 실험에서 제외**합니다. 드론이 지도 밖으로 이동했을 때 계속 계산할지, 제한할지, 실험을 종료할지는 후속 이동 코드에서 정해야 합니다.

## 4. 건물 레코드와 높이

`scene.local.json`의 최상위 키는 `format`, `metadata`, `buildings`, `no_fly_zones`입니다. `buildings` 배열의 주요 필드는 다음과 같습니다.

| 필드 | 의미와 처리 |
|---|---|
| `building_id` | 건물 식별자 **문자열**. 숫자로 바꾸지 않음. 지역 내 건물·제한 영역·품질 기록 연결에 사용 |
| `geometry_local_m` | 2D 건물 바닥 외곽선. 실제 부피는 이 형상을 `z=0`부터 `height_m`까지 세운 것 |
| `height_m` | 현재 실험에서 사용할 모델 높이(m). 추정값을 포함할 수 있음 |
| `height_source` | `gis`, `ledger`, `imputed`, `missing` 중 하나 |
| `observed_height_m` | GIS 또는 동일 건물로 검증한 대장 높이. 추정 건물에서는 `null` |
| `height_source_original` | 추정 전 출처. 추정 건물은 `missing` |
| `imputation_delta_m` | 해당 지역 확인 높이 평균에서 더하거나 뺀 편차. 확인 높이 건물은 `null` |
| `ledger_check_status` | 대장 검토 결과. 조회 완료와 높이 채택 완료를 구분하는 기록 |
| `quality_flags` | 세미콜론으로 구분한 처리 기록. 예: `clipped_at_aoi_boundary`, `missing_or_invalid_gis_height`, `experimental_height_imputation` |
| `building_name`, `name_source` | 참고 속성. 이름이 비어 있을 수 있으며 화면 이름표는 표시하지 않음 |

`model.gpkg`의 `buildings`와 `quality.csv`에는 `source_building_id`, `gis_height_raw`, `match_status`, `identity_evidence`, `area_m2`도 있습니다. 이 필드들이 모두 로컬 JSON에 들어 있다고 가정하지 않습니다. 여러 지역을 한 번에 관리할 때는 `(region_key, building_id)`를 객체 키로 사용하면 구역별 결과를 분리할 수 있습니다.

높이 정책은 **원본 GIS → 동일 건물로 검증한 개별 대장 → 명시적 실험용 추정** 순서입니다. 대장 조회가 미완료이면 모델을 생성하지 않으며, 검토 후에도 미확인 수가 확인 수보다 많으면 보류합니다.

추정값은 해당 구역의 확인 높이 산술평균 `μ`의 ±5m 안에서 배정하고, 편차 합을 0으로 맞춰 추정값 평균도 `μ`로 유지합니다. 지역별 시드는 `metadata.height_policy.seed`에 기록합니다. **시뮬레이터는 전달된 `height_m`을 그대로 읽으며 실행할 때마다 재추정하지 않습니다.** 추정값 민감도 실험을 수행하려면 변경 시드를 기록해 별도의 맵을 다시 생성합니다. 평균이 매우 낮은 경우 양수 높이를 유지하도록 편차 폭을 줄이는 정책도 기록됩니다.

현재 모델의 `missing_height=0`은 미확인 원본 높이가 전부 실측됐다는 뜻이 아닙니다. 추정 260개를 결과 분석에서도 구분해야 합니다. 향후 `height_source=missing` 또는 `height_m=null`인 장면을 받으면 이를 0m나 자유공간으로 조용히 처리하지 말고 미정 상태로 처리해야 합니다.

## 5. 폴리곤과 메시의 의미

`geometry_local_m.type`은 `Polygon` 또는 `MultiPolygon`일 수 있습니다. `Polygon.coordinates`의 첫 번째 링은 외곽, 뒤의 링들은 중정·빈 공간입니다. `MultiPolygon`에서는 이 구조가 여러 부분으로 반복됩니다. 현재 강남·홍대 출력에는 실제로 `MultiPolygon` 건물이 있습니다.

모든 부분과 내부 빈 공간을 유지합니다. 첫 외곽선만 읽거나 경계 상자·볼록껍질로 바꾸면 원래의 차폐 형상이 바뀝니다. 하나의 `building_id`가 여러 폴리곤 부분을 갖더라도 한 건물이며, OBJ에는 `part_1`, `part_2`처럼 여러 객체 부분으로 나갈 수 있습니다. OBJ 객체 부분 수와 건물 수를 혼동하지 않습니다.

건물은 일정한 높이를 갖는 수직 블록입니다. 창문·재료·층별 돌출·정교한 지붕은 제공하지 않습니다. 배경을 제외한 건물 부피는 `geometry_local_m × [0, height_m]`으로 해석합니다. 메시를 사용할 때도 원본 JSON을 함께 읽어 건물 ID와 높이 출처를 유지합니다.

## 6. 고도 50m 비행 제약

현재 `flight_policy`는 지면 기준 고도 50m, 판정 `height_m >= 50`, 수평 안전 여유 5m입니다. **전체 원본 외곽선을 5m 확장한 뒤 AOI로 자른 별도 폴리곤**이며, 원래 건물 메시를 확장한 것이 아닙니다.

`no_fly.local.json`의 최상위 키는 `format`, `flight_policy`, `zones`입니다. 각 영역은 `building_id`, `height_m`, `height_source`, `is_estimated`, `flight_altitude_m`, `threshold_m`, `safety_margin_m`, `geometry_local_m`을 갖습니다. 이 파일에는 원점·전체 지도 메타데이터가 없으므로 **같은 지역의 manifest 또는 scene과 함께** 읽습니다.

제한 영역은 건물별로 저장되어 서로 겹칠 수 있습니다. 충돌 검사에서는 각 영역을 검사하거나 합집합을 사용할 수 있지만, 분석용 원본의 건물별 ID·확인/추정 구분은 보존합니다. `is_estimated=true`는 실제 높이를 확인해서 제한한 건물이 아니라 추정 높이로 분류한 영역입니다.

홍대는 해당 기준의 영역이 없어 JSON 배열이 비어 있으며, `model.gpkg`에는 `no_fly_zones` 레이어가 생성되지 않습니다. 이를 읽기 오류로 처리하지 않습니다. 다른 지역도 향후 설정에 따라 0개가 될 수 있습니다.

후속 경로 코드는 드론 위치뿐 아니라 **연속된 위치 사이 이동 구간**의 침범도 검사해야 합니다. 영역 경계에 닿는 경우의 판정과 수치 허용오차를 명시합니다. 5m가 이미 반영돼 있으므로 의도 없이 다시 5m 버퍼를 적용하지 않습니다. 기체 크기·위치 오차·추가 여유를 따로 사용할 경우 그 의미를 설정에 기록합니다.

이 영역은 **실험용 장애물 회피 제약**입니다. 법정 비행 금지구역 자료는 아닙니다. 통신 차폐 계산에는 이 확장 영역이 아니라 **원래 건물 폴리곤과 높이**를 사용해야 합니다. 비행 고도나 안전 여유를 바꾸면 제한 영역도 다시 생성해야 하며, 현재 CLI는 안전 여유만 `--safety-margin`으로 받습니다. 고도 50m는 `region_model/suite.py`의 설정입니다.

## 7. 배경·화면과 계산 데이터 구분

`model.gpkg`의 `background`는 도로·보행로 중심선, 토지이용·공원·수계의 선 또는 면 형상입니다. `kind`와 `osm_id`를 보존하고, `highway`, `landuse`, `tunnel`, `layer` 등 원본에 있는 태그만 지역별로 가집니다. 모든 지역에 같은 선택 열이 존재한다고 가정하지 않습니다. 이 배경은 `scene.local.json`에 별도 배열로 내보내지 않습니다.

도로 폭·지표 고도·수목 높이·재료별 전파 손실은 제공하지 않습니다. `tunnel` 태그가 있는 요소는 화면의 지표면에서 숨기지만 저장 데이터에는 남깁니다. OSM `layer` 값을 실제 고도(m)로 해석하지 않습니다.

화면의 배경·비행 제한 면에 적용한 작은 z 오프셋은 겹침을 방지하는 표시 효과입니다. 물리적 높이나 전파 장애물로 가져오면 안 됩니다. 색상과 카메라 설정 역시 계산 속성이 아닙니다.

## 8. 통신·드론 담당자가 구현할 부분

| 구현할 항목 | 이 저장소와의 연결 |
|---|---|
| 드론 수·포메이션·초기 위치·이동 | 같은 지역 로컬 m 좌표로 생성하고 고도·AOI·비행 제한을 적용 |
| 기하학적 LoS/NLoS | 두 드론의 3D 선분과 원래 건물 부피의 교차 검사. 내부 빈 공간·다중 부분·경계 접촉 규칙 포함 |
| 채널·경로손실 | 선정한 모델의 주파수·거리·고도·드론 간 링크 적용 범위를 검토하고 LoS/NLoS 결과를 입력 |
| 간섭·잡음 | 같은 자원을 쓰는 동시 송신 드론, 송신 스케줄, 링크별 수신전력, 잡음 계산을 정의 |
| 통신 성공·단절 | SINR 등 판정 지표, 임계값, 시간 조건, 패킷 손실·지연과의 관계를 정의 |
| 예측·재배치·복구 | 시간에 따른 링크 상태와 이동 가능한 영역을 이용해 알고리즘 구현 |
| 결과 시각화 | 계산한 드론 위치·링크 상태·경로를 웹 지도에 추가 연결. 현재는 이 연결도 구현하지 않음 |

실험 설정에는 최소한 지역 키·맵 버전, 시드, 드론 수와 포메이션, 위치·고도·속도·시간 간격, 주파수·대역폭·채널 공유 방식, 송신 출력·안테나, 채널 모델과 추가 손실·잡음 가정, 단절 판정 기준을 기록합니다. 건물별 재료·투과 손실·반사·회절·페이딩 값은 현재 지도에 없으므로 선택한 모델과 설정에서 명시해야 합니다.

## 9. 인계 후 첫 확인

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

새 실행의 manifest가 선택한 지역·400m 범위·시드·비행 정책과 일치하는지 확인합니다. 로더에서는 모든 건물 ID와 Polygon/MultiPolygon 부분을 유지하고, 전체 모델 건물 수가 표와 일치하는지 확인합니다. 그다음 장애물 없는 선분, 건물을 가로지르는 선분, 지붕·벽 접촉, 중정, 경계 건물, 비행 제한 영역을 통과하는 이동 구간으로 후속 계산기의 판정을 검증하면 됩니다.

자료 수집·대장 연결 근거와 보류 조건의 상세 내용은 [데이터 파이프라인](data-pipeline.md), 실행 개요는 [README](../README.md)를 참고합니다. 생성 규약의 구현은 [core.py](../region_model/core.py), 높이·비행 제약은 [experiment.py](../region_model/experiment.py), 실행 게이트는 [suite.py](../region_model/suite.py)에 있습니다.
