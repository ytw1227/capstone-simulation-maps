"""Acquire and verify real OSM backgrounds for the configured 400 m study areas.

Requests run sequentially, with a process-level wall-clock limit in addition to
OSMnx's per-request timeout. A verified saved bundle is reused by default. Use
--offline to require existing bundles; --rebuild replays the same cached query
when available. No building geometry, road width, or missing feature is invented.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import geopandas as gpd
from pyproj import CRS

from region_model.background import OSM_TAGS, fetch_background
from region_model.core import METRIC_CRS, region_geometry


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False), encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bundle_dir(region):
    directory = (ROOT / region["data_dir"]).resolve()
    if not directory.is_relative_to((ROOT / "data" / "regions_400").resolve()):
        raise ValueError("Background outputs must stay under data/regions_400.")
    return directory


def verify_bundle(region):
    directory = bundle_dir(region)
    path = directory / "background.gpkg"
    provenance = read_json(directory / "background.provenance.json")
    if provenance["center_lonlat"] != region["center_lonlat"] or provenance["size_m"] != 400:
        raise ValueError("Saved background center or size differs from configuration.")
    if provenance["sha256"] != sha256(path):
        raise ValueError("Saved background checksum does not match provenance.")
    if provenance["status"] != "ok" or provenance["synthetic"] or provenance["buildings_used"]:
        raise ValueError("Background must be successfully acquired non-synthetic OSM data.")
    origin, aoi = region_geometry(*region["center_lonlat"], 400)
    if provenance["aoi_bounds_metric"] != list(aoi.bounds):
        raise ValueError("Saved background bounds differ from the exact metric square.")
    frame = gpd.read_file(path, layer="background")
    if not CRS(frame.crs).equals(CRS(METRIC_CRS)):
        raise ValueError("Saved background uses an unexpected CRS.")
    if len(frame) != provenance["feature_count"] or frame.empty:
        raise ValueError("Saved background feature count differs from provenance.")
    if not frame.geometry.is_valid.all() or frame.geometry.is_empty.any():
        raise ValueError("Saved background contains invalid or empty geometry.")
    if not frame.geometry.map(aoi.buffer(1e-7).covers).all():
        raise ValueError("Saved background extends outside the 400 m square.")
    counts = {kind: int((frame["kind"] == kind).sum())
              for kind in ("road", "park", "landuse", "water")}
    if counts != provenance["counts_by_kind"]:
        raise ValueError("Saved background classification differs from provenance.")
    if frame["osm_id"].isna().any():
        raise ValueError("Saved background lost original OSM identifiers.")
    if not (directory / "LICENSE-OSM.md").is_file():
        raise ValueError("Saved background attribution notice is missing.")
    return {"region": region["name"], "feature_count": len(frame),
            "counts_by_kind": counts, "verified": True}


def acquire_region(key, region):
    import osmnx as ox

    directory = bundle_dir(region)
    directory.mkdir(parents=True, exist_ok=True)
    origin, aoi = region_geometry(*region["center_lonlat"], 400)
    signature = json.dumps({"center": region["center_lonlat"], "size": 400,
                            "crs": METRIC_CRS, "tags": OSM_TAGS,
                            "osmnx_version": ox.__version__,
                            "endpoint": ox.settings.overpass_url}, sort_keys=True)
    query_key = hashlib.sha256(signature.encode()).hexdigest()[:16]
    cache_dir = ROOT / ".cache" / "osmnx_regions_400" / f"{key}_{query_key}"
    frame, acquisition = fetch_background(aoi, METRIC_CRS, cache_dir)
    if acquisition["status"] != "ok" or frame.empty:
        raise RuntimeError(f"OSM acquisition failed for {key}: {acquisition}")
    completed = datetime.now(timezone.utc).isoformat()
    source_responses = []
    timestamps = set()
    for cached in sorted(cache_dir.glob("*.json")):
        payload = read_json(cached)
        if isinstance(payload, dict) and "elements" in payload:
            timestamp = payload.get("osm3s", {}).get("timestamp_osm_base")
            if timestamp:
                timestamps.add(timestamp)
            source_responses.append({"file": cached.name, "sha256": sha256(cached),
                                     "osm_database_timestamp_utc": timestamp,
                                     "element_count": len(payload["elements"])})
    if not source_responses or not timestamps:
        raise RuntimeError("Cannot establish source response checksum and OSM database date.")
    output = directory / "background.gpkg"
    temporary = directory / "background.pending.gpkg"
    if temporary.exists():
        temporary.unlink()
    frame.to_file(temporary, layer="background", driver="GPKG", index=False)
    temporary.replace(output)
    counts = {kind: int((frame["kind"] == kind).sum())
              for kind in ("road", "park", "landuse", "water")}
    metadata = {name: value for name, value in acquisition.items() if name != "cache_dir"}
    metadata.update({
        "schema_version": 1, "region_key": key,
        "source_endpoint": ox.settings.overpass_url,
        "completed_at_utc": completed,
        "osm_database_timestamps_utc": sorted(timestamps),
        "center_lonlat": region["center_lonlat"], "size_m": 400,
        "aoi_bounds_metric": list(aoi.bounds), "aoi_area_m2": aoi.area,
        "origin_metric": list(origin), "synthetic": False,
        "counts_by_kind": counts,
        "geometry_types": {str(kind): int(count) for kind, count in
                           frame.geometry.geom_type.value_counts().items()},
        "file": "background.gpkg", "layer": "background", "sha256": sha256(output),
        "license_uri": "https://opendatacommons.org/licenses/odbl/1-0/",
        "license_notice_file": "LICENSE-OSM.md",
        "source_responses": source_responses,
        "query_signature_sha256": hashlib.sha256(signature.encode()).hexdigest(),
        "preserved_osm_tags": [column for column in frame.columns
                               if column not in {"geometry", "kind", "osm_id"}],
        "processing": [
            "OSMnx features_from_polygon with the recorded union tags and geographic AOI.",
            "Excluded features tagged building or building:part; no OSM building footprint is used.",
            "Projected to EPSG:5179 and clipped to the exact 400 metre square.",
            "Preserved original OSM element type/id, available names, and road/water/tunnel/layer tags.",
            "Roads retain source line geometry; no road widths or elevations are inferred."
        ],
        "interpretation_notes": [
            "Feature count describes clipped geometry parts, not a count of separately named streets.",
            "Tunnel features remain in the source data and are hidden by the surface viewer.",
            "Zero retained park or water features is not an independently verified absence.",
            "Retrieval time may refer to a cache replay; database timestamps identify the OSM snapshot."
        ],
    })
    write_json(directory / "background.provenance.json", metadata)
    (directory / "LICENSE-OSM.md").write_text(
        "# OpenStreetMap background snapshot\n\n"
        "**© OpenStreetMap contributors**\n\n"
        "The database in `background.gpkg` is derived from OpenStreetMap and is made "
        "available under the [Open Data Commons Open Database License 1.0 (ODbL)]"
        "(https://opendatacommons.org/licenses/odbl/1-0/).\n\n"
        "Retain attribution and the license notice when sharing the data or maps. "
        "Modified versions of this database are subject to the ODbL share-alike terms. "
        "See the [OpenStreetMap copyright page](https://www.openstreetmap.org/copyright).\n\n"
        f"This snapshot covers the configured 400 m square for {region['name']}. "
        "Features were projected to EPSG:5179, clipped to the exact square, and classified "
        "as roads, parks, land use or water. Building features were excluded. "
        "Original OSM identifiers, available names, and relevant source tags are retained. "
        "Road surfaces, widths, elevations and missing features were not inferred.\n\n"
        "`background.provenance.json` records source endpoint, OSM database timestamps, "
        "retrieval time, exact bounds, counts, processing and checksums. This notice "
        "applies to the OSM database; the building data and software have separate notices.\n",
        encoding="utf-8")
    return verify_bundle(region)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "regions.five.json")
    parser.add_argument("--regions", nargs="+")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--wall-timeout", type=int, default=300)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    regions = read_json(args.config)["regions"]
    selected = args.regions or list(regions)
    if args.offline and args.rebuild:
        parser.error("--offline and --rebuild are mutually exclusive.")
    if any(key not in regions for key in selected):
        parser.error("Unknown region key.")
    if args.worker:
        if len(selected) != 1:
            parser.error("Worker requires one region.")
        result = acquire_region(selected[0], regions[selected[0]])
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return
    failed = []
    for key in selected:
        try:
            if not args.rebuild:
                try:
                    result = verify_bundle(regions[key])
                    result["reused"] = True
                    print(json.dumps(result, ensure_ascii=False), flush=True)
                    continue
                except (OSError, KeyError, ValueError):
                    if args.offline:
                        raise
            print(f"Acquiring OSM background: {key}", flush=True)
            subprocess.run([sys.executable, str(Path(__file__).resolve()),
                            "--config", str(args.config.resolve()),
                            "--regions", key, "--worker"], cwd=ROOT,
                           timeout=args.wall_timeout, check=True)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            failed.append(key)
            print(f"FAILED {key}: {type(error).__name__}: {error}", flush=True)
    if failed:
        raise SystemExit("Background acquisition/verification failed: " + ", ".join(failed))


if __name__ == "__main__":
    main()
