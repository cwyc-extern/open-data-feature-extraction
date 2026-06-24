#!/usr/bin/env python3
"""
Download electricity transmission/distribution lines from the UN GeoPortal ArcGIS
Feature Service.

Default item: Tunisia Electricity Transmissions Lines (WorldBank) 2020
https://geoportal.un.org/arcgis/home/item.html?id=6c53909d572744b8912dde97664683ed
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "power-grid-infrastructure"

GEOPORTAL_PORTAL_URL = "https://geoportal.un.org/arcgis"
GEOPORTAL_SHARING_API = f"{GEOPORTAL_PORTAL_URL}/sharing/rest/content/items"
DEFAULT_ITEM_ID = "6c53909d572744b8912dde97664683ed"
DEFAULT_LAYER_ID = 0
DEFAULT_FORMAT = "gpkg"
DEFAULT_BATCH_SIZE = 2000

# Repeated dataset-level HTML fields; full text is recorded in README.txt.
REDUNDANT_ATTRIBUTE_COLUMNS = (
    "concepts",
    "sources",
    "methods",
    "limitations",
    "licenses",
)


@dataclass(frozen=True)
class GeoportalItem:
    item_id: str
    title: str
    name: str
    service_url: str
    layer_id: int
    layer_name: str
    country: str | None
    source_year: str | None
    description: str
    portal_url: str
    tags: tuple[str, ...]
    extent: tuple[tuple[float, float], tuple[float, float]] | None
    modified_ms: int | None


@dataclass(frozen=True)
class DownloadEstimate:
    item: GeoportalItem
    feature_count: int
    query_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download electricity line features from a UN GeoPortal ArcGIS item."
        )
    )
    parser.add_argument(
        "--item-id",
        default=DEFAULT_ITEM_ID,
        help=f"UN GeoPortal item ID (default: {DEFAULT_ITEM_ID})",
    )
    parser.add_argument(
        "--layer",
        type=int,
        default=DEFAULT_LAYER_ID,
        help=f"Feature layer index on the service (default: {DEFAULT_LAYER_ID})",
    )
    parser.add_argument(
        "--format",
        choices=("gpkg", "geojson"),
        default=DEFAULT_FORMAT,
        help="Output vector format (default: gpkg)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Output file path "
            "(default: data/power-grid-infrastructure/un_geoportal_<item-name>_<timestamp>.<format>)"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Features per ArcGIS query page (default: {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument(
        "--keep-metadata-columns",
        action="store_true",
        help="Keep repeated dataset-level HTML metadata columns on every feature",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and download immediately after estimate",
    )
    return parser.parse_args()


def portal_item_url(item_id: str) -> str:
    return f"{GEOPORTAL_PORTAL_URL}/home/item.html?id={item_id}"


def fetch_json(url: str, *, params: dict[str, Any] | None = None, timeout: int = 120) -> dict:
    headers = {"User-Agent": "un-geoportal-powerline-downloader/1.0 (educational GIS script)"}
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        message = payload["error"].get("message", "Unknown ArcGIS error")
        raise RuntimeError(message)
    return payload


def fetch_item_metadata(item_id: str, layer_id: int) -> GeoportalItem:
    item = fetch_json(f"{GEOPORTAL_SHARING_API}/{item_id}", params={"f": "json"})
    service_url = item.get("url")
    if not service_url:
        raise RuntimeError(f"Item {item_id} does not expose a Feature Service URL.")

    service = fetch_json(service_url, params={"f": "json"})
    layers = service.get("layers", [])
    if layer_id >= len(layers):
        raise RuntimeError(
            f"Layer {layer_id} not found on service (available: 0..{len(layers) - 1})."
        )

    layer_name = layers[layer_id]["name"]
    layer_meta = fetch_json(f"{service_url}/{layer_id}", params={"f": "json"})
    sample_attrs = (
        layer_meta.get("templates", [{}])[0]
        .get("prototype", {})
        .get("attributes", {})
    )

    extent = item.get("extent")
    parsed_extent = None
    if isinstance(extent, list) and len(extent) == 2:
        parsed_extent = ((extent[0][0], extent[0][1]), (extent[1][0], extent[1][1]))

    return GeoportalItem(
        item_id=item_id,
        title=item.get("title", item_id),
        name=item.get("name", item_id),
        service_url=service_url.rstrip("/"),
        layer_id=layer_id,
        layer_name=layer_name,
        country=sample_attrs.get("country"),
        source_year=sample_attrs.get("source_year"),
        description=item.get("description", ""),
        portal_url=portal_item_url(item_id),
        tags=tuple(item.get("tags", [])),
        extent=parsed_extent,
        modified_ms=item.get("modified"),
    )


def layer_query_url(item: GeoportalItem) -> str:
    return f"{item.service_url}/{item.layer_id}/query"


def count_features(item: GeoportalItem) -> int:
    payload = fetch_json(
        layer_query_url(item),
        params={"where": "1=1", "returnCountOnly": "true", "f": "json"},
        timeout=180,
    )
    return int(payload["count"])


def estimate_download(item: GeoportalItem) -> DownloadEstimate:
    started = time.perf_counter()
    feature_count = count_features(item)
    elapsed = time.perf_counter() - started
    return DownloadEstimate(item=item, feature_count=feature_count, query_seconds=elapsed)


def human_bytes(num_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def download_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")


def default_output_path(item: GeoportalItem, fmt: str, timestamp: str) -> Path:
    slug = item.name.lower().replace(" ", "_")
    return DATA_DIR / f"un_geoportal_{slug}_{timestamp}.{fmt}"


def print_estimate(estimate: DownloadEstimate, output_path: Path, batch_size: int) -> None:
    item = estimate.item
    pages = (estimate.feature_count + batch_size - 1) // batch_size if estimate.feature_count else 0

    print()
    print("=" * 60)
    print("DOWNLOAD ESTIMATE (no features downloaded yet)")
    print("=" * 60)
    print(f"Title:              {item.title}")
    print(f"Item ID:            {item.item_id}")
    print(f"Portal URL:         {item.portal_url}")
    print(f"Feature service:    {item.service_url}")
    print(f"Layer:              {item.layer_id} ({item.layer_name})")
    if item.country:
        print(f"Country attribute:  {item.country}")
    if item.source_year:
        print(f"Source year:        {item.source_year}")
    print(f"Matching features:  {estimate.feature_count:,}")
    print(f"Query pages:        {pages:,} x up to {batch_size:,}")
    print(f"Count query time:     {estimate.query_seconds:.1f} s")
    print(f"Output file:          {output_path}")
    print("=" * 60)


def confirm_download() -> bool:
    try:
        answer = input("Proceed with download? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def download_features(
    item: GeoportalItem,
    *,
    batch_size: int,
    keep_metadata_columns: bool,
) -> gpd.GeoDataFrame:
    if batch_size < 1:
        raise ValueError("batch-size must be at least 1.")

    total = count_features(item)
    if total == 0:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")

    pages = (total + batch_size - 1) // batch_size
    frames: list[gpd.GeoDataFrame] = []

    with tqdm(total=total, unit="feat", desc="Downloading features") as progress:
        for page in range(pages):
            offset = page * batch_size
            payload = fetch_json(
                layer_query_url(item),
                params={
                    "where": "1=1",
                    "outFields": "*",
                    "f": "geojson",
                    "resultOffset": offset,
                    "resultRecordCount": batch_size,
                },
                timeout=300,
            )
            features = payload.get("features", [])
            if not features:
                break

            frame = gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")
            frames.append(frame)
            progress.update(len(frame))

    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs="EPSG:4326")

    if not keep_metadata_columns:
        drop_cols = [col for col in REDUNDANT_ATTRIBUTE_COLUMNS if col in gdf.columns]
        if drop_cols:
            gdf = gdf.drop(columns=drop_cols)

    return gdf


def write_output(gdf: gpd.GeoDataFrame, output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "gpkg":
        gdf.to_file(output_path, driver="GPKG")
    else:
        gdf.to_file(output_path, driver="GeoJSON")


def main() -> int:
    args = parse_args()
    output_format = args.format

    try:
        item = fetch_item_metadata(args.item_id, args.layer)
        estimate = estimate_download(item)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Estimate failed: {exc}", file=sys.stderr)
        return 1

    snapshot_timestamp = download_timestamp()
    output_path = args.output or default_output_path(
        item, output_format, snapshot_timestamp
    )
    print_estimate(estimate, output_path.resolve(), args.batch_size)

    if estimate.feature_count == 0:
        print("No features found. Nothing to download.")
        return 0

    if not args.yes and not confirm_download():
        print("Download cancelled.")
        return 0

    try:
        gdf = download_features(
            item,
            batch_size=args.batch_size,
            keep_metadata_columns=args.keep_metadata_columns,
        )
        write_output(gdf, output_path, output_format)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    file_size = output_path.stat().st_size
    print()
    print(f"Saved {len(gdf):,} features to {output_path.resolve()}")
    print(f"Output size: {human_bytes(file_size)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
