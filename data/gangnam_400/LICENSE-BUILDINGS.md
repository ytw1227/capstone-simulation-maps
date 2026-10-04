# Official building subset

Source: **국토교통부 GIS건물통합정보**, distributed by [브이월드](https://www.vworld.kr/dtmk/dtmk_ntads_s002.do?svcCde=NA&dsId=18).

The original Seoul dataset is `AL_D010_11_20260909.zip`, with the published reference date **2026-09-09**. The user downloaded this file from the official VWorld download page. The original city-wide archive is preserved locally and is not included in this repository.

The VWorld download page labels this dataset **CC BY** and links to [Creative Commons Attribution 2.0 Korea](https://creativecommons.org/licenses/by/2.0/kr/). The corresponding [public-data catalog](https://www.data.go.kr/data/15083092/fileData.do) separately lists **공공누리 제1유형 (출처표시)**. Preserve source attribution and identify modifications when sharing this subset or maps made from it.

Suggested attribution:

> 건물: 국토교통부 GIS건물통합정보, 브이월드 제공, 자료 기준일 2026-09-09. 강남역 중심 400m 관심영역 추출 및 좌표 변환.

`buildings.gpkg` contains source records intersecting the fixed 400 m square around longitude 127.0276, latitude 37.4979. Coordinates are transformed to EPSG:5179; complete intersecting source footprints, building identifiers and original height values are retained. Model generation clips the footprints to the square, repairs invalid geometry when necessary with a quality flag, and extrudes only valid source heights. Missing heights are not estimated.

`buildings.provenance.json` records the archive and subset SHA256 values, original CRS, selected fields, processing steps and quality counts. `provenance.json` binds the building and OSM files used together. A source attribute is not an independent survey of present-day conditions.

This notice applies to the official building subset. OSM background data is covered by `LICENSE-OSM.md`; software is separate from both datasets. No endorsement by the source organizations is implied.
