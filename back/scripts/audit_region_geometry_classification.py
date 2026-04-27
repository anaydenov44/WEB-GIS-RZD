import argparse
import json
import sys
import time
from datetime import datetime, UTC
from decimal import Decimal
from pathlib import Path

BACK_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACK_DIR.parent

if str(BACK_DIR) not in sys.path:
    sys.path.insert(0, str(BACK_DIR))

from sqlalchemy import text

from app.db import engine

FEDERAL_DISTRICTS_GEOJSON_PATH = ROOT_DIR / "front" / "public" / "federal-districts.geojson"
OUTPUT_PATH = BACK_DIR / "data" / "audit" / "region_geometry_classification.json"

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

DEFAULT_SAMPLE_LIMIT = 100
LOW_SHARE_THRESHOLD = 0.50


def log_step(title: str):
    print(f"[START] {title}", flush=True)
    started_at = time.perf_counter()

    def done():
        elapsed = time.perf_counter() - started_at
        print(f"[DONE]  {title} ({elapsed:.2f}s)", flush=True)

    return done


def normalize_for_json(value):
    if isinstance(value, dict):
        return {key: normalize_for_json(val) for key, val in value.items()}

    if isinstance(value, list):
        return [normalize_for_json(item) for item in value]

    if isinstance(value, tuple):
        return [normalize_for_json(item) for item in value]

    if isinstance(value, set):
        return [normalize_for_json(item) for item in sorted(value)]

    if isinstance(value, Decimal):
        return float(value)

    return value


def load_federal_district_features(selected_region_code: str | None) -> list[dict]:
    if not FEDERAL_DISTRICTS_GEOJSON_PATH.exists():
        raise FileNotFoundError(
            f"GeoJSON not found: {FEDERAL_DISTRICTS_GEOJSON_PATH}"
        )

    payload = json.loads(FEDERAL_DISTRICTS_GEOJSON_PATH.read_text(encoding="utf-8"))
    features = payload.get("features", [])

    result = []
    for feature in features:
        properties = feature.get("properties", {}) or {}
        geometry = feature.get("geometry")

        region_code = properties.get("code")
        region_name = properties.get("name")

        if not region_code or not geometry:
            continue

        if selected_region_code and region_code != selected_region_code:
            continue

        result.append(
            {
                "region_code": region_code,
                "region_name": region_name,
                "geometry_json": json.dumps(geometry, ensure_ascii=False),
            }
        )

    return result


def fetch_scalar(connection, sql: str, params: dict | None = None):
    return connection.execute(text(sql), params or {}).scalar_one()


def fetch_rows(connection, sql: str, params: dict | None = None) -> list[dict]:
    result = connection.execute(text(sql), params or {})
    return [dict(row._mapping) for row in result]


def build_region_filter_sql(alias: str, selected_region_code: str | None) -> tuple[str, dict]:
    if not selected_region_code:
        return "", {}

    return f" AND {alias}.region_code = :selected_region_code", {
        "selected_region_code": selected_region_code
    }


def prepare_temp_boundaries(connection, district_features: list[dict]) -> None:
    done = log_step("prepare temp federal district boundaries")

    connection.execute(text("DROP TABLE IF EXISTS tmp_federal_district_boundaries;"))

    connection.execute(text("""
        CREATE TEMP TABLE tmp_federal_district_boundaries (
            region_code TEXT PRIMARY KEY,
            region_name TEXT NOT NULL,
            geom geometry(MULTIPOLYGON, 4326) NOT NULL
        ) ON COMMIT DROP;
    """))

    insert_sql = text("""
        INSERT INTO tmp_federal_district_boundaries (
            region_code,
            region_name,
            geom
        )
        VALUES (
            :region_code,
            :region_name,
            ST_Multi(
                ST_SetSRID(
                    ST_GeomFromGeoJSON(:geometry_json),
                    4326
                )
            )
        );
    """)

    for item in district_features:
        connection.execute(insert_sql, item)

    connection.execute(text("""
        CREATE INDEX tmp_federal_district_boundaries_geom_idx
        ON tmp_federal_district_boundaries
        USING GIST (geom);
    """))

    done()


