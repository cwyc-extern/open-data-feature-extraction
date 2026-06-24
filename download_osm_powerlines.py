#!/usr/bin/env python3
"""
Download OpenStreetMap power infrastructure for a given country.

Downloads:
  - Transmission lines (power=line) filtered by voltage
  - Electricity transformers (power=transformer nodes), comparable to the UN
    GeoPortal OSM transformer layers, e.g. Tunisia item f5069ff368e34aed988a03c5f0d8effd
  - Electrical substations (power=substation) filtered by operational voltage

Default trial country: Tunisia (ISO 3166-1 alpha-2 code TN).
Data source: Overpass API (ODbL — https://www.openstreetmap.org/copyright).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import geopandas as gpd
import requests
from shapely.geometry import LineString, Point
from tqdm import tqdm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data" / "power-grid-infrastructure"

DEFAULT_COUNTRY = "TN"
DEFAULT_VOLTAGES_KV = (225, 150)
DEFAULT_FORMAT = "gpkg"

OVERPASS_SERVERS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)

# Calibrated from Tunisia trial download (308 ways -> ~920 KiB OSM JSON).
BYTES_PER_WAY_ESTIMATE = 3_000
GEOMETRY_POINTS_PER_WAY_ESTIMATE = 41
# Calibrated from Tunisia transformer download (245 nodes -> ~39 KiB OSM JSON).
BYTES_PER_NODE_ESTIMATE = 200
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


@dataclass(frozen=True)
class LineDownloadEstimate:
    country_code: str
    country_name: str
    voltages_v: tuple[int, ...]
    way_count: int
    estimated_osm_json_bytes: int
    estimated_geojson_bytes: int
    estimated_gpkg_bytes: int
    query_seconds: float

    @property
    def estimated_geometry_points(self) -> int:
        return self.way_count * GEOMETRY_POINTS_PER_WAY_ESTIMATE


@dataclass(frozen=True)
class TransformerDownloadEstimate:
    country_code: str
    country_name: str
    node_count: int
    estimated_osm_json_bytes: int
    estimated_geojson_bytes: int
    estimated_gpkg_bytes: int
    query_seconds: float


@dataclass(frozen=True)
class SubstationDownloadEstimate:
    country_code: str
    country_name: str
    voltages_v: tuple[int, ...]
    feature_count: int
    estimated_osm_json_bytes: int
    estimated_geojson_bytes: int
    estimated_gpkg_bytes: int
    query_seconds: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate and download OSM power lines, transformer nodes, and "
            "voltage-filtered substations within a country."
        )
    )
    parser.add_argument(
        "--country",
        default=DEFAULT_COUNTRY,
        metavar="ISO2",
        help=f"ISO 3166-1 alpha-2 country code (default: {DEFAULT_COUNTRY} = Tunisia)",
    )
    parser.add_argument(
        "--voltages",
        default=",".join(str(v) for v in DEFAULT_VOLTAGES_KV),
        help="Comma-separated voltages in kV (default: 225,150)",
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
            "Power line output file path "
            "(default: data/power-grid-infrastructure/powerlines_<country>_<voltages>kv_<timestamp>.<format>)"
        ),
    )
    parser.add_argument(
        "--transformer-output",
        type=Path,
        help=(
            "Transformer node output file path "
            "(default: data/power-grid-infrastructure/transformers_<country>_<timestamp>.<format>)"
        ),
    )
    parser.add_argument(
        "--substation-output",
        type=Path,
        help=(
            "Substation output file path "
            "(default: data/power-grid-infrastructure/substations_<country>_<voltages>kv_<timestamp>.<format>)"
        ),
    )
    feature_group = parser.add_mutually_exclusive_group()
    feature_group.add_argument(
        "--lines-only",
        action="store_true",
        help="Download only power lines",
    )
    feature_group.add_argument(
        "--transformers-only",
        action="store_true",
        help="Download only electricity transformer nodes (power=transformer)",
    )
    feature_group.add_argument(
        "--substations-only",
        action="store_true",
        help="Download only electrical substations (power=substation) at target voltages",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt and download immediately after estimate",
    )
    return parser.parse_args()


def kv_to_volts(kv_values: Iterable[str]) -> tuple[int, ...]:
    volts: list[int] = []
    for raw in kv_values:
        token = raw.strip()
        if not token:
            continue
        try:
            kv = float(token)
        except ValueError as exc:
            raise ValueError(f"Invalid voltage value: {raw!r}") from exc
        volts.append(int(round(kv * 1_000)))
    if not volts:
        raise ValueError("At least one voltage must be provided.")
    return tuple(sorted(set(volts)))


def country_display_name(code: str) -> str:
    normalized = code.strip().upper()
    return COUNTRY_NAMES.get(normalized, normalized)


def overpass_regex_for_volts(volts: tuple[int, ...]) -> str:
    # Broad match; strict filtering happens in Python after download.
    return "|".join(str(v) for v in volts)


def build_count_query(country_code: str, volts: tuple[int, ...]) -> str:
    voltage_regex = overpass_regex_for_volts(volts)
    return f"""
