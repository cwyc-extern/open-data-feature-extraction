#!/usr/bin/env python3
"""
Download ITU Interactive Transmission Maps (BBmaps) global transmission data.

Data source: ITU BDT GeoCatalogue / GeoServer WFS
https://bbmaps.itu.int/
Layer: itu-geocatalogue:trx_geocatalogue (terrestrial fibre and microwave links)
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import pandas as pd
import requests
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "telecommunications"

WFS_BASE_URL = "https://bbmaps.itu.int/geoserver/itu-geocatalogue/wfs"
FEATURE_TYPE = "itu-geocatalogue:trx_geocatalogue"
GEONETWORK_RECORD_ID = "f9af598b-da16-4a7a-a757-6cffc02e9565"
GEONETWORK_RECORD_URL = (
    f"https://bbmaps.itu.int/geonetwork/srv/api/records/{GEONETWORK_RECORD_ID}"
)
PORTAL_URL = "https://www.itu.int/en/ITU-D/Technology/Pages/InteractiveTransmissionMaps.aspx"
NATURAL_EARTH_COUNTRIES_URL = (
    "https://naciscdn.org/naturalearth/110m/cultural/ne_110m_admin_0_countries.zip"
)

DEFAULT_FORMAT = "gpkg"
DEFAULT_PAGE_SIZE = 10_000
MAX_SINGLE_REQUEST = 100_000
WFS_SORT_BY = "uid"

ATTRIBUTE_COLUMNS = ("uid", "id", "type_inf", "status", "type_", "country_name", "iso2")


@dataclass(frozen=True)
class DownloadEstimate:
    feature_count: int
    query_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download ITU BBmaps global transmission links and split by country."
        )
    )
    parser.add_argument(
        "--format",
        choices=("gpkg", "geojson"),
        default=DEFAULT_FORMAT,
        help="Output vector format (default: gpkg)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Directory for country files "
            "(default: data/telecommunications/itu_bbmaps_<timestamp>/)"
        ),
    )
    parser.add_argument(
        "--combined-output",
        type=Path,
        help="Optional combined global output file path",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"WFS page size when paginating (default: {DEFAULT_PAGE_SIZE})",
    )
    parser.add_argument(
        "--technology",
        choices=("all", "fibre", "microwave"),
        default="all",
        help="Filter by transmission technology (default: all)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and download immediately after estimate",
    )
    return parser.parse_args()


def download_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")


def wfs_getfeature_url(
    *,
    start_index: int | None = None,
    count: int | None = None,
    sort_by: str | None = None,
) -> str:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": FEATURE_TYPE,
        "outputFormat": "application/json",
    }
    if count is not None:
        params["count"] = str(count)
    if start_index is not None:
        params["startIndex"] = str(start_index)
        params["sortBy"] = sort_by or WFS_SORT_BY

    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"{WFS_BASE_URL}?{query}"


def fetch_feature_count() -> int:
    headers = {"User-Agent": "itu-bbmaps-downloader/1.0 (educational GIS script)"}
    response = requests.get(
        wfs_getfeature_url(count=1),
        headers=headers,
        timeout=180,
    )
    response.raise_for_status()
    payload = response.json()
    total = payload.get("totalFeatures")
    if total is None:
        raise RuntimeError("WFS response did not include totalFeatures.")
    return int(total)


def estimate_download() -> DownloadEstimate:
    started = time.perf_counter()
    feature_count = fetch_feature_count()
    elapsed = time.perf_counter() - started
    return DownloadEstimate(feature_count=feature_count, query_seconds=elapsed)


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
    output_dir: Path,
    combined_output: Path,
    technology: str,
) -> None:
    print()
    print("=" * 60)
    print("DOWNLOAD ESTIMATE (no features downloaded yet)")
    print("=" * 60)
    print("Source:             ITU Interactive Transmission Maps (BBmaps)")
    print(f"Portal:             {PORTAL_URL}")
    print(f"GeoCatalogue:       {GEONETWORK_RECORD_URL}")
    print(f"WFS layer:          {FEATURE_TYPE}")
    print(f"Technology filter:  {technology}")
    print(f"Matching segments:  {estimate.feature_count:,}")
    print(f"Count query time:     {estimate.query_seconds:.1f} s")
    print(f"Output directory:   {output_dir}")
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


def download_features(page_size: int) -> gpd.GeoDataFrame:
    total = fetch_feature_count()
    if total == 0:
        return gpd.GeoDataFrame(columns=[*ATTRIBUTE_COLUMNS, "geometry"], crs="EPSG:4326")

    headers = {"User-Agent": "itu-bbmaps-downloader/1.0 (educational GIS script)"}
    frames: list[gpd.GeoDataFrame] = []

    with tqdm(total=total, unit="feat", desc="Downloading WFS features") as progress:
        if total <= MAX_SINGLE_REQUEST:
            response = requests.get(
                wfs_getfeature_url(count=total),
                headers=headers,
                timeout=300,
            )
            response.raise_for_status()
            frame = gpd.read_file(response.text)
            if not frame.empty:
                frames.append(frame)
                progress.update(len(frame))
        else:
            start_index = 0
            while start_index < total:
                response = requests.get(
                    wfs_getfeature_url(
                        start_index=start_index,
                        count=page_size,
                        sort_by=WFS_SORT_BY,
                    ),
                    headers=headers,
                    timeout=300,
                )
                response.raise_for_status()
                frame = gpd.read_file(response.text)
                if frame.empty:
                    break
                frames.append(frame)
                progress.update(len(frame))
                start_index += len(frame)
                if len(frame) < page_size:
                    break

    if not frames:
        return gpd.GeoDataFrame(columns=[*ATTRIBUTE_COLUMNS, "geometry"], crs="EPSG:4326")

    gdf = pd.concat(frames, ignore_index=True)
    return gpd.GeoDataFrame(gdf, geometry="geometry", crs="EPSG:4326")


def apply_technology_filter(gdf: gpd.GeoDataFrame, technology: str) -> gpd.GeoDataFrame:
    if technology == "all" or gdf.empty:
        return gdf

    needle = "fibre" if technology == "fibre" else "microwave"
    mask = gdf["type_inf"].fillna("").str.contains(needle, case=False, regex=False)
    return gdf.loc[mask].copy()


def load_country_boundaries() -> gpd.GeoDataFrame:
    world = gpd.read_file(NATURAL_EARTH_COUNTRIES_URL)
    world = world[["ADMIN", "ISO_A2", "geometry"]].rename(
        columns={"ADMIN": "country_name", "ISO_A2": "iso2"}
    )
    world["iso2"] = world["iso2"].replace({"-99": None})
    return world


def assign_countries(gdf: gpd.GeoDataFrame, world: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.empty:
        return gdf

    gdf = gdf.copy()
    gdf["_row_id"] = range(len(gdf))

    joined = gpd.sjoin(
        gdf,
        world,
        how="left",
        predicate="intersects",
    )
    joined = joined.reset_index(drop=True)

    # Keep the country with the longest shared boundary when a segment crosses borders.
    joined["_overlap"] = joined.apply(
        lambda row: (
            row.geometry.intersection(world.loc[row["index_right"], "geometry"]).length
            if pd.notna(row.get("index_right"))
            else 0.0
        ),
        axis=1,
    )

    best_rows = joined.groupby("_row_id")["_overlap"].idxmax()
    assigned = joined.loc[best_rows].copy()
    assigned = assigned.drop(
        columns=[col for col in assigned.columns if col.startswith("index")],
        errors="ignore",
    )
    assigned = assigned.drop(columns=["_overlap", "_row_id"], errors="ignore")

    columns = [*ATTRIBUTE_COLUMNS, "geometry"]
    for column in columns:
        if column not in assigned.columns:
            assigned[column] = None

    return gpd.GeoDataFrame(assigned[columns], geometry="geometry", crs="EPSG:4326")


def slugify_iso2(iso2: object, country_name: object) -> str:
    if isinstance(iso2, str) and iso2.strip() and iso2.strip() != "-99":
        return iso2.strip().lower()
    if isinstance(country_name, str) and country_name.strip():
        return re.sub(r"[^a-z0-9]+", "_", country_name.strip().lower()).strip("_")
    return "unknown"


def write_output(gdf: gpd.GeoDataFrame, output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "gpkg":
        gdf.to_file(output_path, driver="GPKG")
    else:
        gdf.to_file(output_path, driver="GeoJSON")


def default_output_dir(timestamp: str) -> Path:
    return DATA_DIR / f"itu_bbmaps_{timestamp}"


def summarize_countries(gdf: gpd.GeoDataFrame) -> list[tuple[str, str, int]]:
    if gdf.empty:
        return []

    rows: list[tuple[str, str, int]] = []
    for (iso2, country_name), group in gdf.groupby(["iso2", "country_name"], dropna=False):
        label = country_name if isinstance(country_name, str) and country_name else "Unknown"
        code = iso2 if isinstance(iso2, str) and iso2 and iso2 != "-99" else ""
        rows.append((label, code, len(group)))
    rows.sort(key=lambda item: (-item[2], item[0]))
    return rows


def main() -> int:
    args = parse_args()
    snapshot_timestamp = download_timestamp()
    output_dir = args.output_dir or default_output_dir(snapshot_timestamp)
    combined_output = args.combined_output or (
        output_dir / f"itu_bbmaps_global_{snapshot_timestamp}.{args.format}"
    )

    try:
        estimate = estimate_download()
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Estimate failed: {exc}", file=sys.stderr)
        return 1

    print_estimate(
        estimate,
        output_dir.resolve(),
        combined_output.resolve(),
        args.technology,
    )

    if estimate.feature_count == 0:
        print("No transmission segments found. Nothing to download.")
        return 0

    if not args.yes and not confirm_download():
        print("Download cancelled.")
        return 0

    try:
        gdf = download_features(args.page_size)
        gdf = apply_technology_filter(gdf, args.technology)
        if gdf.empty:
            print("Download finished, but no features matched the selected filter.")
            return 1

        world = load_country_boundaries()
        gdf = assign_countries(gdf, world)

        write_output(gdf, combined_output, args.format)

        output_dir.mkdir(parents=True, exist_ok=True)
        country_groups: dict[str, gpd.GeoDataFrame] = {}
        for (iso2, country_name), group in gdf.groupby(["iso2", "country_name"], dropna=False):
            slug = slugify_iso2(iso2, country_name)
            country_groups[slug] = group.copy()

        for slug, country_gdf in sorted(country_groups.items()):
            country_path = output_dir / f"itu_bbmaps_{slug}_{snapshot_timestamp}.{args.format}"
            write_output(country_gdf, country_path, args.format)

    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    combined_size = combined_output.stat().st_size
    country_rows = summarize_countries(gdf)

    print()
    print(f"Saved {len(gdf):,} transmission segments to {combined_output.resolve()}")
    print(f"Combined output size: {human_bytes(combined_size)}")
    print(f"Saved {len(country_groups):,} country files under {output_dir.resolve()}")
    for country_name, iso2, count in country_rows[:20]:
        code = iso2 or "—"
        print(f"  - {country_name} ({code}): {count:,} segments")
    if len(country_rows) > 20:
        print(f"  ... and {len(country_rows) - 20} more countries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
