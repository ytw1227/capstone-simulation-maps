"""Validate a user/browser-downloaded full legal-dong title export and retain it.

This reads downloaded files; it does not access browser state or authenticate.
Pass the total shown by the HUB search UI to detect truncated/page-only exports.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
SOURCE_URL = "https://www.hub.go.kr/portal/opn/tyb/idx-bdrg-ttlldr.do"


def register(district, dong, expected_count, downloads, source_month="2026-08"):
    target_dir = ROOT / "data/raw/building_hub"
    target_dir.mkdir(parents=True, exist_ok=True)
    for candidate in sorted(downloads.glob("03. *표제부*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            rows = data["Data"]
        except (OSError, UnicodeError, ValueError, KeyError):
            continue
        if not rows or {(str(r["SGG_CD"]), str(r["STDG_CD"])) for r in rows} != {(district, dong)}:
            continue
        if len(rows) != expected_count:
            raise ValueError(f"UI total {expected_count} != JSON rows {len(rows)}")
        if not {"HG", "BDRG_SN", "LDGR_KIND_CD", "PLOT_PSTN"}.issubset(data["Description"]):
            raise ValueError("Missing required title fields")
        identifiers = [str(row["BDRG_SN"]) for row in rows]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate ledger primary keys")
        filename = f"{district}_{dong}_{source_month.replace('-', '')}.json"
        target = target_dir / filename
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Refusing to replace a different source export: {target.name}")
        if not target.exists():
            shutil.copyfile(candidate, target)
        acquisition_file = target_dir / "acquisition.json"
        acquisition = json.loads(acquisition_file.read_text(encoding="utf-8")) if acquisition_file.exists() else {"files": {}}
        record = {
            "file": filename, "sha256": digest, "source_url": SOURCE_URL,
            "source_month": source_month, "sigungu_cd": district, "bjdong_cd": dong,
            "ui_search_total": expected_count, "export_row_count": len(rows),
            "full_export_count_verified": True, "download_original_filename": candidate.name,
            "registered_at_utc": datetime.now(timezone.utc).isoformat(),
            "method": "Official HUB public title search, legal-dong filter, JSON export, research purpose",
        }
        acquisition["files"][filename] = record
        acquisition_file.write_text(json.dumps(acquisition, ensure_ascii=False, indent=2), encoding="utf-8")
        return record
    raise FileNotFoundError("Matching completed HUB JSON download not found")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("district")
    parser.add_argument("dong")
    parser.add_argument("expected_count", type=int)
    parser.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    args = parser.parse_args()
    print(json.dumps(register(args.district, args.dong, args.expected_count, args.downloads), ensure_ascii=False, indent=2))
