"""Audit official building-title exports before any experimental height filling.

Only exact parcel + corroborated, one-building identity evidence is accepted.
A19 is never transformed into a HUB key. Full-source GIS parcel context prevents
a clipped AOI from falsely making a multi-building parcel look one-to-one.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd

from region_model.core import valid_height
from scripts.import_official_gangnam import sha256

SOURCE_URL = "https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-ttlldr.do"
NAMESPACE = "building_hub_202608"
FIELDS = {
    "HG": "높이(m)", "BDRG_SN": "관리건축물대장PK", "DNG_NM": "동명칭",
    "BLDG_NM": "건물명", "BDAR": "건축면적(㎡)", "GFA": "연면적(㎡)",
    "GRND_NOFL": "지상층수", "USE_APRV_DAY": "사용승인일",
    "SGG_CD": "시군구코드", "STDG_CD": "법정동코드", "PLOT_SE_CD": "대지구분코드",
    "MNO": "번", "SNO": "지", "LDGR_KIND_CD": "대장종류코드",
}


def text(value):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def date_key(value):
    value = text(value).split("T")[0].replace("-", "")
    if not re.fullmatch(r"\d{8}", value):
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        return None


def title_pnu(row):
    """HUB plot 0=ordinary,1=mountain; PNU digit 1=ordinary,2=mountain."""
    sgg, dong = text(row.get("SGG_CD")), text(row.get("STDG_CD"))
    land = {"0": "1", "1": "2"}.get(text(row.get("PLOT_SE_CD")))
    main, sub = text(row.get("MNO")), text(row.get("SNO"))
    if not (re.fullmatch(r"\d{5}", sgg) and re.fullmatch(r"\d{5}", dong)
            and land and re.fullmatch(r"\d{1,4}", main) and re.fullmatch(r"\d{1,4}", sub)):
        return None
    return sgg + dong + land + main.zfill(4) + sub.zfill(4)


def numeric_pair(left, right, *, area=False):
    left, right = valid_height(left), valid_height(right)
    if left is None or right is None:
        return None
    return math.isclose(left, right, rel_tol=0, abs_tol=0.05 if area else 1e-9)


def identity_comparison(gis, title, parcel_gis, parcel_titles):
    """Return explicit evidence for a very conservative same-building decision."""
    comparisons = {}
    for label, gis_field, ledger_field in (
        ("building_area_m2", "A12", "BDAR"), ("gross_floor_area_m2", "A14", "GFA"),
        ("above_ground_floors", "A26", "GRND_NOFL"),
    ):
        result = numeric_pair(gis.get(gis_field), title.get(ledger_field), area="area" in label)
        if result is not None:
            comparisons[label] = {"gis": gis.get(gis_field), "ledger": title.get(ledger_field), "matches": result}
    left_date, right_date = date_key(gis.get("A13")), date_key(title.get("USE_APRV_DAY"))
    if left_date and right_date:
        comparisons["approval_date"] = {"gis": left_date, "ledger": right_date, "matches": left_date == right_date}
    named = []
    for gis_field, ledger_field, label in (("A24", "BLDG_NM", "building_name"), ("A25", "DNG_NM", "dong_name")):
        value = text(gis.get(gis_field))
        if value and value == text(title.get(ledger_field)):
            named.append({"field": label, "value": value,
                          "unique_in_full_gis_parcel": sum(text(row.get(gis_field)) == value for row in parcel_gis) == 1,
                          "unique_in_title_parcel": sum(text(row.get(ledger_field)) == value for row in parcel_titles) == 1})
    unique_name = any(item["unique_in_full_gis_parcel"] and item["unique_in_title_parcel"] for item in named)
    one_to_one = len(parcel_gis) == len(parcel_titles) == 1
    matched = {key for key, value in comparisons.items() if value["matches"]}
    conflicts = [key for key, value in comparisons.items() if not value["matches"]]
    # At least one positive area and a separate positive-floor or valid date
    # agreement; mere parcel equality or a generic/shared name cannot suffice.
    corroborated = bool(matched & {"building_area_m2", "gross_floor_area_m2"}) and bool(
        matched & {"above_ground_floors", "approval_date"})
    accepted = (one_to_one or unique_name) and corroborated and not conflicts
    return {
        "accepted": accepted, "rule": ("full_parcel_one_gis_one_title_with_corroboration" if one_to_one else
                                            "unique_exact_name_or_dong_with_corroboration" if unique_name else None),
        "full_gis_parcel_rows": len(parcel_gis), "title_parcel_rows": len(parcel_titles),
        "exact_name_matches": named, "attribute_comparisons": comparisons,
        "conflicting_attributes": conflicts, "corroboration_sufficient": corroborated,
        "a19_to_hub_pk_mapping_used": False,
    }


def load_title_file(path, expected_dong, acquisition):
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload.get("Data"), list):
        raise ValueError(f"Invalid HUB export Data: {path.name}")
    description = payload.get("Description", {})
    for field, label in FIELDS.items():
        if description.get(field) != label:
            raise ValueError(f"HUB field definition differs: {path.name} {field}")
    rows = payload["Data"]
    if not (acquisition.get("file") == path.name
            and acquisition.get("sha256") == sha256(path)
            and acquisition.get("source_url") == SOURCE_URL
            and acquisition.get("source_month") == "2026-08"
            and acquisition.get("sigungu_cd", "") + acquisition.get("bjdong_cd", "") == expected_dong
            and acquisition.get("full_export_count_verified") is True
            and acquisition.get("ui_search_total") == len(rows)
            and acquisition.get("export_row_count") == len(rows)):
        raise ValueError(f"HUB full-export acquisition evidence is inconsistent: {path.name}")
    seen = set()
    for row in rows:
        if text(row.get("SGG_CD")) + text(row.get("STDG_CD")) != expected_dong:
            raise ValueError(f"Unexpected legal neighborhood in {path.name}")
        key = text(row.get("BDRG_SN"))
        if not key or key in seen:
            raise ValueError(f"Missing/duplicate title key in {path.name}")
        seen.add(key)
        if text(row.get("LDGR_KIND_CD")) not in {"2", "3"}:
            raise ValueError(f"HUB export includes a non-title record in {path.name}")
    metadata = {
        "file": path.name, "sha256": sha256(path), "row_count": len(rows),
        "source_url": SOURCE_URL, "source_name": "건축HUB 건축물대장 표제부 공개조회 JSON 내보내기",
        "snapshot_month": "2026-08", "legal_dong_code": expected_dong,
        "source_file_modified_at_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "verified_height_field": {"name": "HG", "description": description["HG"]},
        "verified_identity_field": {"name": "BDRG_SN", "description": description["BDRG_SN"]},
        "full_export_evidence": acquisition,
    }
    return rows, metadata


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def reconcile_region(key, config, *, root=ROOT):
    root = Path(root)
    directory = root / config["data_dir"]
    buildings = gpd.read_file(directory / "buildings.gpkg")
    context = json.loads((directory / "ledger_identity_context.json").read_text(encoding="utf-8"))
    source_provenance = json.loads((directory / "buildings.provenance.json").read_text(encoding="utf-8"))
    if context["original_file_sha256"] != source_provenance["original_file_sha256"]:
        raise ValueError("GIS parcel context belongs to a different source archive.")
    if sha256(directory / "buildings.gpkg") != source_provenance["sha256"]:
        raise ValueError("Building data differs from its acquisition record.")
    title_by_pnu = defaultdict(list)
    available_dongs, source_files, record_source = set(), [], {}
    acquisition_path = root / "data/raw/building_hub/acquisition.json"
    acquisitions = (json.loads(acquisition_path.read_text(encoding="utf-8"))["files"]
                    if acquisition_path.exists() else {})
    for dong in sorted(set(buildings.A3)):
        path = root / "data/raw/building_hub" / f"{dong[:5]}_{dong[5:]}_202608.json"
        if not path.exists() or path.name not in acquisitions:
            continue
        rows, metadata = load_title_file(path, dong, acquisitions[path.name])
        available_dongs.add(dong)
        source_files.append(metadata)
        for row in rows:
            pnu = title_pnu(row)
            if pnu:
                title_by_pnu[pnu].append(row)
                record_source[text(row["BDRG_SN"])] = metadata
    audits, selected_titles, matches, provisional = {}, {}, [], []
    for _, building in buildings.iterrows():
        if valid_height(building.A16) is not None:
            continue  # Source GIS heights retain priority, untouched.
        building_id, pnu = text(building.A1), text(building.A2)
        entry = {"status": "pending_lookup", "lookup_pnu": pnu,
                 "legal_dong_code": text(building.A3), "candidate_ids": [],
                 "checked_source": None, "identity_evidence": None}
        audits[building_id] = entry
        if text(building.A3) not in available_dongs:
            entry["reason"] = "The complete legal-neighborhood HUB export and its verified acquisition record are not available. No completed check is claimed."
            continue
        entry["checked_source"] = next(item for item in source_files if item["legal_dong_code"] == text(building.A3))
        candidates = title_by_pnu.get(pnu, [])
        entry["candidate_ids"] = [text(row["BDRG_SN"]) for row in candidates]
        gis_parcel = context["records_by_pnu"].get(pnu, [])
        raw = [row for row in gis_parcel if text(row.get("A1")) == building_id]
        if len(raw) != 1 or context["parcel_building_counts"].get(pnu) != len(gis_parcel):
            raise ValueError("Full-source GIS parcel identity context is inconsistent.")
        if not candidates:
            entry.update(status="no_title_found", reason="No title in the full exported legal-neighborhood dataset has this exact PNU.")
            continue
        evidence = []
        accepted = []
        for candidate in candidates:
            proof = identity_comparison(raw[0], candidate, gis_parcel, candidates)
            candidate_id = text(candidate["BDRG_SN"])
            # Reverse uniqueness covers known GIS rows and rows outside the AOI.
            owners = [text(row["A1"]) for row in gis_parcel
                      if identity_comparison(row, candidate, gis_parcel, candidates)["accepted"]]
            proof["ledger_id"] = candidate_id
            proof["corroborated_gis_owners"] = owners
            proof["height_m"] = candidate.get("HG")
            if proof["accepted"] and owners == [building_id]:
                accepted.append((candidate, proof))
            evidence.append(proof)
        entry["candidate_evidence"] = evidence
        if len(accepted) != 1:
            ambiguous = len(candidates) > 1 or len(gis_parcel) > 1 or len(accepted) > 1
            entry.update(status="identity_ambiguous" if ambiguous else "identity_unconfirmed",
                         reason="No unique same-building identity satisfies the documented corroboration rule; parcel similarity alone is not used.")
            continue
        candidate, proof = accepted[0]
        proof.update(source_file=record_source[text(candidate["BDRG_SN"])]["file"],
                     source_sha256=record_source[text(candidate["BDRG_SN"])]["sha256"],
                     source_url=SOURCE_URL, exact_pnu=pnu, gis_building_id=building_id)
        entry["identity_evidence"] = proof
        if valid_height(candidate.get("HG")) is None:
            entry.update(status="title_height_missing", matched_ledger_id=text(candidate["BDRG_SN"]),
                         reason="Identity is corroborated, but the matched title does not provide a positive finite HG height.")
            continue
        provisional.append((building_id, candidate, proof))
    # A final global guard prevents accidental reuse of one title across GIS IDs.
    usages = Counter(text(candidate["BDRG_SN"]) for _, candidate, _ in provisional)
    for building_id, candidate, proof in provisional:
        ledger_id = text(candidate["BDRG_SN"])
        if usages[ledger_id] != 1:
            audits[building_id].update(status="identity_ambiguous", reason="A title was selected by more than one GIS building.")
            continue
        height = valid_height(candidate["HG"])
        audits[building_id].update(status="verified_height", matched_ledger_id=ledger_id, height_m=height,
                                  reason="Same-building identity confirmed by exact parcel, full-parcel uniqueness and corroborating official attributes.")
        selected_titles[ledger_id] = {"namespace": NAMESPACE, "ledger_id": ledger_id,
                                      "record_type": "title", "height_m": height}
        matches.append({"building_id": building_id, "namespace": NAMESPACE, "ledger_id": ledger_id,
                        "verified": "true", "identity_method": "manual_building_confirmation",
                        "identity_evidence": json.dumps(proof, ensure_ascii=False, separators=(",", ":"), allow_nan=False)})
    counts = dict(Counter(row["status"] for row in audits.values()))
    audit = {
        "region_key": key, "center_lonlat": config["center_lonlat"], "size_m": 400,
        "building_source_sha256": source_provenance["sha256"],
        "full_gis_parcel_context_sha256": sha256(directory / "ledger_identity_context.json"),
        "audited_at_utc": datetime.now(timezone.utc).isoformat(), "source_files": source_files,
        "gis_building_count": len(buildings), "gis_missing_height_count": len(audits),
        "lookup_complete": counts.get("pending_lookup", 0) == 0,
        "counts": counts, "buildings": audits,
        "matching_policy": {
            "id": "exact_parcel_corroborated_identity_v1",
            "rules": ["Exact PNU, then either full-source parcel one-GIS/one-title or a nonempty exact name/dong unique in both full parcel datasets.",
                      "At least one positive building/gross-floor area within 0.05 square metres and an equal positive floor count or equal valid approval date.",
                      "No conflicting available area/floor/date attributes. One unique title candidate and reverse unique GIS owner, including outside-AOI records.",
                      "No A19 conversion, prefix guessing, address similarity, floor-height estimation or GIS height replacement."],
            "known_gis_height_priority": True,
        },
    }
    write_csv(directory / "ledger.csv", ["namespace", "ledger_id", "record_type", "height_m"], list(selected_titles.values()))
    write_csv(directory / "verified_matches.csv", ["building_id", "namespace", "ledger_id", "verified", "identity_method", "identity_evidence"], matches)
    audit["ledger_sha256"] = sha256(directory / "ledger.csv")
    audit["verified_matches_sha256"] = sha256(directory / "verified_matches.csv")
    (directory / "ledger_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regions", nargs="+", help="Region keys; default all five")
    args = parser.parse_args(argv)
    config = json.loads((ROOT / "config/regions.five.json").read_text(encoding="utf-8"))
    for key in args.regions or config["regions"]:
        result = reconcile_region(key, config["regions"][key])
        print(json.dumps({"region": key, "lookup_complete": result["lookup_complete"], "counts": result["counts"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
