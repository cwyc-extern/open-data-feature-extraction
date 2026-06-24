#!/usr/bin/env python3
"""
Download OpenStreetMap fibre optic backbone / communications cable routes.

Tag guidance (verified against OSM wiki and live data, June 2026):
  - communication=line  — de facto standard for communications cables (~15k ways
    globally). Recommended primary tag; combine with telecom:medium=fibre for
    terrestrial fibre backbone routes.
  - telecom=line        — secondary / stub tag (~3.7k ways). Less common; included
    by default via --tags both for completeness.

Wiki: https://wiki.openstreetmap.org/wiki/Tag:communication=line
      https://wiki.openstreetmap.org/wiki/Tag:telecom=line

Data source: Overpass API (ODbL — https://www.openstreetmap.org/copyright).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import LineString
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "telecommunications"

DEFAULT_COUNTRY = "TN"
DEFAULT_FORMAT = "gpkg"
DEFAULT_TAGS = "both"

OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)

BYTES_PER_WAY_ESTIMATE = 2_500
GEOJSON_OVERHEAD_FACTOR = 1.05
GPKG_COMPRESSION_FACTOR = 0.65

COUNTRY_NAMES = {
    "TN": "Tunisia",
    "DZ": "Algeria",
    "MA": "Morocco",
    "LY": "Libya",
    "EG": "Egypt",
    "VN": "Vietnam",
    "ID": "Indonesia",
    "IN": "India",
    "BD": "Bangladesh",
    "FR": "France",
    "DE": "Germany",
    "IT": "Italy",
    "ES": "Spain",
    "GB": "United Kingdom",
    "US": "United States",
}

TAG_MODES = {
    "communication": ('["communication"="line"]', "communication=line"),
    "telecom": ('["telecom"="line"]', "telecom=line"),
    "both": ("both", "communication=line + telecom=line (deduplicated)"),
}

ATTRIBUTE_COLUMNS = (
    "osm_id",
    "primary_tag",
    "communication",
    "telecom",
    "telecom_medium",
    "location",
    "name",
    "operator",
    "capacity",
    "cables",
    "ref",
    "submarine",
    "seamark_type",
    "seamark_category",
    "geometry",
)


@dataclass(frozen=True)
class DownloadEstimate:
    country_code: str
    country_name: str
    tag_filter: str
    way_count: int
    counts_by_mode: tuple[tuple[str, int], ...]
    estimated_osm_json_bytes: int
    estimated_output_bytes: int
    query_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate and download OSM fibre optic backbone / communications "
            "cable routes within a country."
        )
    )
    parser.add_argument(
        "--country",
        default=DEFAULT_COUNTRY,
        metavar="ISO2",
        help=f"ISO 3166-1 alpha-2 country code (default: {DEFAULT_COUNTRY})",
    )
    parser.add_argument(
        "--tags",
        choices=tuple(TAG_MODES),
        default=DEFAULT_TAGS,
        help=(
            "Which OSM line tags to query (default: both). "
            "communication=line is the wiki-recommended primary tag."
        ),
    )
    parser.add_argument(
        "--fibre-only",
        action="store_true",
        help=(
            "Keep only fibre routes (telecom:medium=fibre or submarine "
            "seamark:cable_submarine:category=fibre_optic)"
        ),
    )
    parser.add_argument(
        "--terrestrial-only",
        action="store_true",
        help="Exclude submarine / underwater cables",
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
            "(default: data/telecommunications/osm_fibre_<country>_<timestamp>.<format>)"
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and download immediately after estimate",
    )
    return parser.parse_args()


def country_display_name(code: str) -> str:
    normalized = code.strip().upper()
    return COUNTRY_NAMES.get(normalized, normalized)


def tag_modes_for(tag_mode: str) -> tuple[str, ...]:
    if tag_mode == "both":
        return ("communication", "telecom")
    return (tag_mode,)


def build_count_query(country_code: str, tag_mode: str) -> str:
    filter_expr, _ = TAG_MODES[tag_mode]
    return f"""
[out:json][timeout:120];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
way(area.country){filter_expr};
out count;
""".strip()


def build_download_query(country_code: str, tag_mode: str) -> str:
    filter_expr, _ = TAG_MODES[tag_mode]
    return f"""