[out:json][timeout:120];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
way(area.country)["power"="line"]["voltage"~"{voltage_regex}"];
out count;
""".strip()


def build_download_query(country_code: str, volts: tuple[int, ...]) -> str:
    voltage_regex = overpass_regex_for_volts(volts)
    return f"""
[out:json][timeout:600];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
way(area.country)["power"="line"]["voltage"~"{voltage_regex}"];
out geom;
""".strip()


def build_transformer_count_query(country_code: str) -> str:
    return f"""
[out:json][timeout:120];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
node(area.country)["power"="transformer"];
out count;
""".strip()


def build_transformer_download_query(country_code: str) -> str:
    return f"""
[out:json][timeout:600];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
node(area.country)["power"="transformer"];
out body;
""".strip()


def build_substation_count_query(country_code: str, volts: tuple[int, ...]) -> str:
    voltage_regex = overpass_regex_for_volts(volts)
    return f"""
[out:json][timeout:120];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
(
  node(area.country)["power"="substation"]["voltage"~"{voltage_regex}"];
  way(area.country)["power"="substation"]["voltage"~"{voltage_regex}"];
);
out count;
""".strip()


def build_substation_download_query(country_code: str, volts: tuple[int, ...]) -> str:
    voltage_regex = overpass_regex_for_volts(volts)
    return f"""
