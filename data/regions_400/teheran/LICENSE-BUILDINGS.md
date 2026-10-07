# Official building subset — 테헤란로

Source: **국토교통부 GIS건물통합정보**, distributed by [브이월드](https://www.vworld.kr/dtmk/dtmk_ntads_s002.do?svcCde=NA&dsId=18).
The user obtained `AL_D010_11_20260909.zip` from the official download page, reference date **2026-09-09**.
The province/city-wide archive is preserved locally and excluded from the repository.

The VWorld download page labels this data **CC BY** and links to [Creative Commons Attribution 2.0 Korea](https://creativecommons.org/licenses/by/2.0/kr/).
The corresponding [public-data catalog](https://www.data.go.kr/data/15083092/fileData.do) separately lists **공공누리 제1유형 (출처표시)**.
Preserve attribution and identify modifications when sharing this subset or derived maps.

> 건물: 국토교통부 GIS건물통합정보, 브이월드 제공, 자료 기준일 2026-09-09. 테헤란로 중심 400m 관심영역 추출 및 좌표 변환.

`buildings.gpkg` retains complete source footprints intersecting the 400 m square centered at longitude 127.05613098792654, latitude 37.50581494620291, transformed to EPSG:5179. Building IDs and original A16 height values are unchanged. The subset does not fill missing heights. Downstream experiments may apply explicitly labeled height assumptions separately and record their parameters; these are not official source measurements.

Center selection: 포스코센터 원본 GIS 건물 폴리곤의 EPSG:5179 무게중심. 400m 구역 GIS 높이 확인 124/148개(83.78%).
Selection reference: https://www.posco.com/homepage/docs/kor7/jsp/common/posco/s91a1000020c.jsp

`buildings.provenance.json` records the configured center-selection evidence, archive/subset hashes, CRS, selected fields and processing. A source attribute is not an independent survey of present-day conditions. OSM background data has its own notice; software is separate. No source-organization endorsement is implied.
