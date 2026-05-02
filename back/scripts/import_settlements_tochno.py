#!/usr/bin/env python3
"""
Import settlements from the "Если быть точным" allsettlements dataset into PostGIS.

Supported input formats:
  - CSV, UTF-8, semicolon-separated by default
  - XLSX
  - Parquet

Typical usage:
  python scripts/import_settlements_tochno.py \
    --file data/allsettlements.csv \
    --database-url postgresql+psycopg2://postgres:postgres@localhost:5432/railway_gis \
    --replace-source

District-scoped import:
  python scripts/import_settlements_tochno.py \
    --file data/allsettlements.csv \
    --district "Центральный федеральный округ" \
    --replace-source

If automatic column detection fails, pass explicit column names, for example:
  python scripts/import_settlements_tochno.py \
    --file data/allsettlements.csv \
    --name-column object_name \
    --object-level-column object_level \
    --population-column population \
    --lat-column lat \
    --lon-column lon
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv is optional
    load_dotenv = None

LOGGER = logging.getLogger("import_settlements_tochno")

DEFAULT_SOURCE = "tochno_allsettlements"
DEFAULT_POPULATION_YEAR = 2021


ENV_CANDIDATES = [
    Path.cwd() / ".env",
    Path.cwd() / "back" / ".env",
    Path(__file__).resolve().parents[1] / ".env",
    Path(__file__).resolve().parents[2] / ".env",
]


COLUMN_ALIASES: dict[str, list[str]] = {
    "source_row_id": [
        "id",
        "row_id",
        "source_id",
        "object_id",
        "territory_id",
        "oktmo_id",
    ],
    "object_level": [
        "object_level",
        "level",
        "territory_level",
        "area_level",
        "уровень",
        "уровень объекта",
        "уровень территории",
    ],
    "name": [
        "name",
        "object_name",
        "territory_name",
        "area_name",
        "settlement",
        "settlement_name",
        "locality_name",
        "place_name",
        "наименование",
        "название",
        "название территории",
        "населенный пункт",
        "населённый пункт",
    ],
    "settlement_type": [
        "type",
        "object_type",
        "settlement_type",
        "place_type",
        "locality_type",
        "тип",
        "тип населенного пункта",
        "тип населённого пункта",
        "вид населенного пункта",
        "вид населённого пункта",
    ],
    "federal_district": [
        "federal_district",
        "fed_district",
        "district",
        "fo",
        "округ",
        "федеральный округ",
    ],
    "region": [
        "region",
        "subject",
        "subject_rf",
        "region_name",
        "субъект",
        "субъект рф",
        "регион",
        "наименование субъекта",
    ],
    "municipality": [
        "municipality",
        "municipality_down_name",
        "municipality_up_name",
        "municipal_district",
        "municipal_region",
        "municipality_name",
        "mo",
        "mun_obr",
        "муниципалитет",
        "муниципальное образование",
        "муниципальный район",
        "городской округ",
        "мо верхнего уровня",
        "муниципальное образование верхнего уровня",
        "муниципальное образование нижнего уровня",
    ],
    "oktmo": [
        "oktmo",
        "oktmo_code",
        "code_oktmo",
        "октмо",
        "код октмо",
    ],
    "population": [
        "population",
        "population_total",
        "total_population",
        "pop",
        "people",
        "численность населения",
        "население",
        "всего население",
        "всего",
    ],
    "lat": [
        "lat",
        "latitude",
        "latitude_dd",
        "geo_lat",
        "dd_lat",
        "y",
        "широта",
    ],
    "lon": [
        "lon",
        "lng",
        "long",
        "longitude",
        "longitude_dd",
        "geo_lon",
        "geo_lng",
        "dd_lon",
        "x",
        "долгота",
    ],
    "coords": [
        "coords",
        "coordinates",
        "geo",
        "point",
        "координаты",
        "геокоординаты",
    ],
}

MANUAL_COLUMN_ARGS = {
    "source_row_id": "source_row_id_column",
    "object_level": "object_level_column",
    "name": "name_column",
    "settlement_type": "type_column",
    "federal_district": "district_column",
    "region": "region_column",
    "municipality": "municipality_column",
    "oktmo": "oktmo_column",
    "population": "population_column",
    "lat": "lat_column",
    "lon": "lon_column",
    "coords": "coords_column",
}


@dataclass(frozen=True)
class ColumnMap:
    source_row_id: str | None
    object_level: str | None
    name: str
    settlement_type: str | None
    federal_district: str | None
    region: str | None
    municipality: str | None
    oktmo: str | None
    population: str | None
    lat: str | None
    lon: str | None
    coords: str | None


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text_value = str(value).strip().replace("ё", "е").replace("Ё", "Е")
    text_value = re.sub(r"\s+", " ", text_value)
    return text_value


def normalize_key(value: Any) -> str:
    text_value = normalize_text(value).lower()
    text_value = text_value.replace("-", "_")
    text_value = re.sub(r"[^0-9a-zа-я_]+", "_", text_value)
    text_value = re.sub(r"_+", "_", text_value)
    return text_value.strip("_")


def as_optional_str(value: Any) -> str | None:
    text_value = normalize_text(value)
    if not text_value:
        return None
    if normalize_key(text_value) in {
        "значение_отсутствует",
        "нет",
        "nan",
        "none",
        "null",
    }:
        return None
    return text_value


def parse_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        if value < 0 or math.isnan(value):
            return None
        return int(round(value))

    text_value = str(value).strip()
    if not text_value:
        return None

    text_value = text_value.replace("\u00a0", " ")
    text_value = text_value.replace(" ", "")
    text_value = text_value.replace(",", ".")
    text_value = re.sub(r"[^0-9.\-]", "", text_value)

    if not text_value or text_value in {"-", "."}:
        return None

    try:
        parsed = float(text_value)
    except ValueError:
        return None

    if parsed < 0 or math.isnan(parsed):
        return None
    return int(round(parsed))


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, float):
        return value if not math.isnan(value) else None
    if isinstance(value, int):
        return float(value)

    text_value = str(value).strip()
    if not text_value:
        return None

    text_value = text_value.replace("\u00a0", " ").replace(" ", "")
    text_value = text_value.replace(",", ".")
    text_value = re.sub(r"[^0-9.\-]", "", text_value)

    try:
        parsed = float(text_value)
    except ValueError:
        return None

    if math.isnan(parsed):
        return None
    return parsed


def parse_coords(value: Any) -> tuple[float | None, float | None]:
    """Parse coordinate strings like '55.75, 37.62' or '37.62 55.75'."""
    if value is None:
        return None, None

    text_value = str(value).strip()
    if not text_value:
        return None, None

    numbers = re.findall(r"-?\d+(?:[\.,]\d+)?", text_value)
    if len(numbers) < 2:
        return None, None

    first = parse_float(numbers[0])
    second = parse_float(numbers[1])
    if first is None or second is None:
        return None, None

    # Most Russian datasets store coordinates as lat, lon. Some GIS exports use lon, lat.
    lat, lon = first, second
    if abs(lat) > 90 and abs(lon) <= 90:
        lat, lon = lon, lat
    elif abs(lat) <= 90 and abs(lon) <= 180:
        # Keep as lat/lon.
        pass

    return lat, lon


def valid_lat_lon(lat: float | None, lon: float | None) -> bool:
    return (
        lat is not None
        and lon is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def get_row_value(row: pd.Series, *column_names: str) -> Any:
    normalized_to_original = {normalize_key(column): column for column in row.index}
    for column_name in column_names:
        normalized = normalize_key(column_name)
        original = normalized_to_original.get(normalized)
        if original is not None:
            return row[original]
    return None


def looks_like_admin_part(name: str | None) -> bool:
    if not name:
        return False
    key = normalize_key(name)
    return any(
        marker in key
        for marker in [
            "округ",
            "район",
            "внутригород",
            "муниципальный_округ",
            "административный_округ",
        ]
    )


def derive_city_name_from_municipality(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_text(value)
    match = re.search("город +(.+)$", normalized, flags=re.IGNORECASE)
    if match:
        return f"г. {match.group(1).strip()}"
    return None


def derive_canonical_name(row: pd.Series, column_map: ColumnMap) -> str | None:
    raw_name = as_optional_str(row[column_map.name])
    municipality_down = as_optional_str(get_row_value(row, "municipality_down_name"))
    municipality_up = as_optional_str(get_row_value(row, "municipality_up_name"))

    if raw_name and not looks_like_admin_part(raw_name):
        return raw_name

    if municipality_down and not looks_like_admin_part(municipality_down):
        return municipality_down

    derived_from_down = derive_city_name_from_municipality(municipality_down)
    if derived_from_down:
        return derived_from_down

    derived_from_up = derive_city_name_from_municipality(municipality_up)
    if derived_from_up:
        return derived_from_up

    if municipality_up and not looks_like_admin_part(municipality_up):
        return municipality_up

    return raw_name


def infer_settlement_type(row: pd.Series, column_map: ColumnMap) -> str | None:
    explicit_type = None
    if column_map.settlement_type:
        explicit_type = as_optional_str(row[column_map.settlement_type])
    if explicit_type:
        return explicit_type

    settlement_type_full = as_optional_str(get_row_value(row, "settlement_type_full"))
    if settlement_type_full:
        return settlement_type_full

    city_type_full = as_optional_str(get_row_value(row, "city_type_full"))
    if city_type_full:
        return city_type_full

    city_type = as_optional_str(get_row_value(row, "city_type"))
    if city_type:
        return city_type

    return None


def best_municipality(row: pd.Series, column_map: ColumnMap) -> str | None:
    municipality_down = as_optional_str(get_row_value(row, "municipality_down_name"))
    municipality_up = as_optional_str(get_row_value(row, "municipality_up_name"))
    if municipality_down:
        return municipality_down
    if municipality_up:
        return municipality_up
    if column_map.municipality:
        return as_optional_str(row[column_map.municipality])
    return None


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def row_to_payload(row: pd.Series) -> dict[str, Any]:
    return {str(key): to_jsonable(value) for key, value in row.items()}


def read_input_file(path: Path, sep: str, encoding: str) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, sep=sep, encoding=encoding, low_memory=False)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported file format: {suffix}. Use CSV, XLSX, or Parquet.")


def resolve_manual_column(df: pd.DataFrame, requested: str | None) -> str | None:
    if not requested:
        return None
    if requested in df.columns:
        return requested

    normalized_requested = normalize_key(requested)
    for column in df.columns:
        if normalize_key(column) == normalized_requested:
            return str(column)

    raise ValueError(f"Column '{requested}' was passed explicitly but was not found in the file.")


def detect_column(df: pd.DataFrame, logical_name: str) -> str | None:
    normalized_to_original = {normalize_key(column): str(column) for column in df.columns}
    for alias in COLUMN_ALIASES[logical_name]:
        normalized_alias = normalize_key(alias)
        if normalized_alias in normalized_to_original:
            return normalized_to_original[normalized_alias]

    # Dataset versions may suffix metric columns with years, for example
    # population_2021, population_total_2021, pop_2021, etc.
    if logical_name == "population":
        population_markers = ["population", "pop", "насел", "числен"]
        year_markers = ["2021", "2020", "2010", "впн"]
        candidates: list[str] = []
        for column in df.columns:
            key = normalize_key(column)
            if any(marker in key for marker in population_markers):
                candidates.append(str(column))
        for column in candidates:
            key = normalize_key(column)
            if any(marker in key for marker in year_markers):
                return column
        if candidates:
            return candidates[0]

    return None


def build_column_map(df: pd.DataFrame, args: argparse.Namespace) -> ColumnMap:
    values: dict[str, str | None] = {}

    for logical_name in COLUMN_ALIASES:
        manual_arg_name = MANUAL_COLUMN_ARGS[logical_name]
        manual_value = getattr(args, manual_arg_name)
        values[logical_name] = resolve_manual_column(df, manual_value) or detect_column(df, logical_name)

    if not values["name"]:
        available = ", ".join(map(str, df.columns[:80]))
        raise ValueError(
            "Could not detect the settlement name column. "
            "Pass --name-column explicitly. "
            f"Available columns start with: {available}"
        )

    if not ((values["lat"] and values["lon"]) or values["coords"]):
        available = ", ".join(map(str, df.columns[:80]))
        raise ValueError(
            "Could not detect latitude/longitude columns. "
            "Pass --lat-column and --lon-column, or --coords-column explicitly. "
            f"Available columns start with: {available}"
        )

    return ColumnMap(
        source_row_id=values["source_row_id"],
        object_level=values["object_level"],
        name=values["name"],
        settlement_type=values["settlement_type"],
        federal_district=values["federal_district"],
        region=values["region"],
        municipality=values["municipality"],
        oktmo=values["oktmo"],
        population=values["population"],
        lat=values["lat"],
        lon=values["lon"],
        coords=values["coords"],
    )


def is_settlement_level(value: Any) -> bool:
    level = normalize_text(value).lower()
    if not level:
        return True
    if "аноним" in level:
        return False
    return (
        "населен" in level
        or "населён" in level
        or "settlement" in level
        or "locality" in level
        or level in {"нп", "np"}
    )


def matches_any_filter(value: Any, filters: list[str]) -> bool:
    if not filters:
        return True
    normalized_value = normalize_key(value)
    return any(normalize_key(item) == normalized_value for item in filters)


def load_env_files() -> None:
    if load_dotenv is None:
        return

    seen: set[Path] = set()
    for env_path in ENV_CANDIDATES:
        resolved = env_path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            load_dotenv(resolved, override=False)
            LOGGER.info("Loaded environment variables from %s", resolved)


def build_database_url_from_env() -> str | None:
    """Build SQLAlchemy URL from common .env variable names if DATABASE_URL is absent."""
    direct_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")
    if direct_url:
        return direct_url

    user = os.getenv("POSTGRES_USER") or os.getenv("DB_USER") or os.getenv("DATABASE_USER")
    password = os.getenv("POSTGRES_PASSWORD") or os.getenv("DB_PASSWORD") or os.getenv("DATABASE_PASSWORD")
    host = os.getenv("POSTGRES_HOST") or os.getenv("DB_HOST") or os.getenv("DATABASE_HOST") or "localhost"
    port = os.getenv("POSTGRES_PORT") or os.getenv("DB_PORT") or os.getenv("DATABASE_PORT") or "5432"
    database = os.getenv("POSTGRES_DB") or os.getenv("DB_NAME") or os.getenv("DATABASE_NAME")

    if not user or not password or not database:
        return None

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


def quote_ident(identifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
        raise ValueError(f"Unsafe SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def table_ref(schema: str, table: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table)}"


def create_schema_and_table(engine: Engine, schema: str, table: str) -> None:
    ref = table_ref(schema, table)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(schema)}"))
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {ref} (
                    id BIGSERIAL PRIMARY KEY,

                    source TEXT NOT NULL DEFAULT 'tochno_allsettlements',
                    source_row_id TEXT,

                    object_level TEXT,
                    canonical_name TEXT NOT NULL,
                    settlement_type TEXT,

                    federal_district TEXT,
                    region TEXT,
                    municipality TEXT,
                    oktmo TEXT,

                    population INTEGER,
                    population_year INTEGER DEFAULT 2021,

                    lat DOUBLE PRECISION,
                    lon DOUBLE PRECISION,
                    geom geometry(Point, 4326),

                    raw_payload JSONB,

                    created_at TIMESTAMP DEFAULT now(),
                    updated_at TIMESTAMP DEFAULT now()
                )
                """
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_geom "
                f"ON {ref} USING GIST (geom)"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_population "
                f"ON {ref} (population)"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_federal_district "
                f"ON {ref} (federal_district)"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_region "
                f"ON {ref} (region)"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_oktmo "
                f"ON {ref} (oktmo)"
            )
        )
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_name_region "
                f"ON {ref} (canonical_name, region)"
            )
        )