[out:json][timeout:600];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
way(area.country){filter_expr};
out geom;
""".strip()


def post_overpass(query: str, timeout: int) -> requests.Response:
    headers = {"User-Agent": "osm-fibre-backbone-downloader/1.0 (educational GIS script)"}
    last_error: Exception | None = None

    for server in OVERPASS_SERVERS:
        for attempt in range(2):
            try:
                response = requests.post(
                    server,
                    data={"data": query},
                    headers=headers,
                    timeout=timeout,
                )
                response.raise_for_status()
                text = response.text.lstrip()
                if text.startswith("<"):
                    if "too busy" in text.lower() and attempt == 0:
                        time.sleep(8)
                        continue
                    raise RuntimeError("Overpass returned HTML instead of JSON.")
                return response
            except Exception as exc:  # noqa: BLE001 - retry next mirror
                last_error = exc
                if attempt == 0:
                    time.sleep(3)
                    continue
                break

    raise RuntimeError(f"All Overpass servers failed. Last error: {last_error}")


def parse_count_response(payload: dict) -> int:
    for element in payload.get("elements", []):
        if element.get("type") == "count":
            tags = element.get("tags", {})
            if "total" in tags:
                return int(tags["total"])
            return int(tags.get("ways", 0))
    raise RuntimeError("Overpass count response did not include way count.")


def estimate_download(country_code: str, tag_mode: str, output_format: str) -> DownloadEstimate:
    country_code = country_code.upper()
    _, tag_label = TAG_MODES[tag_mode]
    started = time.perf_counter()
    counts_by_mode: list[tuple[str, int]] = []
    way_count = 0
    for mode in tag_modes_for(tag_mode):
        response = post_overpass(build_count_query(country_code, mode), timeout=180)
        mode_count = parse_count_response(response.json())
        counts_by_mode.append((mode, mode_count))
        way_count += mode_count
    elapsed = time.perf_counter() - started

    osm_json_bytes = max(way_count, 1) * BYTES_PER_WAY_ESTIMATE
    output_bytes = int(
        osm_json_bytes
        * GEOJSON_OVERHEAD_FACTOR
        * (GPKG_COMPRESSION_FACTOR if output_format == "gpkg" else 1.0)
    )

    return DownloadEstimate(
        country_code=country_code,
        country_name=country_display_name(country_code),
        tag_filter=tag_label,
        way_count=way_count,
        counts_by_mode=tuple(counts_by_mode),
        estimated_osm_json_bytes=osm_json_bytes,
        estimated_output_bytes=output_bytes,
        query_seconds=elapsed,
    )


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
    output_path: Path,
    output_format: str,
    fibre_only: bool,
    terrestrial_only: bool,
) -> None:
    print()
    print("=" * 60)
    print("DOWNLOAD ESTIMATE (no data downloaded yet)")
    print("=" * 60)
    print("Source:             OpenStreetMap (Overpass API)")
    print("Feature:            Fibre optic / communications backbone routes")
    print(f"Country:            {estimate.country_name} ({estimate.country_code})")
    print(f"OSM tag filter:     {estimate.tag_filter}")
    print(f"Post-filter:        fibre_only={fibre_only}, terrestrial_only={terrestrial_only}")
    print(f"Matching ways:      {estimate.way_count:,}")
    print(f"Count query time:     {estimate.query_seconds:.1f} s")
    print(f"Output file:        {output_path}")
    print(f"Estimated output ({output_format.upper()}): ~{human_bytes(estimate.estimated_output_bytes)}")
    print()
    print("Tag notes:")
    print("  communication=line  — wiki-recommended primary tag (de facto)")
    print("  telecom=line        — secondary tag, less common")
    print("  telecom:medium=fibre — use with communication=line for terrestrial fibre")
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


def download_with_progress(query: str, estimated_bytes: int) -> bytes:
    headers = {"User-Agent": "osm-fibre-backbone-downloader/1.0 (educational GIS script)"}
    last_error: Exception | None = None

    for server in OVERPASS_SERVERS:
        try:
            with requests.post(
                server,
                data={"data": query},
                headers=headers,
                timeout=900,
                stream=True,
            ) as response:
                response.raise_for_status()
                total = int(response.headers.get("Content-Length", 0)) or estimated_bytes

                chunks: list[bytes] = []
                start = time.perf_counter()

                with tqdm(
                    total=total,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc="Downloading OSM data",
                    bar_format=(
                        "{l_bar}{bar}| {n_fmt}/{total_fmt} "
                        "[{elapsed}<{remaining}, {rate_fmt}]"
                    ),
                ) as progress:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        progress.update(len(chunk))

                        if total == estimated_bytes and progress.n > total:
                            total = progress.n
                            progress.total = total
                            progress.refresh()

                elapsed = max(time.perf_counter() - start, 1e-6)
                downloaded = sum(len(c) for c in chunks)
                print(
                    f"Download complete: {human_bytes(downloaded)} "
                    f"in {elapsed:.1f} s ({human_bytes(int(downloaded / elapsed))}/s)."
                )
                return b"".join(chunks)
        except Exception as exc:  # noqa: BLE001 - retry next mirror
            last_error = exc
            continue

    raise RuntimeError(f"Download failed on all Overpass servers. Last error: {last_error}")


def osm_way_to_linestring(way: dict) -> LineString | None:
    geometry = way.get("geometry") or []
    if len(geometry) < 2:
        return None
    coords = [(point["lon"], point["lat"]) for point in geometry]
    return LineString(coords)


def primary_tag_for(tags: dict) -> str:
    if tags.get("communication") == "line":
        return "communication=line"
    if tags.get("telecom") == "line":
        return "telecom=line"
    return "unknown"


def is_fibre_route(tags: dict) -> bool:
    medium = (tags.get("telecom:medium") or "").strip().lower()
    if medium == "fibre":
        return True
    seamark = (tags.get("seamark:cable_submarine:category") or "").strip().lower()
    return seamark == "fibre_optic"


def is_submarine_route(tags: dict) -> bool:
    location = (tags.get("location") or "").strip().lower()
    if location == "underwater":
        return True
    if (tags.get("submarine") or "").strip().lower() == "yes":
        return True
    if (tags.get("seamark:type") or "").strip().lower() == "cable_submarine":
        return True
    return False


def osm_to_geodataframe(
    payload: dict,
    *,
    fibre_only: bool,
    terrestrial_only: bool,
) -> gpd.GeoDataFrame:
    features: list[dict] = []
    seen_ids: set[int] = set()

    for element in payload.get("elements", []):
        if element.get("type") != "way":
            continue

        osm_id = element.get("id")
        if osm_id in seen_ids:
            continue
        seen_ids.add(osm_id)

        tags = element.get("tags", {})
        if fibre_only and not is_fibre_route(tags):
            continue
        if terrestrial_only and is_submarine_route(tags):
            continue

        line = osm_way_to_linestring(element)
        if line is None:
            continue

        features.append(
            {
                "osm_id": osm_id,
                "primary_tag": primary_tag_for(tags),
                "communication": tags.get("communication"),
                "telecom": tags.get("telecom"),
                "telecom_medium": tags.get("telecom:medium"),
                "location": tags.get("location"),
                "name": tags.get("name"),
                "operator": tags.get("operator"),
                "capacity": tags.get("capacity"),
                "cables": tags.get("cables"),
                "ref": tags.get("ref"),
                "submarine": tags.get("submarine"),
                "seamark_type": tags.get("seamark:type"),
                "seamark_category": tags.get("seamark:cable_submarine:category"),
                "geometry": line,
            }
        )

    if not features:
        return gpd.GeoDataFrame(columns=[*ATTRIBUTE_COLUMNS], geometry="geometry", crs="EPSG:4326")

    return gpd.GeoDataFrame(features, geometry="geometry", crs="EPSG:4326")


def download_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")


def default_output_path(country_code: str, fmt: str, timestamp: str) -> Path:
    return DATA_DIR / f"osm_fibre_{country_code.lower()}_{timestamp}.{fmt}"


def write_output(gdf: gpd.GeoDataFrame, output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "gpkg":
        gdf.to_file(output_path, driver="GPKG")
    else:
        gdf.to_file(output_path, driver="GeoJSON")


def main() -> int:
    args = parse_args()
    country_code = args.country.strip().upper()
    snapshot_timestamp = download_timestamp()
    output_path = args.output or default_output_path(
        country_code, args.format, snapshot_timestamp
    )

    try:
        estimate = estimate_download(country_code, args.tags, args.format)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Estimate failed: {exc}", file=sys.stderr)
        return 1

    print_estimate(
        estimate,
        output_path.resolve(),
        args.format,
        args.fibre_only,
        args.terrestrial_only,
    )

    if estimate.way_count == 0:
        print("No matching OSM communications routes found. Nothing to download.")
        return 0

    if not args.yes and not confirm_download():
        print("Download cancelled.")
        return 0

    try:
        payloads: list[dict] = []
        modes_to_fetch = [
            mode for mode, count in estimate.counts_by_mode if count > 0
        ] or list(tag_modes_for(args.tags))
        per_query_bytes = max(
            estimate.estimated_osm_json_bytes // max(len(modes_to_fetch), 1),
            1,
        )
        for mode in modes_to_fetch:
            query = build_download_query(country_code, mode)
            raw_bytes = download_with_progress(query, per_query_bytes)
            payloads.append(json.loads(raw_bytes))

        merged_elements: list[dict] = []
        for payload in payloads:
            merged_elements.extend(payload.get("elements", []))
        gdf = osm_to_geodataframe(
            {"elements": merged_elements},
            fibre_only=args.fibre_only,
            terrestrial_only=args.terrestrial_only,
        )
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1

    if gdf.empty:
        print(
            "Download finished, but no features remained after post-filters "
            f"(fibre_only={args.fibre_only}, terrestrial_only={args.terrestrial_only})."
        )
        return 1

    try:
        write_output(gdf, output_path, args.format)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Failed to write output: {exc}", file=sys.stderr)
        return 1

    file_size = output_path.stat().st_size
    tag_counts = gdf["primary_tag"].value_counts().to_dict()

    print()
    print(f"Saved {len(gdf):,} routes to {output_path.resolve()}")
    print(f"Output size: {human_bytes(file_size)}")
    print("By primary tag:")
    for tag, count in sorted(tag_counts.items()):
        print(f"  - {tag}: {count:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