def build_report(
    connection,
    selected_region_code: str | None,
    sample_limit: int,
    deep_low_share: bool,
) -> dict:
    region_filter_stations_sql, region_filter_stations_params = build_region_filter_sql(
        "s",
        selected_region_code,
    )
    region_filter_lines_sql, region_filter_lines_params = build_region_filter_sql(
        "l",
        selected_region_code,
    )

    summary = {}
    stations_section = {}
    rail_lines_section = {}

    done = log_step("count totals")
    summary["stations_total"] = fetch_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM stations s
        WHERE 1=1
        {region_filter_stations_sql};
        """,
        region_filter_stations_params,
    )
    summary["rail_lines_total"] = fetch_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM rail_lines l
        WHERE 1=1
        {region_filter_lines_sql};
        """,
        region_filter_lines_params,
    )
    done()

    done = log_step("classify stations")
    station_counts = fetch_rows(
        connection,
        f"""
        WITH station_hits AS (
            SELECT
                s.id,
                s.region_code,
                COUNT(b.region_code) AS matched_regions_count,
                BOOL_OR(b.region_code = s.region_code) AS inside_own_region,
                STRING_AGG(DISTINCT b.region_code, ', ' ORDER BY b.region_code) AS matched_region_codes
            FROM stations s
            LEFT JOIN tmp_federal_district_boundaries b
              ON ST_Intersects(s.geom, b.geom)
            WHERE 1=1
            {region_filter_stations_sql}
            GROUP BY s.id, s.region_code
        )
        SELECT
            CASE
                WHEN matched_regions_count = 0 THEN 'outside_all_federal_districts'
                WHEN inside_own_region AND matched_regions_count = 1 THEN 'inside_own_region_only'
                WHEN inside_own_region AND matched_regions_count > 1 THEN 'inside_own_and_other_regions'
                WHEN NOT inside_own_region AND matched_regions_count >= 1 THEN 'inside_other_region_only'
                ELSE 'unclassified'
            END AS category,
            COUNT(*) AS count
        FROM station_hits
        GROUP BY category
        ORDER BY count DESC, category;
        """,
        region_filter_stations_params,
    )
    done()

    done = log_step("collect station samples by class")
    station_sample_params = dict(region_filter_stations_params)
    station_sample_params["sample_limit"] = sample_limit

    station_samples = fetch_rows(
        connection,
        f"""
        WITH station_hits AS (
            SELECT
                s.id,
                s.region_code,
                s.name,
                s.station_type,
                ROUND(ST_X(s.geom)::numeric, 6) AS lon,
                ROUND(ST_Y(s.geom)::numeric, 6) AS lat,
                COUNT(b.region_code) AS matched_regions_count,
                BOOL_OR(b.region_code = s.region_code) AS inside_own_region,
                COALESCE(
                    STRING_AGG(DISTINCT b.region_code, ', ' ORDER BY b.region_code),
                    ''
                ) AS matched_region_codes
            FROM stations s
            LEFT JOIN tmp_federal_district_boundaries b
              ON ST_Intersects(s.geom, b.geom)
            WHERE 1=1
            {region_filter_stations_sql}
            GROUP BY
                s.id,
                s.region_code,
                s.name,
                s.station_type,
                s.geom
        ),
        classified AS (
            SELECT
                *,
                CASE
                    WHEN matched_regions_count = 0 THEN 'outside_all_federal_districts'
                    WHEN inside_own_region AND matched_regions_count = 1 THEN 'inside_own_region_only'
                    WHEN inside_own_region AND matched_regions_count > 1 THEN 'inside_own_and_other_regions'
                    WHEN NOT inside_own_region AND matched_regions_count >= 1 THEN 'inside_other_region_only'
                    ELSE 'unclassified'
                END AS category
            FROM station_hits
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY category
                    ORDER BY region_code, id
                ) AS rn
            FROM classified
        )
        SELECT
            category,
            id,
            region_code,
            name,
            station_type,
            lon,
            lat,
            matched_regions_count,
            matched_region_codes
        FROM ranked
        WHERE rn <= :sample_limit
        ORDER BY category, region_code, id;
        """,
        station_sample_params,
    )
    done()

    station_samples_by_category: dict[str, list] = {}
    for row in station_samples:
        category = row.pop("category")
        station_samples_by_category.setdefault(category, []).append(row)

    station_counts_map = {row["category"]: row["count"] for row in station_counts}

    stations_section = {
        "categories": station_counts,
        "samples": station_samples_by_category,
    }

    summary["stations_outside_all_federal_districts_count"] = station_counts_map.get(
        "outside_all_federal_districts", 0
    )
    summary["stations_inside_other_region_only_count"] = station_counts_map.get(
        "inside_other_region_only", 0
    )
    summary["stations_inside_own_and_other_regions_count"] = station_counts_map.get(
        "inside_own_and_other_regions", 0
    )

    done = log_step("classify rail lines")
    line_counts = fetch_rows(
        connection,
        f"""
        WITH line_hits AS (
            SELECT
                l.id,
                l.region_code,
                COUNT(DISTINCT b.region_code) AS matched_regions_count,
                BOOL_OR(b.region_code = l.region_code) AS intersects_own_region,
                STRING_AGG(DISTINCT b.region_code, ', ' ORDER BY b.region_code) AS matched_region_codes
            FROM rail_lines l
            LEFT JOIN tmp_federal_district_boundaries b
              ON ST_Intersects(l.geom, b.geom)
            WHERE 1=1
            {region_filter_lines_sql}
            GROUP BY l.id, l.region_code
        )
        SELECT
            CASE
                WHEN matched_regions_count = 0 THEN 'outside_all_federal_districts'
                WHEN intersects_own_region AND matched_regions_count = 1 THEN 'inside_own_region_only'
                WHEN intersects_own_region AND matched_regions_count > 1 THEN 'intersects_own_and_other_regions'
                WHEN NOT intersects_own_region AND matched_regions_count >= 1 THEN 'intersects_other_region_only'
                ELSE 'unclassified'
            END AS category,
            COUNT(*) AS count
        FROM line_hits
        GROUP BY category
        ORDER BY count DESC, category;
        """,
        region_filter_lines_params,
    )
    done()

    done = log_step("collect rail line samples by class")
    line_sample_params = dict(region_filter_lines_params)
    line_sample_params["sample_limit"] = sample_limit

    line_samples = fetch_rows(
        connection,
        f"""
        WITH line_hits AS (
            SELECT
                l.id,
                l.region_code,
                l.name,
                l.line_type,
                l.is_service_line,
                COUNT(DISTINCT b.region_code) AS matched_regions_count,
                COALESCE(
                    BOOL_OR(b.region_code = l.region_code),
                    FALSE
                ) AS intersects_own_region,
                COALESCE(
                    STRING_AGG(DISTINCT b.region_code, ', ' ORDER BY b.region_code),
                    ''
                ) AS matched_region_codes
            FROM rail_lines l
            LEFT JOIN tmp_federal_district_boundaries b
              ON ST_Intersects(l.geom, b.geom)
            WHERE 1=1
            {region_filter_lines_sql}
            GROUP BY
                l.id,
                l.region_code,
                l.name,
                l.line_type,
                l.is_service_line
        ),
        classified AS (
            SELECT
                *,
                CASE
                    WHEN matched_regions_count = 0 THEN 'outside_all_federal_districts'
                    WHEN intersects_own_region AND matched_regions_count = 1 THEN 'inside_own_region_only'
                    WHEN intersects_own_region AND matched_regions_count > 1 THEN 'intersects_own_and_other_regions'
                    WHEN NOT intersects_own_region AND matched_regions_count >= 1 THEN 'intersects_other_region_only'
                    ELSE 'unclassified'
                END AS category
            FROM line_hits
        ),
        ranked AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY category
                    ORDER BY region_code, id
                ) AS rn
            FROM classified
        )
        SELECT
            category,
            id,
            region_code,
            name,
            line_type,
            is_service_line,
            matched_regions_count,
            matched_region_codes
        FROM ranked
        WHERE rn <= :sample_limit
        ORDER BY category, region_code, id;
        """,
        line_sample_params,
    )
    done()

    line_samples_by_category: dict[str, list] = {}
    for row in line_samples:
        category = row.pop("category")
        line_samples_by_category.setdefault(category, []).append(row)

    line_counts_map = {row["category"]: row["count"] for row in line_counts}

    rail_lines_section = {
        "categories": line_counts,
        "samples": line_samples_by_category,
    }

    summary["lines_outside_all_federal_districts_count"] = line_counts_map.get(
        "outside_all_federal_districts", 0
    )
    summary["lines_intersects_other_region_only_count"] = line_counts_map.get(
        "intersects_other_region_only", 0
    )
    summary["lines_intersects_own_and_other_regions_count"] = line_counts_map.get(
        "intersects_own_and_other_regions", 0
    )

    if deep_low_share:
        print(
            "[INFO] Deep low-share check enabled. This stage can be heavy on large datasets.",
            flush=True,
        )

        done = log_step("count rail lines with low own-region share")
        low_share_params = dict(region_filter_lines_params)
        low_share_params["threshold"] = LOW_SHARE_THRESHOLD

        lines_low_own_share_count = fetch_scalar(
            connection,
            f"""
            WITH candidates AS (
                SELECT
                    l.id,
                    l.region_code,
                    l.geom,
                    b.geom AS own_region_geom
                FROM rail_lines l
                JOIN tmp_federal_district_boundaries b
                  ON b.region_code = l.region_code
                WHERE ST_Intersects(l.geom, b.geom)
                {region_filter_lines_sql}
                  AND EXISTS (
                      SELECT 1
                      FROM tmp_federal_district_boundaries b2
                      WHERE b2.region_code <> l.region_code
                        AND ST_Intersects(l.geom, b2.geom)
                  )
            ),
            measured AS (
                SELECT
                    c.id,
                    c.region_code,
                    ST_Length(ST_Transform(c.geom, 3857)) AS total_length_m,
                    ST_Length(
                        ST_Transform(
                            ST_CollectionExtract(ST_Intersection(c.geom, c.own_region_geom), 2),
                            3857
                        )
                    ) AS own_length_m
                FROM candidates c
            )
            SELECT COUNT(*)
            FROM measured
            WHERE total_length_m > 0
              AND (own_length_m / NULLIF(total_length_m, 0)) < :threshold;
            """,
            low_share_params,
        )
        done()

        done = log_step("collect rail lines with low own-region share samples")
        low_share_sample_params = dict(low_share_params)
        low_share_sample_params["sample_limit"] = sample_limit

        lines_low_own_share_samples = fetch_rows(
            connection,
            f"""
            WITH candidates AS (
                SELECT
                    l.id,
                    l.region_code,
                    l.name,
                    l.line_type,
                    l.is_service_line,
                    l.geom,
                    b.geom AS own_region_geom
                FROM rail_lines l
                JOIN tmp_federal_district_boundaries b
                  ON b.region_code = l.region_code
                WHERE ST_Intersects(l.geom, b.geom)
                {region_filter_lines_sql}
                  AND EXISTS (
                      SELECT 1
                      FROM tmp_federal_district_boundaries b2
                      WHERE b2.region_code <> l.region_code
                        AND ST_Intersects(l.geom, b2.geom)
                  )
            ),
            measured AS (
                SELECT
                    c.id,
                    c.region_code,
                    c.name,
                    c.line_type,
                    c.is_service_line,
                    ST_Length(ST_Transform(c.geom, 3857)) AS total_length_m,
                    ST_Length(
                        ST_Transform(
                            ST_CollectionExtract(ST_Intersection(c.geom, c.own_region_geom), 2),
                            3857
                        )
                    ) AS own_length_m
                FROM candidates c
            )
            SELECT
                id,
                region_code,
                name,
                line_type,
                is_service_line,
                ROUND((total_length_m / 1000.0)::numeric, 3) AS total_length_km,
                ROUND((own_length_m / 1000.0)::numeric, 3) AS own_region_length_km,
                ROUND(((own_length_m / NULLIF(total_length_m, 0)) * 100.0)::numeric, 2) AS own_region_share_pct
            FROM measured
            WHERE total_length_m > 0
              AND (own_length_m / NULLIF(total_length_m, 0)) < :threshold
            ORDER BY own_region_share_pct ASC, region_code, id
            LIMIT :sample_limit;
            """,
            low_share_sample_params,
        )
        done()

        rail_lines_section["low_own_region_share"] = {
            "threshold_pct": LOW_SHARE_THRESHOLD * 100,
            "count": lines_low_own_share_count,
            "samples": lines_low_own_share_samples,
        }
        summary["lines_low_own_region_share_count"] = lines_low_own_share_count
    else:
        rail_lines_section["low_own_region_share"] = {
            "threshold_pct": LOW_SHARE_THRESHOLD * 100,
            "count": None,
            "samples": [],
            "skipped": True,
            "reason": "deep_low_share_disabled",
        }
        summary["lines_low_own_region_share_count"] = None

    return {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "source_federal_districts_geojson": str(FEDERAL_DISTRICTS_GEOJSON_PATH),
        "selected_region_code": selected_region_code,
        "thresholds": {
            "line_low_own_region_share_threshold_pct": LOW_SHARE_THRESHOLD * 100,
            "sample_limit": sample_limit,
        },
        "summary": summary,
        "stations": stations_section,
        "rail_lines": rail_lines_section,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="Classify spatial consistency between region_code and federal district boundaries"
    )
    parser.add_argument(
        "--region-code",
        dest="region_code",
        default=None,
        help="Audit only one federal district, for example: volga_fd",
    )
    parser.add_argument(
        "--deep-low-share",
        dest="deep_low_share",
        action="store_true",
        help="Enable heavy check for line share inside own region",
    )
    parser.add_argument(
        "--sample-limit",
        dest="sample_limit",
        type=int,
        default=DEFAULT_SAMPLE_LIMIT,
        help=f"Number of sample records per category (default: {DEFAULT_SAMPLE_LIMIT})",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("[INFO] Starting geometry classification audit", flush=True)
    print(f"[INFO] Federal districts GeoJSON: {FEDERAL_DISTRICTS_GEOJSON_PATH}", flush=True)
    print(f"[INFO] Output path: {OUTPUT_PATH}", flush=True)
    print(f"[INFO] Region filter: {args.region_code or 'ALL'}", flush=True)
    print(f"[INFO] Deep low-share mode: {args.deep_low_share}", flush=True)
    print(f"[INFO] Sample limit: {args.sample_limit}", flush=True)

    done = log_step("load federal district features")
    district_features = load_federal_district_features(args.region_code)
    done()

    if not district_features:
        raise RuntimeError("No federal district geometries loaded from GeoJSON")

    print(f"[INFO] Loaded district features: {len(district_features)}", flush=True)

    with engine.begin() as connection:
        prepare_temp_boundaries(connection, district_features)
        report = build_report(
            connection=connection,
            selected_region_code=args.region_code,
            sample_limit=args.sample_limit,
            deep_low_share=args.deep_low_share,
        )

    report_json = normalize_for_json(report)

    done = log_step("write classification report to json")
    OUTPUT_PATH.write_text(
        json.dumps(report_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    done()

    print("[INFO] Classification audit finished", flush=True)
    print(f"[INFO] Stations total: {report_json['summary']['stations_total']}", flush=True)
    print(f"[INFO] Rail lines total: {report_json['summary']['rail_lines_total']}", flush=True)
    print(
        f"[INFO] Stations outside all federal districts: "
        f"{report_json['summary']['stations_outside_all_federal_districts_count']}",
        flush=True,
    )
    print(
        f"[INFO] Stations inside other region only: "
        f"{report_json['summary']['stations_inside_other_region_only_count']}",
        flush=True,
    )
    print(
        f"[INFO] Lines outside all federal districts: "
        f"{report_json['summary']['lines_outside_all_federal_districts_count']}",
        flush=True,
    )
    print(
        f"[INFO] Lines intersects other region only: "
        f"{report_json['summary']['lines_intersects_other_region_only_count']}",
        flush=True,
    )
    print(
        f"[INFO] Lines intersects own and other regions: "
        f"{report_json['summary']['lines_intersects_own_and_other_regions_count']}",
        flush=True,
    )
    print(
        f"[INFO] Lines low own region share: "
        f"{report_json['summary']['lines_low_own_region_share_count']}",
        flush=True,
    )


if __name__ == "__main__":
    main()