def delete_existing_source(engine: Engine, schema: str, table: str, source: str) -> None:
    ref = table_ref(schema, table)
    with engine.begin() as conn:
        result = conn.execute(text(f"DELETE FROM {ref} WHERE source = :source"), {"source": source})
        LOGGER.info("Deleted %s existing rows for source=%s", result.rowcount, source)


def truncate_table(engine: Engine, schema: str, table: str) -> None:
    ref = table_ref(schema, table)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {ref} RESTART IDENTITY"))
        LOGGER.info("Truncated %s", ref)


def build_insert_sql(schema: str, table: str) -> str:
    ref = table_ref(schema, table)
    return f"""
        INSERT INTO {ref} (
            source,
            source_row_id,
            object_level,
            canonical_name,
            settlement_type,
            federal_district,
            region,
            municipality,
            oktmo,
            population,
            population_year,
            lat,
            lon,
            geom,
            raw_payload,
            updated_at
        )
        VALUES (
            :source,
            :source_row_id,
            :object_level,
            :canonical_name,
            :settlement_type,
            :federal_district,
            :region,
            :municipality,
            :oktmo,
            :population,
            :population_year,
            :lat,
            :lon,
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
            CAST(:raw_payload AS jsonb),
            now()
        )
    """


def iter_normalized_records(
    df: pd.DataFrame,
    column_map: ColumnMap,
    args: argparse.Namespace,
) -> Iterable[dict[str, Any]]:
    skipped_level = 0
    skipped_district = 0
    skipped_region = 0
    skipped_population = 0
    skipped_coords = 0
    skipped_name = 0

    for index, row in df.iterrows():
        if column_map.object_level and not args.no_level_filter:
            if not is_settlement_level(row[column_map.object_level]):
                skipped_level += 1
                continue

        if column_map.federal_district and args.district:
            if not matches_any_filter(row[column_map.federal_district], args.district):
                skipped_district += 1
                continue

        if column_map.region and args.region:
            if not matches_any_filter(row[column_map.region], args.region):
                skipped_region += 1
                continue

        name = derive_canonical_name(row, column_map)
        if not name:
            skipped_name += 1
            continue

        population = parse_int(row[column_map.population]) if column_map.population else None
        if args.min_population is not None:
            if population is None or population < args.min_population:
                skipped_population += 1
                continue

        if column_map.lat and column_map.lon:
            lat = parse_float(row[column_map.lat])
            lon = parse_float(row[column_map.lon])
            if lat is not None and lon is not None and abs(lat) > 90 and abs(lon) <= 90:
                lat, lon = lon, lat
        elif column_map.coords:
            lat, lon = parse_coords(row[column_map.coords])
        else:
            lat, lon = None, None

        if not valid_lat_lon(lat, lon):
            skipped_coords += 1
            continue

        source_row_id = None
        if column_map.source_row_id:
            source_row_id = as_optional_str(row[column_map.source_row_id])
        if source_row_id is None:
            source_row_id = str(index)

        yield {
            "source": args.source,
            "source_row_id": source_row_id,
            "object_level": as_optional_str(row[column_map.object_level]) if column_map.object_level else None,
            "canonical_name": name,
            "settlement_type": infer_settlement_type(row, column_map),
            "federal_district": as_optional_str(row[column_map.federal_district]) if column_map.federal_district else None,
            "region": as_optional_str(row[column_map.region]) if column_map.region else None,
            "municipality": best_municipality(row, column_map),
            "oktmo": as_optional_str(row[column_map.oktmo]) if column_map.oktmo else None,
            "population": population,
            "population_year": args.population_year,
            "lat": lat,
            "lon": lon,
            "raw_payload": json.dumps(row_to_payload(row), ensure_ascii=False),
        }

    LOGGER.info(
        "Skipped rows: level=%s, district=%s, region=%s, population=%s, coords=%s, name=%s",
        skipped_level,
        skipped_district,
        skipped_region,
        skipped_population,
        skipped_coords,
        skipped_name,
    )


