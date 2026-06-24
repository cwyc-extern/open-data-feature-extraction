#!/usr/bin/env python3
"""
Convert downloaded infrastructure GeoPackages to Open Fibre Data Standard (OFDS) 0.3.0.

Outputs are written under data/OFDS-schema/, preserving the power-grid-infrastructure
and telecommunications subfolder layout. Each source file produces:

  - <basename>_package.json   OFDS network package (networks array)
  - <basename>_spans.geojson  OFDS GeoJSON spans (LineString features)
  - <basename>_nodes.geojson  OFDS GeoJSON nodes (Point features)

Reference: https://standard.ofds.info/en/0.3/reference/publication_formats/geojson.html
Schema:    https://raw.githubusercontent.com/Open-Telecoms-Data/open-fibre-data-standard/0__3__0/schema/network-schema.json

Power-grid features are mapped onto OFDS spans (lines) and nodes (substations,
transformers) using extension fields prefixed with x_ for domain-specific attributes
(voltage, OSM tags, etc.) so layers remain comparable in QGIS.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import geopandas as gpd
import libcoveofds.geojson
from shapely.geometry import LineString, MultiLineString, Point, mapping

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
INPUT_CATEGORIES = ("power-grid-infrastructure", "telecommunications")
OUTPUT_ROOT = DATA_DIR / "OFDS-schema"

OFDS_SCHEMA_URL = (
    "https://raw.githubusercontent.com/Open-Telecoms-Data/"
    "open-fibre-data-standard/0__3__0/schema/network-schema.json"
)
OFDS_CRS = {
    "name": "urn:ogc:def:crs:OGC::CRS84",
    "uri": "http://www.opengis.net/def/crs/OGC/1.3/CRS84",
}
OFDS_NAMESPACE = uuid.UUID("646f6473-6d61-7020-0000-000000000001")

DEFAULT_COUNTRIES = ("TN", "MA", "EG", "ID", "VN", "IN", "BD")

COUNTRY_NAMES = {
    "TN": "Tunisia",
    "MA": "Morocco",
    "EG": "Egypt",
    "ID": "Indonesia",
    "VN": "Vietnam",
    "IN": "India",
    "BD": "Bangladesh",
}

TIMESTAMP_RE = re.compile(r"(\d{8}T\d{6})")


@dataclass(frozen=True)
class ConversionResult:
    source: Path
    spans: int
    nodes: int
    package_path: Path
    spans_path: Path
    nodes_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert data/power-grid-infrastructure and data/telecommunications "
            "GeoPackages to OFDS 0.3.0 JSON and GeoJSON under data/OFDS-schema/."
        )
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DATA_DIR,
        help="Root data directory (default: data/)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=OUTPUT_ROOT,
        help="OFDS output root (default: data/OFDS-schema/)",
    )
    parser.add_argument(
        "--countries",
        default=",".join(DEFAULT_COUNTRIES),
        help=(
            "Comma-separated ISO2 country codes to include when a file name "
            f"contains a country code (default: {','.join(DEFAULT_COUNTRIES)}). "
            "Use 'all' to convert every file."
        ),
    )
    parser.add_argument(
        "--categories",
        default=",".join(INPUT_CATEGORIES),
        help="Comma-separated input categories under data/ to convert",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing OFDS outputs",
    )
    parser.add_argument(
        "--skip-geojson",
        action="store_true",
        help="Write package JSON only (skip libcoveofds GeoJSON export)",
    )
    return parser.parse_args()


def parse_country_filter(raw: str) -> set[str] | None:
    if raw.strip().lower() == "all":
        return None
    codes = {token.strip().upper() for token in raw.split(",") if token.strip()}
    if not codes:
        raise ValueError("At least one country code is required unless --countries all.")
    return codes


def extract_timestamp(path: Path) -> str | None:
    match = TIMESTAMP_RE.search(path.stem)
    return match.group(1) if match else None


def extract_country_code(path: Path) -> str | None:
    stem = path.stem.lower()
    aliases = {"tun": "TN"}
    for alias, code in aliases.items():
        if re.search(rf"(?:^|_){alias}(?:_|$)", stem):
            return code
    for code in DEFAULT_COUNTRIES:
        if re.search(rf"(?:^|_){code.lower()}(?:_|$)", stem):
            return code
    return None


def detect_source_type(path: Path) -> str:
    stem = path.stem.lower()
    if "powerlines" in stem or "powerline" in stem:
        return "osm_powerlines"
    if "transformer" in stem:
        return "osm_transformers"
    if "substation" in stem:
        return "osm_substations"
    if "un_geoportal" in stem:
        return "un_geoportal_powerlines"
    if "osm_fibre" in stem:
        return "osm_fibre"
    if "afterfibre" in stem:
        return "afterfibre"
    if "itu_bbmaps" in stem:
        return "itu_bbmaps"
    return "unknown"


def should_convert(path: Path, country_filter: set[str] | None) -> bool:
    if country_filter is None:
        return True
    code = extract_country_code(path)
    if code is None:
        return False
    return code in country_filter


def geometry_kind(gdf: gpd.GeoDataFrame) -> str:
    kinds = set(gdf.geometry.geom_type.unique())
    if kinds <= {"Point"}:
        return "point"
    if kinds <= {"LineString"}:
        return "line"
    if "Point" in kinds and "LineString" in kinds:
        return "mixed"
    return "other"


def clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_")


def stable_id(*parts: str) -> str:
    key = "|".join(parts)
    return str(uuid.uuid5(OFDS_NAMESPACE, key))


def parse_collection_date(path: Path) -> str:
    timestamp = extract_timestamp(path)
    if not timestamp:
        return date.today().isoformat()
    try:
        parsed = datetime.strptime(timestamp, "%Y%m%dT%H%M%S")
        return parsed.date().isoformat()
    except ValueError:
        return date.today().isoformat()


def map_span_status(source_type: str, row: dict[str, Any]) -> str | None:
    if source_type == "itu_bbmaps":
        text = clean_text(row.get("type_inf")) or clean_text(row.get("status")) or ""
        lowered = text.lower()
        if "operational" in lowered or "active" in lowered:
            return "operational"
        if "under construction" in lowered or "construction" in lowered:
            return "underConstruction"
        if "planned" in lowered:
            return "planned"
        if "proposed" in lowered:
            return "proposed"
        if "decommission" in lowered:
            return "decommissioned"
        return None

    if source_type == "afterfibre":
        live = clean_text(row.get("live"))
        if live and live.lower() in {"yes", "true", "1", "live"}:
            return "operational"
        if live and live.lower() in {"no", "false", "0"}:
            return "planned"
        return "operational"

    if source_type == "osm_fibre":
        return "operational"

    if source_type in {"osm_powerlines", "un_geoportal_powerlines"}:
        return "operational"

    return "operational"


def map_node_status(source_type: str, row: dict[str, Any]) -> str | None:
    if source_type in {"osm_substations", "osm_transformers", "un_geoportal_powerlines"}:
        return "operational"
    return "operational"


def map_transmission_medium(source_type: str, row: dict[str, Any]) -> list[str] | None:
    if source_type == "itu_bbmaps":
        text = (clean_text(row.get("type_inf")) or "").lower()
        if "microwave" in text:
            return ["microwave"]
        if "fibre" in text or "fiber" in text:
            return ["fibre"]
        return None

    if source_type == "afterfibre":
        return ["fibre"]

    if source_type == "osm_fibre":
        medium = (clean_text(row.get("telecom_medium")) or "").lower()
        if medium == "fibre":
            return ["fibre"]
        seamark = (clean_text(row.get("seamark_category")) or "").lower()
        if seamark == "fibre_optic":
            return ["fibre"]
        if (clean_text(row.get("location")) or "").lower() == "underwater":
            return ["fibre"]
        return ["fibre"]

    return None


def map_deployment(source_type: str, row: dict[str, Any]) -> list[str] | None:
    location = (clean_text(row.get("location")) or "").lower()
    if location == "underwater":
        return ["submarine"]
    if location == "underground":
        return ["underground"]
    if location in {"overhead", "overground"}:
        return ["aerial"]

    if source_type == "osm_fibre":
        if (clean_text(row.get("submarine")) or "").lower() == "yes":
            return ["submarine"]
        if (clean_text(row.get("seamark_type")) or "").lower() == "cable_submarine":
            return ["submarine"]

    if source_type in {"osm_powerlines", "un_geoportal_powerlines"}:
        return ["aerial"]

    return None


def organisation_ref(name: str | None, org_id: str) -> dict[str, Any] | None:
    if not name:
        return None
    return {"id": org_id, "name": name}


def iter_linestrings(geometry: object) -> Iterator[LineString]:
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        yield geometry
        return
    if isinstance(geometry, MultiLineString):
        for part in geometry.geoms:
            if not part.is_empty:
                yield part
        return
    mapped = mapping(geometry)
    if mapped.get("type") == "LineString":
        yield LineString(mapped["coordinates"])
    elif mapped.get("type") == "MultiLineString":
        for coords in mapped["coordinates"]:
            yield LineString(coords)


def linestring_coords(geometry: LineString) -> list[list[float]] | None:
    if geometry is None or geometry.is_empty:
        return None
    return [[float(x), float(y)] for x, y in geometry.coords]


def point_coords(geometry: object) -> list[float] | None:
    if geometry is None or geometry.is_empty:
        return None
    if isinstance(geometry, Point):
        return [float(geometry.x), float(geometry.y)]
    mapped = mapping(geometry)
    if mapped.get("type") == "Point":
        return mapped["coordinates"]
    return None


def span_from_row(
    *,
    source_path: Path,
    source_type: str,
    row: dict[str, Any],
    index: int,
    part_index: int = 0,
    geometry: LineString,
) -> dict[str, Any] | None:
    coords = linestring_coords(geometry)
    if not coords:
        return None

    source_id = (
        clean_text(row.get("osm_id"))
        or clean_text(row.get("uid"))
        or clean_text(row.get("cartodb_id"))
        or clean_text(row.get("objectid"))
        or clean_text(row.get("id"))
        or str(index)
    )
    span_key = f"{source_id}:{part_index}" if part_index else source_id
    span_id = stable_id(source_type, str(source_path), "span", span_key)

    span: dict[str, Any] = {
        "id": span_id,
        "status": map_span_status(source_type, row),
        "route": {"type": "LineString", "coordinates": coords},
    }

    name = clean_text(row.get("name")) or clean_text(row.get("phase_name"))
    if name:
        span["name"] = name

    medium = map_transmission_medium(source_type, row)
    if medium:
        span["transmissionMedium"] = medium

    deployment = map_deployment(source_type, row)
    if deployment:
        span["deployment"] = deployment

    operator = clean_text(row.get("operator")) or clean_text(row.get("operator_name"))
    if operator:
        org_id = stable_id("org", operator)
        span["networkProviders"] = [organisation_ref(operator, org_id)]

    owner = clean_text(row.get("owner")) or clean_text(row.get("owner_name"))
    if owner:
        org_id = stable_id("org", owner)
        span["physicalInfrastructureProvider"] = organisation_ref(owner, org_id)

    span["x_sourceDataset"] = source_path.name
    span["x_sourceSchema"] = source_type
    span["x_sourceFeatureId"] = source_id
    span["x_infrastructureDomain"] = (
        "electricityTransmission"
        if source_type in {"osm_powerlines", "un_geoportal_powerlines"}
        else "telecommunications"
    )

    if source_type in {"osm_powerlines", "osm_substations", "osm_transformers"}:
        if clean_text(row.get("voltage")):
            span["x_voltage"] = clean_text(row.get("voltage"))
        if clean_text(row.get("power")):
            span["x_powerTag"] = clean_text(row.get("power"))

    if source_type == "itu_bbmaps":
        if clean_text(row.get("type_inf")):
            span["x_typeInf"] = clean_text(row.get("type_inf"))
        if clean_text(row.get("country_name")):
            span["x_countryName"] = clean_text(row.get("country_name"))
        if clean_text(row.get("iso2")):
            span["x_iso2"] = clean_text(row.get("iso2"))

    if source_type == "afterfibre":
        for key in ("technology", "type", "fibre_cores", "go_live", "iso2", "country"):
            value = clean_text(row.get(key))
            if value:
                span[f"x_{key}"] = value

    if source_type == "osm_fibre":
        for key in (
            "primary_tag",
            "communication",
            "telecom",
            "telecom_medium",
            "location",
            "capacity",
            "ref",
            "seamark_category",
        ):
            value = clean_text(row.get(key))
            if value:
                span[f"x_{key}"] = value

    if source_type == "un_geoportal_powerlines":
        for key in ("country", "length_km", "source_year", "mainsource"):
            value = clean_text(row.get(key))
            if value:
                span[f"x_{key}"] = value

    return span


def node_from_row(
    *,
    source_path: Path,
    source_type: str,
    row: dict[str, Any],
    index: int,
) -> dict[str, Any] | None:
    coords = point_coords(row.get("geometry"))
    if not coords:
        return None

    source_id = clean_text(row.get("osm_id")) or str(index)
    node_id = stable_id(source_type, str(source_path), "node", source_id)

    node: dict[str, Any] = {
        "id": node_id,
        "status": map_node_status(source_type, row),
        "location": {"type": "Point", "coordinates": coords},
    }

    name = clean_text(row.get("name"))
    if name:
        node["name"] = name

    if source_type == "osm_substations":
        node["type"] = ["exchange"]
        node["power"] = True
    elif source_type == "osm_transformers":
        node["type"] = ["transition"]
        node["power"] = True
    else:
        node["type"] = ["transition"]

    operator = clean_text(row.get("operator"))
    if operator:
        org_id = stable_id("org", operator)
        node["networkProviders"] = [organisation_ref(operator, org_id)]

    node["x_sourceDataset"] = source_path.name
    node["x_sourceSchema"] = source_type
    node["x_sourceFeatureId"] = source_id
    node["x_infrastructureDomain"] = "electricityTransmission"
    node["x_powerFeatureType"] = (
        "substation" if source_type == "osm_substations" else "transformer"
    )

    for key in ("voltage", "voltage_primary", "voltage_secondary", "substation", "power"):
        value = clean_text(row.get(key))
        if value:
            node[f"x_{key}"] = value

    return node


def build_network(
    *,
    source_path: Path,
    category: str,
    gdf: gpd.GeoDataFrame,
) -> dict[str, Any]:
    source_type = detect_source_type(source_path)
    country = extract_country_code(source_path)
    timestamp = extract_timestamp(source_path)
    country_label = COUNTRY_NAMES.get(country or "", country or "Global")

    network_id = stable_id("network", category, str(source_path))
    network_name = f"{country_label} {source_type.replace('_', ' ')} ({source_path.stem})"

    spans: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []
    kind = geometry_kind(gdf)

    for index, row in enumerate(gdf.to_dict(orient="records")):
        if kind in {"line", "mixed", "other"}:
            for part_index, line in enumerate(iter_linestrings(row.get("geometry"))):
                span = span_from_row(
                    source_path=source_path,
                    source_type=source_type,
                    row=row,
                    index=index,
                    part_index=part_index,
                    geometry=line,
                )
                if span:
                    spans.append(span)
        if kind in {"point", "mixed"}:
            node = node_from_row(
                source_path=source_path,
                source_type=source_type,
                row=row,
                index=index,
            )
            if node:
                nodes.append(node)

    network: dict[str, Any] = {
        "id": network_id,
        "name": network_name,
        "publisher": {"name": "646 ODP map setup"},
        "publicationDate": date.today().isoformat(),
        "collectionDate": parse_collection_date(source_path),
        "crs": OFDS_CRS,
        "language": "en",
        "links": [{"rel": "describedby", "href": OFDS_SCHEMA_URL}],
        "x_sourceCategory": category,
        "x_sourceFile": str(source_path.relative_to(DATA_DIR)),
        "x_sourceType": source_type,
    }
    if country:
        network["x_countryCode"] = country
    if timestamp:
        network["x_snapshotTimestamp"] = timestamp
    if spans:
        network["spans"] = spans
    if nodes:
        network["nodes"] = nodes

    return {"networks": [network]}


def output_paths(output_dir: Path, source_path: Path, input_root: Path) -> tuple[Path, Path, Path]:
    relative = source_path.relative_to(input_root)
    stem = source_path.stem
    target_dir = output_dir / relative.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    package_path = target_dir / f"{stem}_package.json"
    spans_path = target_dir / f"{stem}_spans.geojson"
    nodes_path = target_dir / f"{stem}_nodes.geojson"
    return package_path, spans_path, nodes_path


def write_package(package: dict[str, Any], package_path: Path) -> None:
    with package_path.open("w", encoding="utf-8") as handle:
        json.dump(package, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def export_geojson(package: dict[str, Any], spans_path: Path, nodes_path: Path) -> None:
    converter = libcoveofds.geojson.JSONToGeoJSONConverter()
    converter.process_package(package)
    with spans_path.open("w", encoding="utf-8") as handle:
        json.dump(converter.get_spans_geojson(), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    with nodes_path.open("w", encoding="utf-8") as handle:
        json.dump(converter.get_nodes_geojson(), handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def convert_file(
    *,
    source_path: Path,
    category: str,
    input_root: Path,
    output_dir: Path,
    force: bool,
    skip_geojson: bool,
) -> ConversionResult | None:
    package_path, spans_path, nodes_path = output_paths(output_dir, source_path, input_root)
    if not force and package_path.exists() and spans_path.exists() and nodes_path.exists():
        print(f"Skip existing: {package_path.relative_to(output_dir)}")
        return None

    gdf = gpd.read_file(source_path)
    if gdf.empty:
        print(f"Skip empty: {source_path}")
        return None

    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    else:
        gdf = gdf.to_crs("EPSG:4326")

    package = build_network(source_path=source_path, category=category, gdf=gdf)
    network = package["networks"][0]
    span_count = len(network.get("spans", []))
    node_count = len(network.get("nodes", []))
    if span_count == 0 and node_count == 0:
        print(f"Skip no convertible features: {source_path}")
        return None

    write_package(package, package_path)

    if not skip_geojson:
        export_geojson(package, spans_path, nodes_path)

    return ConversionResult(
        source=source_path,
        spans=span_count,
        nodes=node_count,
        package_path=package_path,
        spans_path=spans_path,
        nodes_path=nodes_path,
    )


def discover_sources(input_root: Path, categories: tuple[str, ...]) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for category in categories:
        category_dir = input_root / category
        if not category_dir.is_dir():
            continue
        for path in sorted(category_dir.rglob("*.gpkg")):
            if "OFDS-schema" in path.parts:
                continue
            files.append((category, path))
    return files


def main() -> int:
    args = parse_args()
    input_root = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()
    country_filter = parse_country_filter(args.countries)
    categories = tuple(
        token.strip()
        for token in args.categories.split(",")
        if token.strip()
    )

    sources = discover_sources(input_root, categories)
    if not sources:
        print("No .gpkg files found to convert.", file=sys.stderr)
        return 1

    results: list[ConversionResult] = []
    skipped = 0

    print(f"Input root:  {input_root}")
    print(f"Output root: {output_dir}")
    if country_filter is None:
        print("Country filter: all files")
    else:
        print(f"Country filter: {', '.join(sorted(country_filter))}")
    print()

    for category, source_path in sources:
        if not should_convert(source_path, country_filter):
            skipped += 1
            continue

        try:
            result = convert_file(
                source_path=source_path,
                category=category,
                input_root=input_root,
                output_dir=output_dir,
                force=args.force,
                skip_geojson=args.skip_geojson,
            )
        except Exception as exc:  # noqa: BLE001 - report per-file failures
            print(f"Failed: {source_path} ({exc})", file=sys.stderr)
            continue

        if result is None:
            continue

        results.append(result)
        rel = result.package_path.relative_to(output_dir)
        print(
            f"Converted {source_path.name}: "
            f"{result.spans} spans, {result.nodes} nodes -> {rel}"
        )

    print()
    print(
        f"Done: {len(results)} file(s) converted, "
        f"{skipped} skipped by country filter."
    )
    if results:
        total_spans = sum(item.spans for item in results)
        total_nodes = sum(item.nodes for item in results)
        print(f"Totals: {total_spans:,} spans, {total_nodes:,} nodes")
        print()
        print("Load the *_spans.geojson and *_nodes.geojson layers in QGIS.")
        print("Each feature carries OFDS properties plus x_* source attributes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
