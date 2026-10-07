"""Read all official GIS records on the five AOIs' parcels, including outside AOIs.

This is identity context for subsequent same-building title-register checks.
It does not infer any relationship between A19 and a register management key,
and it never assigns heights or validates a parcel-only building match.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd
import pandas as pd
import pyogrio

from scripts.import_official_gangnam import _sources, sha256


def main():
    config = json.loads((ROOT / "config/regions.five.json").read_text(encoding="utf-8"))
    archives = defaultdict(dict)
    for key, region in config["regions"].items():
        frame = gpd.read_file(ROOT / region["data_dir"] / "buildings.gpkg")
        archives[region["source_archive"]][key] = (region, frame)
    for relative_archive, regions in archives.items():
        pnus = sorted({value for _, frame in regions.values() for value in frame.A2.dropna().astype(str)})
        if not all(re.fullmatch(r"\d{19,}", value) for value in pnus):
            raise ValueError("Unexpected PNU syntax; cannot safely form attribute filter.")
        query = "A2 IN (" + ",".join("'" + value + "'" for value in pnus) + ")"
        archive = ROOT / relative_archive
        frames = []
        for source, _, _ in _sources(archive):
            # No bbox: include every geometry record sharing an AOI parcel.
            frame = pyogrio.read_dataframe(source, encoding="cp949", where=query, read_geometry=False)
            frames.append(frame)
        records = pd.concat(frames, ignore_index=True)
        for key, (region, aoi_frame) in regions.items():
            chosen = records.loc[records.A2.isin(aoi_frame.A2)]
            # DataFrame JSON converts nulls/dates without lossy ID conversion.
            rows = json.loads(chosen.to_json(orient="records", force_ascii=False, date_format="iso"))
            by_pnu = defaultdict(list)
            for row in rows:
                by_pnu[row["A2"]].append(row)
            counts = {pnu: len(values) for pnu, values in by_pnu.items()}
            inside_ids = set(aoi_frame.A1)
            payload = {
                "region_key": key, "source_file": archive.name,
                "original_file_sha256": sha256(archive),
                "scope": "All records in the complete source archive sharing any AOI-selected PNU; no spatial/bbox filter.",
                "identity_warning": "PNU identifies a parcel, not a building. A19 is named 건축물ID in the official column definitions; no mapping to mgmBldrgstPk has been verified. These records are context, not confirmed ledger matches.",
                "aoi_building_rows": len(aoi_frame), "full_source_parcel_rows": len(rows),
                "outside_aoi_building_rows": sum(row["A1"] not in inside_ids for row in rows),
                "parcel_building_counts": counts, "records_by_pnu": dict(by_pnu),
            }
            output = ROOT / region["data_dir"] / "ledger_identity_context.json"
            with output.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            print(json.dumps({"region": key, "aoi_rows": len(aoi_frame),
                              "full_parcel_rows": len(rows), "outside_aoi_rows": payload["outside_aoi_building_rows"],
                              "single_building_parcels": sum(count == 1 for count in counts.values()),
                              "multiple_building_parcels": sum(count > 1 for count in counts.values())}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