def aggregate_duplicate_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse city-district rows into one settlement row.

    The 21 507-row Tochno dataset may contain one row per city district for
    large cities. For analytics we need one point per settlement, so population
    is summed for rows with the same settlement identity.
    """
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}

    for record in records:
        raw_payload = json.loads(record["raw_payload"])
        fias_code = as_optional_str(raw_payload.get("fias_code"))
        oktmo = record.get("oktmo")
        region = record.get("region")
        name = record.get("canonical_name")
        # Do not include coordinates in the strongest grouping key: city
        # districts of the same city may have slightly different geocoded points,
        # but they still belong to the same settlement by FIAS/OKTMO/name.
        if fias_code or oktmo:
            key = (region, oktmo, fias_code, name)
        else:
            lat_bucket = round(float(record["lat"]), 3) if record.get("lat") is not None else None
            lon_bucket = round(float(record["lon"]), 3) if record.get("lon") is not None else None
            key = (region, name, lat_bucket, lon_bucket)
        groups.setdefault(key, []).append(record)

    aggregated: list[dict[str, Any]] = []
    merged_groups = 0
    merged_rows = 0

    for key, group in groups.items():
        if len(group) == 1:
            aggregated.append(group[0])
            continue

        merged_groups += 1
        merged_rows += len(group)

        base = dict(group[0])
        populations = [item.get("population") for item in group if item.get("population") is not None]
        base["population"] = int(sum(populations)) if populations else None
        base["source_row_id"] = "agg:" + ":".join(str(part) for part in key if part is not None)

        lats = [float(item["lat"]) for item in group if item.get("lat") is not None]
        lons = [float(item["lon"]) for item in group if item.get("lon") is not None]
        if lats and lons:
            base["lat"] = sum(lats) / len(lats)
            base["lon"] = sum(lons) / len(lons)

        raw_rows = [json.loads(item["raw_payload"]) for item in group]
        base["raw_payload"] = json.dumps(
            {
                "aggregated": True,
                "aggregation_method": "sum_population_by_region_oktmo_fias_name",
                "rows_count": len(group),
                "source_rows": raw_rows,
            },
            ensure_ascii=False,
        )
        aggregated.append(base)

    second_pass_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    passthrough: list[dict[str, Any]] = []

    for record in aggregated:
        name = record.get("canonical_name")
        settlement_type = normalize_key(record.get("settlement_type"))
        is_city = bool(name and str(name).startswith("г.")) or settlement_type in {"г", "город"}
        if is_city:
            second_pass_groups.setdefault((record.get("region"), name), []).append(record)
        else:
            passthrough.append(record)

    second_pass: list[dict[str, Any]] = []
    second_pass_merged_groups = 0
    second_pass_merged_rows = 0

    for key, group in second_pass_groups.items():
        if len(group) == 1:
            second_pass.append(group[0])
            continue

        second_pass_merged_groups += 1
        second_pass_merged_rows += len(group)

        base = dict(group[0])
        populations = [item.get("population") for item in group if item.get("population") is not None]
        base["population"] = int(sum(populations)) if populations else None
        base["source_row_id"] = "city_agg:" + ":".join(str(part) for part in key if part is not None)

        lats = [float(item["lat"]) for item in group if item.get("lat") is not None]
        lons = [float(item["lon"]) for item in group if item.get("lon") is not None]
        if lats and lons:
            base["lat"] = sum(lats) / len(lats)
            base["lon"] = sum(lons) / len(lons)

        raw_rows = [json.loads(item["raw_payload"]) for item in group]
        base["raw_payload"] = json.dumps(
            {
                "aggregated": True,
                "aggregation_method": "second_pass_sum_city_population_by_region_name",
                "rows_count": len(group),
                "source_rows": raw_rows,
            },
            ensure_ascii=False,
        )
        second_pass.append(base)

    result = passthrough + second_pass

    LOGGER.info(
        "Aggregated duplicate settlement rows: groups=%s, source_rows_in_groups=%s, output_rows=%s",
        merged_groups,
        merged_rows,
        len(aggregated),
    )
    LOGGER.info(
        "Second-pass city aggregation: groups=%s, source_rows_in_groups=%s, output_rows=%s",
        second_pass_merged_groups,
        second_pass_merged_rows,
        len(result),
    )
    return result


def insert_records(
    engine: Engine,
    schema: str,
    table: str,
    records: Iterable[dict[str, Any]],
    batch_size: int,
    dry_run: bool,
) -> int:
    insert_sql = text(build_insert_sql(schema, table))
    total = 0
    batch: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal total, batch
        if not batch:
            return
        if dry_run:
            total += len(batch)
            LOGGER.info("Dry run: would insert %s rows, total=%s", len(batch), total)
            batch = []
            return
        with engine.begin() as conn:
            conn.execute(insert_sql, batch)
        total += len(batch)
        LOGGER.info("Inserted %s rows, total=%s", len(batch), total)
        batch = []

    for record in records:
        batch.append(record)
        if len(batch) >= batch_size:
            flush()
    flush()
    return total


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import settlements from the 'Если быть точным' allsettlements dataset into PostGIS."
    )

    parser.add_argument("--file", required=True, help="Path to CSV, XLSX, or Parquet file.")
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "SQLAlchemy database URL. If omitted, the script tries DATABASE_URL, "
            "SQLALCHEMY_DATABASE_URL, or common POSTGRES_/DB_ variables from .env."
        ),
    )
    parser.add_argument("--schema", default="public", help="Target DB schema. Default: public.")
    parser.add_argument("--table", default="settlements", help="Target table. Default: settlements.")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help=f"Source name. Default: {DEFAULT_SOURCE}.")

    parser.add_argument("--sep", default=";", help="CSV separator. Default: ';'.")
    parser.add_argument("--encoding", default="utf-8", help="CSV encoding. Default: utf-8.")
    parser.add_argument("--batch-size", type=int, default=2000, help="Insert batch size. Default: 2000.")
    parser.add_argument("--population-year", type=int, default=DEFAULT_POPULATION_YEAR)

    parser.add_argument(
        "--district",
        action="append",
        default=[],
        help="Federal district filter. Can be repeated. Exact normalized match is used.",
    )
    parser.add_argument(
        "--region",
        action="append",
        default=[],
        help="Region filter. Can be repeated. Exact normalized match is used.",
    )
    parser.add_argument(
        "--min-population",
        type=int,
        default=None,
        help="Optional import-time population filter.",
    )

    parser.add_argument(
        "--replace-source",
        action="store_true",
        help="Delete existing rows with the same source before import.",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate the whole target table before import. Be careful.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and normalize data, but do not write to the database.",
    )
    parser.add_argument(
        "--no-create-table",
        action="store_true",
        help="Do not create table/indexes automatically.",
    )
    parser.add_argument(
        "--no-level-filter",
        action="store_true",
        help="Do not filter by object_level. Useful if the dataset has only settlement rows or column names are unusual.",
    )
    parser.add_argument(
        "--no-aggregate",
        action="store_true",
        help="Do not collapse duplicate rows for cities split by districts. Not recommended for analytics.",
    )
    parser.add_argument(
        "--print-columns",
        action="store_true",
        help="Print available columns and detected mapping, then exit before inserting.",
    )

    # Manual column overrides.
    parser.add_argument("--source-row-id-column")
    parser.add_argument("--object-level-column")
    parser.add_argument("--name-column")
    parser.add_argument("--type-column")
    parser.add_argument("--district-column")
    parser.add_argument("--region-column")
    parser.add_argument("--municipality-column")
    parser.add_argument("--oktmo-column")
    parser.add_argument("--population-column")
    parser.add_argument("--lat-column")
    parser.add_argument("--lon-column")
    parser.add_argument("--coords-column")

    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    load_env_files()

    args = parse_args(argv)
    if not args.database_url:
        args.database_url = build_database_url_from_env()

    file_path = Path(args.file)
    if not file_path.exists():
        LOGGER.error("Input file does not exist: %s", file_path)
        return 2

    if not args.database_url and not args.dry_run and not args.print_columns:
        LOGGER.error("--database-url is required unless --dry-run or --print-columns is used.")
        return 2

    LOGGER.info("Reading input file: %s", file_path)
    df = read_input_file(file_path, sep=args.sep, encoding=args.encoding)
    LOGGER.info("Loaded %s rows and %s columns", len(df), len(df.columns))

    column_map = build_column_map(df, args)
    LOGGER.info("Detected column mapping: %s", column_map)

    if args.print_columns:
        print("\nAvailable columns:")
        for column in df.columns:
            print(f"  - {column}")
        print("\nDetected mapping:")
        print(column_map)
        return 0

    records: Iterable[dict[str, Any]] = iter_normalized_records(df, column_map, args)
    if not args.no_aggregate:
        records = aggregate_duplicate_records(records)

    if args.dry_run:
        total = insert_records(
            engine=None,  # type: ignore[arg-type]
            schema=args.schema,
            table=args.table,
            records=records,
            batch_size=args.batch_size,
            dry_run=True,
        )
        LOGGER.info("Dry run finished. Rows that would be inserted: %s", total)
        return 0

    engine = create_engine(args.database_url, future=True)

    if not args.no_create_table:
        create_schema_and_table(engine, args.schema, args.table)

    if args.truncate:
        truncate_table(engine, args.schema, args.table)
    elif args.replace_source:
        delete_existing_source(engine, args.schema, args.table, args.source)

    inserted = insert_records(
        engine=engine,
        schema=args.schema,
        table=args.table,
        records=records,
        batch_size=args.batch_size,
        dry_run=False,
    )
    LOGGER.info("Import finished. Inserted rows: %s", int(inserted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