[out:json][timeout:600];
area["ISO3166-1"="{country_code.upper()}"][admin_level=2]->.country;
(
  node(area.country)["power"="substation"]["voltage"~"{voltage_regex}"];
  way(area.country)["power"="substation"]["voltage"~"{voltage_regex}"];
);
out center;
""".strip()


def post_overpass(query: str, timeout: int) -> requests.Response:
    headers = {"User-Agent": "osm-powerline-downloader/1.0 (educational GIS script)"}
    last_error: Exception | None = None

    for server in OVERPASS_SERVERS:
        try:
            response = requests.post(
                server,
                data={"data": query},
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            if response.text.lstrip().startswith("<"):
                raise RuntimeError("Overpass returned HTML instead of JSON.")
            return response
        except Exception as exc:  # noqa: BLE001 - retry next mirror
            last_error = exc
            continue

    raise RuntimeError(f"All Overpass servers failed. Last error: {last_error}")


def parse_count_response(payload: dict, *, element_key: str) -> int:
    for element in payload.get("elements", []):
        if element.get("type") == "count":
            return int(element.get("tags", {}).get(element_key, 0))
    raise RuntimeError(f"Overpass count response did not include {element_key} count.")


def parse_union_count_response(payload: dict) -> int:
    for element in payload.get("elements", []):
        if element.get("type") == "count":
            tags = element.get("tags", {})
            if "total" in tags:
                return int(tags["total"])
            return int(tags.get("nodes", 0)) + int(tags.get("ways", 0))
    raise RuntimeError("Overpass count response did not include feature count.")


def estimate_line_download(
    country_code: str, volts: tuple[int, ...]
) -> LineDownloadEstimate:
    country_code = country_code.upper()
    started = time.perf_counter()
    response = post_overpass(build_count_query(country_code, volts), timeout=180)
    way_count = parse_count_response(response.json(), element_key="ways")
    elapsed = time.perf_counter() - started

    osm_json_bytes = max(way_count, 1) * BYTES_PER_WAY_ESTIMATE
    geojson_bytes = int(osm_json_bytes * GEOJSON_OVERHEAD_FACTOR)
    gpkg_bytes = int(geojson_bytes * GPKG_COMPRESSION_FACTOR)

    return LineDownloadEstimate(
        country_code=country_code,
        country_name=country_display_name(country_code),
        voltages_v=volts,
        way_count=way_count,
        estimated_osm_json_bytes=osm_json_bytes,
        estimated_geojson_bytes=geojson_bytes,
        estimated_gpkg_bytes=gpkg_bytes,
        query_seconds=elapsed,
    )


def estimate_substation_download(
    country_code: str, volts: tuple[int, ...]
) -> SubstationDownloadEstimate:
    country_code = country_code.upper()
    started = time.perf_counter()
    response = post_overpass(build_substation_count_query(country_code, volts), timeout=180)
    feature_count = parse_union_count_response(response.json())
    elapsed = time.perf_counter() - started

    osm_json_bytes = max(feature_count, 1) * BYTES_PER_NODE_ESTIMATE
    geojson_bytes = int(osm_json_bytes * GEOJSON_OVERHEAD_FACTOR)
    gpkg_bytes = int(geojson_bytes * GPKG_COMPRESSION_FACTOR)

    return SubstationDownloadEstimate(
        country_code=country_code,
        country_name=country_display_name(country_code),
        voltages_v=volts,
        feature_count=feature_count,
        estimated_osm_json_bytes=osm_json_bytes,
        estimated_geojson_bytes=geojson_bytes,
        estimated_gpkg_bytes=gpkg_bytes,
        query_seconds=elapsed,
    )


def estimate_transformer_download(country_code: str) -> TransformerDownloadEstimate:
    country_code = country_code.upper()
    started = time.perf_counter()
    response = post_overpass(build_transformer_count_query(country_code), timeout=180)
    node_count = parse_count_response(response.json(), element_key="nodes")
    elapsed = time.perf_counter() - started

    osm_json_bytes = max(node_count, 1) * BYTES_PER_NODE_ESTIMATE
    geojson_bytes = int(osm_json_bytes * GEOJSON_OVERHEAD_FACTOR)
    gpkg_bytes = int(geojson_bytes * GPKG_COMPRESSION_FACTOR)

    return TransformerDownloadEstimate(
        country_code=country_code,
        country_name=country_display_name(country_code),
        node_count=node_count,
        estimated_osm_json_bytes=osm_json_bytes,
        estimated_geojson_bytes=geojson_bytes,
        estimated_gpkg_bytes=gpkg_bytes,
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


def print_line_estimate(
    estimate: LineDownloadEstimate, output_format: str, output_path: Path
) -> None:
    selected_bytes = (
        estimate.estimated_gpkg_bytes
        if output_format == "gpkg"
        else estimate.estimated_geojson_bytes
    )
    voltages_kv = ", ".join(str(v // 1000) for v in estimate.voltages_v)

    print("Power lines")
    print("-" * 60)
    print(f"Country:            {estimate.country_name} ({estimate.country_code})")
    print(f"Voltages:           {voltages_kv} kV")
    print(f"Matching powerlines: {estimate.way_count:,} ways")
    print(
        "Geometry points:    "
        f"~{estimate.estimated_geometry_points:,} "
        f"(~{GEOMETRY_POINTS_PER_WAY_ESTIMATE} per line, estimated)"
    )
    print(f"Count query time:     {estimate.query_seconds:.1f} s")
    print(f"Output file:          {output_path}")
    print(f"Estimated output ({output_format.upper()}): ~{human_bytes(selected_bytes)}")


def print_substation_estimate(
    estimate: SubstationDownloadEstimate, output_format: str, output_path: Path
) -> None:
    selected_bytes = (
        estimate.estimated_gpkg_bytes
        if output_format == "gpkg"
        else estimate.estimated_geojson_bytes
    )
    voltages_kv = ", ".join(str(v // 1000) for v in estimate.voltages_v)

    print("Electrical substations")
    print("-" * 60)
    print(f"Country:            {estimate.country_name} ({estimate.country_code})")
    print(f"Voltages:           {voltages_kv} kV")
    print("OSM filter:         power=substation (nodes and way centroids)")
    print(f"Matching features:  {estimate.feature_count:,}")
    print(f"Count query time:     {estimate.query_seconds:.1f} s")
    print(f"Output file:          {output_path}")
    print(f"Estimated output ({output_format.upper()}): ~{human_bytes(selected_bytes)}")


def print_transformer_estimate(
    estimate: TransformerDownloadEstimate, output_format: str, output_path: Path
) -> None:
    selected_bytes = (
        estimate.estimated_gpkg_bytes
        if output_format == "gpkg"
        else estimate.estimated_geojson_bytes
    )

    print("Electricity transformers (nodes)")
    print("-" * 60)
    print(f"Country:            {estimate.country_name} ({estimate.country_code})")
    print("OSM filter:         power=transformer")
    print(f"Matching nodes:     {estimate.node_count:,}")
    print(f"Count query time:     {estimate.query_seconds:.1f} s")
    print(f"Output file:          {output_path}")
    print(f"Estimated output ({output_format.upper()}): ~{human_bytes(selected_bytes)}")


def print_estimates(
    *,
    line_estimate: LineDownloadEstimate | None,
    transformer_estimate: TransformerDownloadEstimate | None,
    substation_estimate: SubstationDownloadEstimate | None,
    output_format: str,
    line_output_path: Path | None,
    transformer_output_path: Path | None,
    substation_output_path: Path | None,
) -> None:
    print()
    print("=" * 60)
    print("DOWNLOAD ESTIMATE (no data downloaded yet)")
    print("=" * 60)
    if line_estimate is not None and line_output_path is not None:
        print_line_estimate(line_estimate, output_format, line_output_path)
        print()
    if transformer_estimate is not None and transformer_output_path is not None:
        print_transformer_estimate(
            transformer_estimate, output_format, transformer_output_path
        )
        print()
    if substation_estimate is not None and substation_output_path is not None:
        print_substation_estimate(
            substation_estimate, output_format, substation_output_path
        )
        print()
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
    headers = {"User-Agent": "osm-powerline-downloader/1.0 (educational GIS script)"}
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


def parse_voltage_values(raw_value: object) -> set[int]:
    if raw_value is None:
        return set()

    text = str(raw_value).strip().lower()
    if not text or text in {"unknown", "none", "n/a"}:
        return set()

    values: set[int] = set()
    for token in re.split(r"[;/,\s]+", text):
        token = token.strip()
        if not token:
            continue

        kv_match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*kv", token)
        if kv_match:
            values.add(int(round(float(kv_match.group(1)) * 1_000)))
            continue

        digits = re.sub(r"[^\d]", "", token)
        if digits:
            values.add(int(digits))

    return values


def way_matches_voltages(tags: dict, target_volts: set[int]) -> bool:
    return bool(parse_voltage_values(tags.get("voltage")) & target_volts)


def substation_matches_voltages(tags: dict, target_volts: set[int]) -> bool:
    voltage_keys = (
        "voltage",
        "voltage:primary",
        "voltage:secondary",
        "voltage-high",
        "voltage-low",
    )
    for key in voltage_keys:
        if parse_voltage_values(tags.get(key)) & target_volts:
            return True
    return False


def osm_way_to_linestring(way: dict) -> LineString | None:
    geometry = way.get("geometry") or []
    if len(geometry) < 2:
        return None
    coords = [(point["lon"], point["lat"]) for point in geometry]
    return LineString(coords)


def osm_to_geodataframe(payload: dict, target_volts: tuple[int, ...]) -> gpd.GeoDataFrame:
    target_set = set(target_volts)
    features: list[dict] = []

    for element in payload.get("elements", []):
        if element.get("type") != "way":
            continue

        tags = element.get("tags", {})
        if not way_matches_voltages(tags, target_set):
            continue

        line = osm_way_to_linestring(element)
        if line is None:
            continue

        row = {
            "osm_id": element.get("id"),
            "geometry": line,
            "voltage": tags.get("voltage"),
            "name": tags.get("name"),
            "operator": tags.get("operator"),
            "cables": tags.get("cables"),
            "circuits": tags.get("circuits"),
            "power": tags.get("power"),
        }
        features.append(row)

    if not features:
        return gpd.GeoDataFrame(
            columns=[
                "osm_id",
                "geometry",
                "voltage",
                "name",
                "operator",
                "cables",
                "circuits",
                "power",
            ],
            geometry="geometry",
            crs="EPSG:4326",
        )

    return gpd.GeoDataFrame(features, geometry="geometry", crs="EPSG:4326")


def osm_node_to_point(node: dict) -> Point | None:
    if "lat" not in node or "lon" not in node:
        return None
    return Point(node["lon"], node["lat"])


def osm_element_to_point(element: dict) -> Point | None:
    element_type = element.get("type")
    if element_type == "node":
        return osm_node_to_point(element)

    if element_type == "way":
        center = element.get("center") or {}
        if "lat" in center and "lon" in center:
            return Point(center["lon"], center["lat"])
    return None


def osm_to_transformers_geodataframe(payload: dict) -> gpd.GeoDataFrame:
    features: list[dict] = []

    for element in payload.get("elements", []):
        if element.get("type") != "node":
            continue

        tags = element.get("tags", {})
        if tags.get("power") != "transformer":
            continue

        point = osm_node_to_point(element)
        if point is None:
            continue

        features.append(
            {
                "osm_id": element.get("id"),
                "geometry": point,
                "power": tags.get("power"),
                "name": tags.get("name"),
                "operator": tags.get("operator"),
                "voltage": tags.get("voltage"),
                "voltage_high": tags.get("voltage:high") or tags.get("voltage-high"),
                "voltage_low": tags.get("voltage:low") or tags.get("voltage-low"),
                "transformer": tags.get("transformer"),
                "substation": tags.get("substation"),
            }
        )

    columns = [
        "osm_id",
        "geometry",
        "power",
        "name",
        "operator",
        "voltage",
        "voltage_high",
        "voltage_low",
        "transformer",
        "substation",
    ]
    if not features:
        return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs="EPSG:4326")

    return gpd.GeoDataFrame(features, geometry="geometry", crs="EPSG:4326")


def osm_to_substations_geodataframe(
    payload: dict, target_volts: tuple[int, ...]
) -> gpd.GeoDataFrame:
    target_set = set(target_volts)
    features: list[dict] = []

    for element in payload.get("elements", []):
        if element.get("type") not in {"node", "way"}:
            continue

        tags = element.get("tags", {})
        if tags.get("power") != "substation":
            continue
        if not substation_matches_voltages(tags, target_set):
            continue

        point = osm_element_to_point(element)
        if point is None:
            continue

        features.append(
            {
                "osm_id": element.get("id"),
                "osm_type": element.get("type"),
                "geometry": point,
                "power": tags.get("power"),
                "name": tags.get("name"),
                "operator": tags.get("operator"),
                "voltage": tags.get("voltage"),
                "voltage_primary": tags.get("voltage:primary"),
                "voltage_secondary": tags.get("voltage:secondary"),
                "substation": tags.get("substation"),
            }
        )

    columns = [
        "osm_id",
        "osm_type",
        "geometry",
        "power",
        "name",
        "operator",
        "voltage",
        "voltage_primary",
        "voltage_secondary",
        "substation",
    ]
    if not features:
        return gpd.GeoDataFrame(columns=columns, geometry="geometry", crs="EPSG:4326")

    return gpd.GeoDataFrame(features, geometry="geometry", crs="EPSG:4326")


def download_timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")


def default_line_output_path(
    country_code: str, volts: tuple[int, ...], fmt: str, timestamp: str
) -> Path:
    kv_label = "_".join(str(v // 1000) for v in volts)
    return DATA_DIR / f"powerlines_{country_code.lower()}_{kv_label}kv_{timestamp}.{fmt}"


def default_transformer_output_path(country_code: str, fmt: str, timestamp: str) -> Path:
    return DATA_DIR / f"transformers_{country_code.lower()}_{timestamp}.{fmt}"


def default_substation_output_path(
    country_code: str, volts: tuple[int, ...], fmt: str, timestamp: str
) -> Path:
    kv_label = "_".join(str(v // 1000) for v in volts)
    return DATA_DIR / f"substations_{country_code.lower()}_{kv_label}kv_{timestamp}.{fmt}"


def write_output(gdf: gpd.GeoDataFrame, output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "gpkg":
        gdf.to_file(output_path, driver="GPKG")
    else:
        gdf.to_file(output_path, driver="GeoJSON")


def download_and_save_lines(
    country_code: str,
    volts: tuple[int, ...],
    output_path: Path,
    output_format: str,
    estimate: LineDownloadEstimate,
) -> int:
    query = build_download_query(country_code, volts)
    try:
        raw_bytes = download_with_progress(query, estimate.estimated_osm_json_bytes)
        payload = json.loads(raw_bytes)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Power line download failed: {exc}", file=sys.stderr)
        return 1

    gdf = osm_to_geodataframe(payload, volts)
    if gdf.empty:
        print(
            "Power line download finished, but no features matched the strict voltage "
            f"filter ({', '.join(str(v // 1000) for v in volts)} kV)."
        )
        return 1

    try:
        write_output(gdf, output_path, output_format)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Failed to write power line output: {exc}", file=sys.stderr)
        return 1

    file_size = output_path.stat().st_size
    print()
    print(f"Saved {len(gdf):,} power lines to {output_path.resolve()}")
    print(f"Output size: {human_bytes(file_size)}")
    return 0


def download_and_save_transformers(
    country_code: str,
    output_path: Path,
    output_format: str,
    estimate: TransformerDownloadEstimate,
) -> int:
    query = build_transformer_download_query(country_code)
    try:
        raw_bytes = download_with_progress(query, estimate.estimated_osm_json_bytes)
        payload = json.loads(raw_bytes)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Transformer download failed: {exc}", file=sys.stderr)
        return 1

    gdf = osm_to_transformers_geodataframe(payload)
    if gdf.empty:
        print("Transformer download finished, but no power=transformer nodes were found.")
        return 1

    try:
        write_output(gdf, output_path, output_format)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Failed to write transformer output: {exc}", file=sys.stderr)
        return 1

    file_size = output_path.stat().st_size
    print()
    print(f"Saved {len(gdf):,} transformer nodes to {output_path.resolve()}")
    print(f"Output size: {human_bytes(file_size)}")
    return 0


def download_and_save_substations(
    country_code: str,
    volts: tuple[int, ...],
    output_path: Path,
    output_format: str,
    estimate: SubstationDownloadEstimate,
) -> int:
    query = build_substation_download_query(country_code, volts)
    try:
        raw_bytes = download_with_progress(query, estimate.estimated_osm_json_bytes)
        payload = json.loads(raw_bytes)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Substation download failed: {exc}", file=sys.stderr)
        return 1

    gdf = osm_to_substations_geodataframe(payload, volts)
    if gdf.empty:
        print(
            "Substation download finished, but no features matched the strict voltage "
            f"filter ({', '.join(str(v // 1000) for v in volts)} kV)."
        )
        return 1

    try:
        write_output(gdf, output_path, output_format)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Failed to write substation output: {exc}", file=sys.stderr)
        return 1

    file_size = output_path.stat().st_size
    print()
    print(f"Saved {len(gdf):,} substations to {output_path.resolve()}")
    print(f"Output size: {human_bytes(file_size)}")
    return 0


def resolve_feature_flags(args: argparse.Namespace) -> tuple[bool, bool, bool]:
    if args.lines_only:
        return True, False, False
    if args.transformers_only:
        return False, True, False
    if args.substations_only:
        return False, False, True
    return True, True, True


def main() -> int:
    args = parse_args()
    country_code = args.country.strip().upper()
    volts = kv_to_volts(args.voltages.split(","))
    output_format = args.format
    download_lines, download_transformers, download_substations = resolve_feature_flags(args)

    snapshot_timestamp = download_timestamp()

    line_output_path = (
        (
            args.output
            or default_line_output_path(
                country_code, volts, output_format, snapshot_timestamp
            )
        )
        if download_lines
        else None
    )
    transformer_output_path = (
        (
            args.transformer_output
            or default_transformer_output_path(
                country_code, output_format, snapshot_timestamp
            )
        )
        if download_transformers
        else None
    )
    substation_output_path = (
        (
            args.substation_output
            or default_substation_output_path(
                country_code, volts, output_format, snapshot_timestamp
            )
        )
        if download_substations
        else None
    )

    line_estimate: LineDownloadEstimate | None = None
    transformer_estimate: TransformerDownloadEstimate | None = None
    substation_estimate: SubstationDownloadEstimate | None = None

    try:
        if download_lines:
            line_estimate = estimate_line_download(country_code, volts)
        if download_transformers:
            transformer_estimate = estimate_transformer_download(country_code)
        if download_substations:
            substation_estimate = estimate_substation_download(country_code, volts)
    except Exception as exc:  # noqa: BLE001 - surface clean CLI error
        print(f"Estimate failed: {exc}", file=sys.stderr)
        return 1

    print_estimates(
        line_estimate=line_estimate,
        transformer_estimate=transformer_estimate,
        substation_estimate=substation_estimate,
        output_format=output_format,
        line_output_path=line_output_path.resolve() if line_output_path else None,
        transformer_output_path=(
            transformer_output_path.resolve() if transformer_output_path else None
        ),
        substation_output_path=(
            substation_output_path.resolve() if substation_output_path else None
        ),
    )

    line_count = line_estimate.way_count if line_estimate else 0
    transformer_count = transformer_estimate.node_count if transformer_estimate else 0
    substation_count = substation_estimate.feature_count if substation_estimate else 0
    if line_count == 0 and transformer_count == 0 and substation_count == 0:
        print(
            "No matching power lines, transformer nodes, or substations found. "
            "Nothing to download."
        )
        return 0

    if not args.yes and not confirm_download():
        print("Download cancelled.")
        return 0

    exit_code = 0

    if download_lines and line_estimate and line_output_path:
        if line_estimate.way_count == 0:
            print("No matching power lines found. Skipping line download.")
        else:
            exit_code = max(
                exit_code,
                download_and_save_lines(
                    country_code,
                    volts,
                    line_output_path,
                    output_format,
                    line_estimate,
                ),
            )

    if download_transformers and transformer_estimate and transformer_output_path:
        if transformer_estimate.node_count == 0:
            print("No matching transformer nodes found. Skipping transformer download.")
        else:
            exit_code = max(
                exit_code,
                download_and_save_transformers(
                    country_code,
                    transformer_output_path,
                    output_format,
                    transformer_estimate,
                ),
            )

    if download_substations and substation_estimate and substation_output_path:
        if substation_estimate.feature_count == 0:
            print("No matching substations found. Skipping substation download.")
        else:
            exit_code = max(
                exit_code,
                download_and_save_substations(
                    country_code,
                    volts,
                    substation_output_path,
                    output_format,
                    substation_estimate,
                ),
            )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
