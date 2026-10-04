"""Optional OSM background layers; building geometry never comes from OSM.

OSMnx API reference: https://osmnx.readthedocs.io/en/stable/user-reference.html
The returned geometries stay in the projected CRS. Local coordinates are a
separate downstream transform, shared with the official building footprints.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import geopandas as gpd
import pandas as pd
from pyproj import CRS
from shapely import make_valid
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry


OSM_TAGS = {
    "highway": True,
    "leisure": ["park", "garden"],
    "landuse": True,
    "natural": ["water"],
    "waterway": True,
}
_KINDS = {"road", "park", "landuse", "water"}
_ALLOWED_GEOMETRIES = {
    "road": {"LineString"},
    "park": {"Polygon"},
    "landuse": {"Polygon"},
    "water": {"Polygon", "LineString"},
}
_PRESERVED_TAG_COLUMNS = (
    "name", "name_en", "name:en", "highway", "tunnel", "layer", "osm_layer",
    "bridge", "waterway", "natural", "covered", "leisure", "landuse",
)


def _empty(crs: str) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"kind": pd.Series(dtype="str"), "osm_id": pd.Series(dtype="str")},
        geometry=gpd.GeoSeries([], crs=crs),
        crs=crs,
    )


def _validate_aoi(aoi: Polygon, target_crs: str) -> None:
    crs = CRS.from_user_input(target_crs)
    if not crs.is_projected or any(
        axis.unit_conversion_factor != 1 for axis in crs.axis_info[:2]
    ):
        raise ValueError("Background target CRS must be projected with metre units.")
    if not isinstance(aoi, Polygon) or aoi.is_empty or not aoi.is_valid:
        raise ValueError("aoi_metric must be a valid, non-empty Polygon in target_crs.")


def _has_tag(value: Any) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(_has_tag(item) for item in value)
    return not pd.isna(value) and str(value).strip().lower() not in {"", "no", "false", "0"}


def _tag_matches(value: Any, accepted: set[str]) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(_tag_matches(item, accepted) for item in value)
    return not pd.isna(value) and str(value) in accepted


def _parts(geometry: BaseGeometry, allowed: set[str]) -> Iterator[BaseGeometry]:
    """Keep only dimensional parts appropriate to a layer, including collections."""
    if geometry.is_empty:
        return
    if geometry.geom_type in allowed:
        yield geometry
    elif geometry.geom_type.startswith("Multi") or geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from _parts(part, allowed)


def _osm_kind(row: pd.Series) -> str | None:
    geom = row.geometry
    if geom is None or geom.is_empty:
        return None
    if _has_tag(row.get("building")) or _has_tag(row.get("building:part")):
        return None
    if _has_tag(row.get("highway")) and geom.geom_type in {"LineString", "MultiLineString"}:
        return "road"
    if _tag_matches(row.get("leisure"), {"park", "garden"}):
        return "park"
    if _tag_matches(row.get("natural"), {"water"}) or _has_tag(row.get("waterway")):
        return "water"
    if _has_tag(row.get("landuse")):
        return "landuse"
    return None


def _prepare(
    frame: gpd.GeoDataFrame, aoi: Polygon, target_crs: str, *, osm: bool
) -> tuple[gpd.GeoDataFrame, dict[str, int]]:
    if frame.crs is None:
        raise ValueError("Background file has no CRS; supply a correctly georeferenced file.")
    projected = frame.to_crs(target_crs)
    tag_columns = [column for column in _PRESERVED_TAG_COLUMNS if column in projected.columns]
    counts = {"input_features": len(frame), "repaired_features": 0, "discarded_features": 0}
    records = []
    for index, row in projected.iterrows():
        kind = _osm_kind(row) if osm else row["kind"]
        geometry = row.geometry
        if kind not in _KINDS or geometry is None or geometry.is_empty:
            counts["discarded_features"] += 1
            continue
        if not geometry.is_valid:
            geometry = make_valid(geometry)
            counts["repaired_features"] += 1
        clipped = geometry.intersection(aoi)
        if not clipped.is_valid:
            clipped = make_valid(clipped)
        if osm:
            # OSM IDs are unique only within node/way/relation namespaces.
            osm_id = "/".join(map(str, index)) if isinstance(index, tuple) else str(index)
        else:
            osm_id = row.get("osm_id", None)
            osm_id = None if pd.isna(osm_id) else str(osm_id)
        kept = list(_parts(clipped, _ALLOWED_GEOMETRIES[kind]))
        if not kept:
            counts["discarded_features"] += 1
        # Preserve source semantics when clipping/splitting: a tunnel must not
        # become an apparent surface feature just because attributes were lost.
        tags = {column: row[column] for column in tag_columns}
        for original, portable in (("name:en", "name_en"), ("layer", "osm_layer")):
            if portable not in tags and original in tags:
                tags[portable] = tags[original]
        for part in kept:
            records.append({"geometry": part, "kind": kind, "osm_id": osm_id, **tags})
    if not records:
        return _empty(target_crs), counts
    return gpd.GeoDataFrame(records, geometry="geometry", crs=target_crs), counts


def _finish(frame: gpd.GeoDataFrame, metadata: dict) -> tuple[gpd.GeoDataFrame, dict]:
    metadata["status"] = "ok" if len(frame) else "no_results"
    metadata["feature_count"] = len(frame)
    metadata["counts_by_kind"] = {str(k): int(v) for k, v in frame["kind"].value_counts().items()}
    return frame, metadata


@contextmanager
def _osmnx_settings(ox: Any, cache_dir: Path) -> Iterator[None]:
    # Restore global OSMnx settings so importing this module does not change
    # callers' later network work. Call fetch_background sequentially.
    overrides = {
        "cache_folder": str(cache_dir),
        "use_cache": True,
        "cache_only_mode": False,
        "requests_timeout": 90,
        "log_file": False,
    }
    previous = {name: getattr(ox.settings, name) for name in overrides}
    try:
        for name, value in overrides.items():
            setattr(ox.settings, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(ox.settings, name, value)


def fetch_background(
    aoi_metric: Polygon,
    target_crs: str = "EPSG:5179",
    cache_dir: Path | None = None,
) -> tuple[gpd.GeoDataFrame, dict]:
    """Fetch roads, parks, land use and water and clip to the exact metric AOI.

    The metadata status distinguishes ``ok``, ``no_results`` and ``error``.
    Failed retrieval never silently masquerades as an empty successful map.
    OSMnx HTTP/Overpass requests use a 90-second timeout; server rate-limit
    waits and retries can make total elapsed time longer than 90 seconds.
    """
    _validate_aoi(aoi_metric, target_crs)
    cache_path = Path(cache_dir) if cache_dir is not None else Path.cwd() / ".cache" / "osmnx"
    metadata: dict[str, Any] = {
        "source": "OpenStreetMap via OSMnx",
        "source_url": "https://www.openstreetmap.org/",
        "attribution": "© OpenStreetMap contributors",
        "license": "ODbL-1.0",
        "license_url": "https://www.openstreetmap.org/copyright",
        "api": "osmnx.features_from_polygon",
        "tags": OSM_TAGS.copy(),
        "target_crs": str(target_crs),
        "cache_dir": str(cache_path.resolve()),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "request_timeout_seconds": 90,
        "timeout_scope": "per_request; server rate-limit waits/retries may take longer",
        "buildings_used": False,
    }
    try:
        import osmnx as ox

        if str(ox.__version__).split(".")[0] != "2":
            raise RuntimeError(f"OSMnx 2.x is required; found {ox.__version__}.")
        metadata["osmnx_version"] = ox.__version__
        cache_path.mkdir(parents=True, exist_ok=True)
        geographic_aoi = gpd.GeoSeries([aoi_metric], crs=target_crs).to_crs("EPSG:4326").iloc[0]
        with _osmnx_settings(ox, cache_path):
            features = ox.features_from_polygon(geographic_aoi, tags=OSM_TAGS)
        result, counts = _prepare(features, aoi_metric, target_crs, osm=True)
        metadata.update(counts)
        return _finish(result, metadata)
    except Exception as error:
        # OSMnx uses InsufficientResponseError for both no matching features
        # and malformed responses. Only its explicit no-match message means
        # a successful request without usable features.
        no_results = (
            type(error).__name__ == "InsufficientResponseError"
            and str(error).startswith("No matching features.")
        )
        metadata.update(
            status="no_results" if no_results else "error",
            error_type=type(error).__name__,
            error=str(error),
            feature_count=0,
            counts_by_kind={},
        )
        return _empty(target_crs), metadata


def load_background(
    path: str | Path, target_crs: str, aoi_metric: Polygon
) -> tuple[gpd.GeoDataFrame, dict]:
    """Load a previously classified GeoPackage/GeoJSON background.

    The file must declare its CRS and contain a ``kind`` column consisting
    of road/park/landuse/water. ``osm_id`` is optional and stays null if absent.
    Available source names and road/water/tunnel/layer tags survive clipping.
    No CRS, feature class, source attribution, or OSM identifier is guessed.
    """
    _validate_aoi(aoi_metric, target_crs)
    file_path = Path(path)
    metadata: dict[str, Any] = {
        "source": "local background file",
        "path": file_path.name,
        "target_crs": str(target_crs),
        "buildings_used": False,
    }
    try:
        if file_path.suffix.lower() not in {".gpkg", ".geojson", ".json"}:
            raise ValueError("Cached background must be a GeoPackage or GeoJSON file.")
        frame = gpd.read_file(file_path)
        if "kind" not in frame.columns:
            raise ValueError("Background requires a 'kind' column: road, park, landuse, or water.")
        if frame["kind"].isna().any() or not set(frame["kind"].unique()).issubset(_KINDS):
            raise ValueError("Every background 'kind' must be road, park, landuse, or water.")
        metadata["osm_ids_present"] = "osm_id" in frame.columns
        result, counts = _prepare(frame, aoi_metric, target_crs, osm=False)
        metadata.update(counts)
        return _finish(result, metadata)
    except Exception as error:
        metadata.update(
            status="error", error_type=type(error).__name__, error=str(error),
            feature_count=0, counts_by_kind={},
        )
        return _empty(target_crs), metadata
