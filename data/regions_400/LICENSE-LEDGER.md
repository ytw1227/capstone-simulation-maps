# Building-title lookup source and reuse notice

Provider: **국토교통부**, through the [건축HUB 건축물대장 표제부 public search and export](https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-ttlldr.do).

The [official public-data catalog entry for 건축물대장 표제부](https://www.data.go.kr/data/15044720/fileData.do) links to this same HUB page and states **이용허락범위 제한 없음** (no restriction on the permitted scope of use). The [건축HUB 건축물대장정보 service catalog](https://www.data.go.kr/data/15134735/openapi.do) states the same permission. These descriptions were checked on 2026-10-07. This notice documents the source conditions; it does not relicense the source as this repository's software.

Attribution used for this research:

> 높이 보완 조회: 국토교통부 건축HUB 건축물대장 표제부, 2026년 8월 자료. 법정동별 공개 JSON 내보내기 후, 동일 건물 여부와 높이 제공 여부를 검토.

The acquired HUB exports represent **August 2026**, whereas the GIS building archive was distributed on **2026-09-09**. Their reference periods differ. A later retrieval date does not make an older source snapshot a later measurement. No independent survey of present-day building conditions is claimed.

The complete legal-neighborhood exports remain in the ignored local `data/raw/building_hub/` directory. Published regional artifacts contain the lookup audit and any verified height crosswalk needed for the selected 400 m square, rather than the complete neighborhood exports. Each regional `ledger_audit.json` records the original export filename, SHA256, source month, legal-neighborhood code, public-search total and exported row count. Export completeness is checked by comparing those two counts and the registered source hash. Missing exports remain `pending_lookup`; they are not treated as completed checks.

`BDRG_SN` is the HUB export's **관리건축물대장PK**, and `HG` is its **높이(m)** field. The original PK strings are preserved. GIS `A19` is separately defined as **건축물ID**. No relationship between the two identifiers is assumed. The API catalog warns that HUB migration changed building-data PKs; the generic migration notice alone does not establish a GIS A19 crosswalk.

Identity checks use exact PNU plus corroborating building attributes. PNU identifies a parcel and is never sufficient on its own. The GIS-side uniqueness check includes all buildings on each relevant parcel in the full original archive, including buildings outside the selected square. The audit records the candidate title IDs, exact matching evidence and reasons for rejection. Valid original GIS heights retain priority. A matched title's zero or absent height remains missing, rather than becoming a zero-metre building.

`no_title_found`, `identity_ambiguous` and `identity_unconfirmed` describe the result of this bounded lookup and conservative matching procedure. **They do not mean that a building has no height, that no record exists anywhere, or that the building is absent.** A changed parcel, source-period difference, incomplete identifying attributes or another unresolved association can prevent a match. `title_height_missing` means a corroborated same-building title did not supply a positive finite height in this source snapshot.

Any subsequent regional-mean height imputation is an explicitly labeled research assumption created by the model, not an official GIS or title-register observation. It is recorded separately and does not alter the source files. No endorsement by the source organizations is implied.
