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
OUTPUT_PATH = BACK_DIR / "data" / "audit" / "region_geometry_audit.json"

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

    done = log_step("count stations total")
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
    done()

    done = log_step("count rail lines total")
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

    done = log_step("count stations missing boundary mapping")
    summary["stations_missing_boundary_mapping_count"] = fetch_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM stations s
        LEFT JOIN tmp_federal_district_boundaries b
          ON b.region_code = s.region_code
        WHERE b.region_code IS NULL
        {region_filter_stations_sql};
        """,
        region_filter_stations_params,
    )
    done()

    done = log_step("count stations outside own region")
    stations_outside_count = fetch_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM stations s
        JOIN tmp_federal_district_boundaries b
          ON b.region_code = s.region_code
        WHERE NOT ST_Intersects(s.geom, b.geom)
        {region_filter_stations_sql};
        """,
        region_filter_stations_params,
    )
    done()
    summary["stations_outside_own_region_count"] = stations_outside_count

    done = log_step("collect stations outside own region by region")
    stations_outside_by_region = fetch_rows(
        connection,
        f"""
        SELECT
            s.region_code,
            COUNT(*) AS count
        FROM stations s
        JOIN tmp_federal_district_boundaries b
          ON b.region_code = s.region_code
        WHERE NOT ST_Intersects(s.geom, b.geom)
        {region_filter_stations_sql}
        GROUP BY s.region_code
        ORDER BY count DESC, s.region_code;
        """,
        region_filter_stations_params,
    )
    done()

    done = log_step("collect stations outside own region samples")
    station_sample_params = dict(region_filter_stations_params)
    station_sample_params["sample_limit"] = sample_limit
    stations_outside_samples = fetch_rows(
        connection,
        f"""
        SELECT
            s.id,
            s.region_code,
            s.name,
            s.station_type,
            ROUND(ST_X(s.geom)::numeric, 6) AS lon,
            ROUND(ST_Y(s.geom)::numeric, 6) AS lat,
            COALESCE(actual.actual_region_codes, '') AS actual_region_codes
        FROM stations s
        JOIN tmp_federal_district_boundaries b
          ON b.region_code = s.region_code
        LEFT JOIN LATERAL (
            SELECT string_agg(b2.region_code, ', ' ORDER BY b2.region_code) AS actual_region_codes
            FROM tmp_federal_district_boundaries b2
            WHERE ST_Intersects(s.geom, b2.geom)
        ) actual ON TRUE
        WHERE NOT ST_Intersects(s.geom, b.geom)
        {region_filter_stations_sql}
        ORDER BY s.region_code, s.id
        LIMIT :sample_limit;
        """,
        station_sample_params,
    )
    done()

    stations_section["outside_own_region"] = {
        "count": stations_outside_count,
        "by_region": stations_outside_by_region,
        "samples": stations_outside_samples,
    }

    done = log_step("count rail lines missing boundary mapping")
    summary["lines_missing_boundary_mapping_count"] = fetch_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM rail_lines l
        LEFT JOIN tmp_federal_district_boundaries b
          ON b.region_code = l.region_code
        WHERE b.region_code IS NULL
        {region_filter_lines_sql};
        """,
        region_filter_lines_params,
    )
    done()

    done = log_step("count rail lines outside own region")
    lines_outside_count = fetch_scalar(
        connection,
        f"""
        SELECT COUNT(*)
        FROM rail_lines l
        JOIN tmp_federal_district_boundaries b
          ON b.region_code = l.region_code
        WHERE NOT ST_Intersects(l.geom, b.geom)
        {region_filter_lines_sql};
        """,
        region_filter_lines_params,
    )
    done()
    summary["lines_outside_own_region_count"] = lines_outside_count

    done = log_step("collect rail lines outside own region by region")
    lines_outside_by_region = fetch_rows(
        connection,
        f"""
        SELECT
            l.region_code,
            COUNT(*) AS count
        FROM rail_lines l
        JOIN tmp_federal_district_boundaries b
          ON b.region_code = l.region_code
        WHERE NOT ST_Intersects(l.geom, b.geom)
        {region_filter_lines_sql}
        GROUP BY l.region_code
        ORDER BY count DESC, l.region_code;
        """,
        region_filter_lines_params,
    )
    done()

    done = log_step("collect rail lines outside own region samples")
    line_sample_params = dict(region_filter_lines_params)
    line_sample_params["sample_limit"] = sample_limit
    lines_outside_samples = fetch_rows(
        connection,
        f"""
        SELECT
            l.id,
            l.region_code,
            l.name,
            l.line_type,
            l.is_service_line,
            COALESCE(actual.actual_region_codes, '') AS actual_region_codes
        FROM rail_lines l
        JOIN tmp_federal_district_boundaries b
          ON b.region_code = l.region_code
        LEFT JOIN LATERAL (
            SELECT string_agg(sub.region_code, ', ' ORDER BY sub.region_code) AS actual_region_codes
            FROM (
                SELECT DISTINCT b2.region_code
                FROM tmp_federal_district_boundaries b2
                WHERE ST_Intersects(l.geom, b2.geom)
            ) sub
        ) actual ON TRUE
        WHERE NOT ST_Intersects(l.geom, b.geom)
        {region_filter_lines_sql}
        ORDER BY l.region_code, l.id
        LIMIT :sample_limit;
        """,
        line_sample_params,
    )
    done()

    rail_lines_section["outside_own_region"] = {
        "count": lines_outside_count,
        "by_region": lines_outside_by_region,
        "samples": lines_outside_samples,
    }

    done = log_step("count rail lines intersecting multiple regions")
    lines_multi_region_count = fetch_scalar(
        connection,
        f"""
        WITH line_regions AS (
            SELECT
                l.id,
                COUNT(DISTINCT b.region_code) AS region_count
            FROM rail_lines l
            JOIN tmp_federal_district_boundaries b
              ON ST_Intersects(l.geom, b.geom)
            WHERE 1=1
            {region_filter_lines_sql}
            GROUP BY l.id
        )
        SELECT COUNT(*)
        FROM line_regions
        WHERE region_count > 1;
        """,
        region_filter_lines_params,
    )
    done()
    summary["lines_intersecting_multiple_regions_count"] = lines_multi_region_count

    done = log_step("collect rail lines intersecting multiple regions samples")
    multi_region_params = dict(region_filter_lines_params)
    multi_region_params["sample_limit"] = sample_limit
    lines_multi_region_samples = fetch_rows(
        connection,
        f"""
        WITH line_regions AS (
            SELECT
                l.id,
                l.region_code,
                l.name,
                l.line_type,
                l.is_service_line,
                COUNT(DISTINCT b.region_code) AS region_count,
                string_agg(sub.region_code, ', ' ORDER BY sub.region_code) AS intersected_region_codes
            FROM rail_lines l
            JOIN tmp_federal_district_boundaries b
              ON ST_Intersects(l.geom, b.geom)
            JOIN LATERAL (
                SELECT DISTINCT b2.region_code
                FROM tmp_federal_district_boundaries b2
                WHERE ST_Intersects(l.geom, b2.geom)
            ) sub ON TRUE
            WHERE 1=1
            {region_filter_lines_sql}
            GROUP BY
                l.id,
                l.region_code,
                l.name,
                l.line_type,
                l.is_service_line
        )
        SELECT
            id,
            region_code,
            name,
            line_type,
            is_service_line,
            region_count,
            intersected_region_codes
        FROM line_regions
        WHERE region_count > 1
        ORDER BY region_count DESC, region_code, id
        LIMIT :sample_limit;
        """,
        multi_region_params,
    )
    done()

    rail_lines_section["intersect_multiple_regions"] = {
        "count": lines_multi_region_count,
        "samples": lines_multi_region_samples,
    }

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

        summary["lines_low_own_region_share_count"] = lines_low_own_share_count
        rail_lines_section["low_own_region_share"] = {
            "threshold_pct": LOW_SHARE_THRESHOLD * 100,
            "count": lines_low_own_share_count,
            "samples": lines_low_own_share_samples,
        }
    else:
        summary["lines_low_own_region_share_count"] = None
        rail_lines_section["low_own_region_share"] = {
            "threshold_pct": LOW_SHARE_THRESHOLD * 100,
            "count": None,
            "samples": [],
            "skipped": True,
            "reason": "deep_low_share_disabled",
        }

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
        description="Audit spatial consistency between region_code and federal district boundaries"
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
        help=f"Number of sample records per section (default: {DEFAULT_SAMPLE_LIMIT})",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    print("[INFO] Starting geometry audit", flush=True)
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

    done = log_step("write audit report to json")
    OUTPUT_PATH.write_text(
        json.dumps(report_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    done()

    print("[INFO] Audit finished", flush=True)
    print(f"[INFO] Stations total: {report_json['summary']['stations_total']}", flush=True)
    print(f"[INFO] Rail lines total: {report_json['summary']['rail_lines_total']}", flush=True)
    print(
        f"[INFO] Stations outside own region: "
        f"{report_json['summary']['stations_outside_own_region_count']}",
        flush=True,
    )
    print(
        f"[INFO] Lines outside own region: "
        f"{report_json['summary']['lines_outside_own_region_count']}",
        flush=True,
    )
    print(
        f"[INFO] Lines intersecting multiple regions: "
        f"{report_json['summary']['lines_intersecting_multiple_regions_count']}",
        flush=True,
    )
    print(
        f"[INFO] Lines low own region share: "
        f"{report_json['summary']['lines_low_own_region_share_count']}",
        flush=True,
    )


if __name__ == "__main__":
    main()