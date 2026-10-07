# Official building subset — 여의도

Source: **국토교통부 GIS건물통합정보**, distributed by [브이월드](https://www.vworld.kr/dtmk/dtmk_ntads_s002.do?svcCde=NA&dsId=18).
The user obtained `AL_D010_11_20260909.zip` from the official download page, reference date **2026-09-09**.
The province/city-wide archive is preserved locally and excluded from the repository.

The VWorld download page labels this data **CC BY** and links to [Creative Commons Attribution 2.0 Korea](https://creativecommons.org/licenses/by/2.0/kr/).
The corresponding [public-data catalog](https://www.data.go.kr/data/15083092/fileData.do) separately lists **공공누리 제1유형 (출처표시)**.
Preserve attribution and identify modifications when sharing this subset or derived maps.

> 건물: 국토교통부 GIS건물통합정보, 브이월드 제공, 자료 기준일 2026-09-09. 여의도 중심 400m 관심영역 추출 및 좌표 변환.

`buildings.gpkg` retains complete source footprints intersecting the 400 m square centered at longitude 126.9266, latitude 37.526, transformed to EPSG:5179. Building IDs and original A16 height values are unchanged. The subset does not fill missing heights. Downstream experiments may apply explicitly labeled height assumptions separately and record their parameters; these are not official source measurements.

The center comes from a [shared-conversation recommendation](https://chatgpt.com/share/6ac5cd6e-38d0-83ee-b55d-a67da72d5ac5); it is not an independently verified landmark centroid. `buildings.provenance.json` records archive/subset hashes, CRS, selected fields and processing. A source attribute is not an independent survey of present-day conditions. OSM background data has its own notice; software is separate. No source-organization endorsement is implied.
