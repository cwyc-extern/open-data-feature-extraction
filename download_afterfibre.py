#!/usr/bin/env python3
"""
Download AfTerFibre terrestrial fibre network data for Africa.

Data source: NSRC AfTerFibre map (https://afterfibre.nsrc.org/)
Vector tiles: https://d316kar6yg8hyq.cloudfront.net/africa-fiber/{z}/{x}/{y}.mvt

The legacy Carto SQL API (af_fibrephase) is no longer available; this script
reconstructs the dataset from the published MapLibre vector tiles.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import mapbox_vector_tile
import requests
from shapely.geometry import LineString, MultiLineString, shape
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "telecommunications"

TILEJSON_URL = "https://d316kar6yg8hyq.cloudfront.net/africa-fiber.json"
TILE_URL_TEMPLATE = "https://d316kar6yg8hyq.cloudfront.net/africa-fiber/{z}/{x}/{y}.mvt"
SOURCE_LAYER = "fiber"
DEFAULT_ZOOM = 10
DEFAULT_FORMAT = "gpkg"
DEFAULT_WORKERS = 24
EXTENT = 4096

ATTRIBUTE_COLUMNS = (
    "cartodb_id",
    "country",
    "iso2",
    "operator",
    "operator_name",
    "owner",
    "owner_name",
    "phase_name",
    "technology",
    "type",
    "live",
    "go_live",
    "fibre_cores",
    "source_url",
    "contributor",
    "contrib_email",
    "operator_web_url",
    "owner_web_url",
    "created_at",
    "updated_at",
)


@dataclass(frozen=True)
class TileJob:
    z: int
    x: int
    y: int


@dataclass(frozen=True)
class DownloadEstimate:
    zoom: int
    tile_count: int
    query_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download AfTerFibre terrestrial fibre networks for all covered "
            "African countries from NSRC vector tiles."
        )
    )
    parser.add_argument(
        "--zoom",
        type=int,
        default=DEFAULT_ZOOM,
        help=f"Vector tile zoom level (default: {DEFAULT_ZOOM})",
    )
    parser.add_argument(
        "--format",
        choices=("gpkg", "geojson"),
        default=DEFAULT_FORMAT,
        help="Per-country output format (default: gpkg)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Directory for country files "
            "(default: data/telecommunications/afterfibre_<timestamp>/)"
        ),
    )
    parser.add_argument(
        "--combined-output",
        type=Path,
        help="Optional combined Africa output file path",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel tile download workers (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and download immediately after estimate",
    )
    return parser.parse_args()


def download_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")


def fetch_tilejson() -> dict[str, Any]:
    headers = {"User-Agent": "afterfibre-downloader/1.0 (educational GIS script)"}
    response = requests.get(TILEJSON_URL, headers=headers, timeout=120)
    response.raise_for_status()
    return response.json()


def tile_range_for_bounds(bounds: tuple[float, float, float, float], zoom: int) -> tuple[int, int, int, int]:
    west, south, east, north = bounds
    n = 2**zoom
    x_min = int((west + 180.0) / 360.0 * n)
    x_max = int((east + 180.0) / 360.0 * n)
    y_min = int((1.0 - math.asinh(math.tan(math.radians(north))) / math.pi) / 2.0 * n)
    y_max = int((1.0 - math.asinh(math.tan(math.radians(south))) / math.pi) / 2.0 * n)
    return x_min, x_max, y_min, y_max


def build_tile_jobs(bounds: tuple[float, float, float, float], zoom: int) -> list[TileJob]:
    x_min, x_max, y_min, y_max = tile_range_for_bounds(bounds, zoom)
    return [
        TileJob(zoom, x, y)
        for x in range(x_min, x_max + 1)
        for y in range(y_min, y_max + 1)
    ]


def make_tile_transformer(x_tile: int, y_tile: int, zoom: int, extent: int = EXTENT):
    def transformer(px: float, py: float) -> tuple[float, float]:
        n = 2**zoom
        lon = (px / extent + x_tile) / n * 360.0 - 180.0
        lat_rad = math.atan(
            math.sinh(math.pi * (1.0 - 2.0 * (py / extent + y_tile) / n))
        )
        lat = math.degrees(lat_rad)
        return lon, lat

    return transformer


def geometry_from_mvt(geometry: dict) -> LineString | MultiLineString | None:
    if not geometry:
        return None
    try:
        geom = shape(geometry)
    except Exception:  # noqa: BLE001 - skip invalid geometries
        return None
    if isinstance(geom, (LineString, MultiLineString)):
        return geom
    return None


def feature_key(properties: dict[str, Any], geometry: dict) -> str:
    cartodb_id = properties.get("cartodb_id")
    if cartodb_id is not None:
        return f"id:{cartodb_id}"
    return "hash:" + json.dumps(
        {"properties": properties, "geometry": geometry},
        sort_keys=True,
        default=str,
    )


def fetch_tile_features(job: TileJob) -> list[dict[str, Any]]:
    url = TILE_URL_TEMPLATE.format(z=job.z, x=job.x, y=job.y)
    headers = {"User-Agent": "afterfibre-downloader/1.0 (educational GIS script)"}
    try:
        response = requests.get(url, headers=headers, timeout=60)
        response.raise_for_status()
    except Exception:  # noqa: BLE001 - skip failed tiles
        return []

    if not response.content:
        return []

    transformer = make_tile_transformer(job.x, job.y, job.z)
    try:
        layers = mapbox_vector_tile.decode(
            response.content,
            default_options={"transformer": transformer, "geojson": True},
        )
    except Exception:  # noqa: BLE001 - skip malformed tiles
        return []

    layer = layers.get(SOURCE_LAYER)
    if not layer:
        return []

    rows: list[dict[str, Any]] = []
    for feature in layer.get("features", []):
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry")
        line = geometry_from_mvt(geometry)
        if line is None:
            continue

        row = {column: properties.get(column) for column in ATTRIBUTE_COLUMNS}
        row["geometry"] = line
        row["_feature_key"] = feature_key(properties, geometry)
        rows.append(row)

    return rows


def estimate_download(bounds: tuple[float, float, float, float], zoom: int) -> DownloadEstimate:
    started = time.perf_counter()
    tile_count = len(build_tile_jobs(bounds, zoom))
    elapsed = time.perf_counter() - started
    return DownloadEstimate(zoom=zoom, tile_count=tile_count, query_seconds=elapsed)


def human_bytes(num_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def print_estimate(
    estimate: DownloadEstimate,
    bounds: tuple[float, float, float, float],
    output_dir: Path,
    combined_output: Path | None,
) -> None:
    print()
    print("=" * 60)
    print("DOWNLOAD ESTIMATE (no features downloaded yet)")
    print("=" * 60)
    print("Source:             AfTerFibre / NSRC vector tiles")
    print(f"TileJSON:           {TILEJSON_URL}")
    print(f"Zoom level:         {estimate.zoom}")
    print(f"Bounds (W,S,E,N):   {bounds}")
    print(f"Tiles to fetch:     {estimate.tile_count:,}")
    print(f"Estimate time:      {estimate.query_seconds:.1f} s")
    print(f"Output directory:   {output_dir}")
    if combined_output is not None:
        print(f"Combined output:    {combined_output}")
    print("=" * 60)
    print()


def confirm_download() -> bool:
    while True:
        answer = input("Proceed with download? [y/N]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Please answer 'y' or 'n'.")


def slugify_country(name: str, iso2: str | None = None) -> str:
    if iso2 and str(iso2).strip():
        return str(iso2).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return slug.strip("_") or "unknown"


def download_all_features(jobs: list[TileJob], workers: int) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=max(workers, 1)) as executor:
        futures = {executor.submit(fetch_tile_features, job): job for job in jobs}
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc="Downloading vector tiles",
            unit="tile",
        ):
            for row in future.result():
                merged[row["_feature_key"]] = row

    features = list(merged.values())
    for row in features:
        row.pop("_feature_key", None)
    return features


def features_to_geodataframe(features: list[dict[str, Any]]) -> gpd.GeoDataFrame:
    if not features:
        return gpd.GeoDataFrame(
            columns=[*ATTRIBUTE_COLUMNS, "geometry"],
            geometry="geometry",
            crs="EPSG:4326",
        )
    return gpd.GeoDataFrame(features, geometry="geometry", crs="EPSG:4326")


def write_output(gdf: gpd.GeoDataFrame, output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "gpkg":
        gdf.to_file(output_path, driver="GPKG")
    else:
        gdf.to_file(output_path, driver="GeoJSON")


def default_output_dir(timestamp: str) -> Path:
    return DATA_DIR / f"afterfibre_{timestamp}"


def main() -> int:
    args = parse_args()
    if args.zoom < 0 or args.zoom > 14:
        print("Zoom must be between 0 and 14.", file=sys.stderr)
        return 1

    snapshot_timestamp = download_timestamp()
    output_dir = args.output_dir or default_output_dir(snapshot_timestamp)
    combined_output = args.combined_output

    try:
        metadata = fetch_tilejson()
        bounds = tuple(metadata["bounds"])
        estimate = estimate_download(bounds, args.zoom)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Estimate failed: {exc}", file=sys.stderr)
        return 1

    if combined_output is None:
        combined_output = output_dir / f"afterfibre_africa_{snapshot_timestamp}.{args.format}"

    print_estimate(estimate, bounds, output_dir.resolve(), combined_output.resolve())

    if estimate.tile_count == 0:
        print("No tiles to download.")
        return 0

    if not args.yes and not confirm_download():
        print("Download cancelled.")
        return 0

    jobs = build_tile_jobs(bounds, args.zoom)
    try:
        features = download_all_features(jobs, args.workers)
        gdf = features_to_geodataframe(features)
        if gdf.empty:
            print("Download finished, but no AfTerFibre features were found.")
            return 1

        write_output(gdf, combined_output, args.format)

        country_groups: dict[str, gpd.GeoDataFrame] = {}
        for (country, iso2), group in gdf.groupby(["country", "iso2"], dropna=False):
            label = country if isinstance(country, str) and country.strip() else "Unknown"
            iso2_text = str(iso2).strip() if iso2 is not None else ""
            iso2_val = iso2_text if iso2_text and iso2_text.lower() != "nan" else None
            slug = slugify_country(label, iso2_val)
            country_groups[slug] = group.copy()

        output_dir.mkdir(parents=True, exist_ok=True)
        for slug, country_gdf in sorted(country_groups.items()):
            country_path = output_dir / f"afterfibre_{slug}_{snapshot_timestamp}.{args.format}"
            write_output(country_gdf, country_path, args.format)

    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    combined_size = combined_output.stat().st_size
    print()
    print(f"Saved {len(gdf):,} fibre segments (all countries) to {combined_output.resolve()}")
    print(f"Combined output size: {human_bytes(combined_size)}")
    print(f"Saved {len(country_groups):,} country files under {output_dir.resolve()}")
    for slug, country_gdf in sorted(country_groups.items()):
        country_name = country_gdf["country"].dropna().iloc[0] if not country_gdf.empty else slug
        print(f"  - {country_name}: {len(country_gdf):,} segments ({slug})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
