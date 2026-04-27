import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.db import engine  # noqa: E402
from app.route_graph_matcher import (  # noqa: E402
    build_candidates_for_stop,
    infer_route_region_codes,
    load_global_station_catalog,
    load_route,
)


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def short(value: Any, max_len: int = 500) -> str:
    text_value = json.dumps(value, ensure_ascii=False, default=json_default)
    if len(text_value) <= max_len:
        return text_value
    return text_value[:max_len] + " ..."


def ensure_output_dir() -> Path:
    output_dir = BASE_DIR / "debug_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_region_filter_clause(
    region_codes: list[str],
    *,
    column_name: str,
    params: dict[str, Any],
    prefix: str,
) -> str:
    if not region_codes:
        return ""

    placeholders: list[str] = []
    for index, code in enumerate(region_codes):
        param_name = f"{prefix}_{index}"
        params[param_name] = code
        placeholders.append(f":{param_name}")

    return f" AND {column_name} IN ({', '.join(placeholders)}) "


def collect_candidates(route_id: int) -> tuple[dict[str, Any], list[dict[str, Any]], list[list[Any]], dict[str, Any]]:
    payload = load_route(route_id)
    route = payload["route"]
    stops = payload["stops"]

    catalog_payload = load_global_station_catalog()
    candidates_per_stop = []

    for stop in stops:
        candidates = build_candidates_for_stop(stop, catalog_payload)
        candidates_per_stop.append(candidates)

    diagnostics: dict[str, Any] = {}
    inferred_region_codes = infer_route_region_codes(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
        diagnostics=diagnostics,
    )

    return route, stops, candidates_per_stop, {
        "catalog_payload": catalog_payload,
        "inferred_region_codes": inferred_region_codes,
        "region_diagnostics": diagnostics.get("inferred_route_regions") or {},
    }


def probe_graph_counts(region_codes: list[str], snap_meters: int) -> dict[str, Any]:
    params: dict[str, Any] = {
        "snap_meters": snap_meters,
        "snap_degrees": snap_meters / 111_320.0,
    }

    line_region_clause = build_region_filter_clause(
        region_codes,
        column_name="l.region_code",
        params=params,
        prefix="line_region",
    )
    station_region_clause = build_region_filter_clause(
        region_codes,
        column_name="s.region_code",
        params=params,
        prefix="station_region",
    )

    query = text(f"""
        WITH visible_line_parts AS (
            SELECT
                l.id AS line_id,
                dump.path[1] AS part_index,
                dump.geom AS geom
            FROM rail_lines l
            CROSS JOIN LATERAL ST_Dump(ST_LineMerge(l.geom)) AS dump
            WHERE
                COALESCE(l.is_service_line, FALSE) = FALSE
                AND (
                    COALESCE(l.is_visible_default, FALSE) = TRUE
                    OR COALESCE(l.is_main_passenger_line, FALSE) = TRUE
                )
                {line_region_clause}
                AND dump.geom IS NOT NULL
                AND GeometryType(dump.geom) = 'LINESTRING'
        ),
        line_station_candidates AS (
            SELECT
                lp.line_id,
                lp.part_index,
                s.id AS station_id,
                ST_LineLocatePoint(lp.geom, ST_ClosestPoint(lp.geom, s.geom)) AS fraction
            FROM visible_line_parts lp
            JOIN stations s
              ON s.is_visible_default = TRUE
             AND s.geom IS NOT NULL
             {station_region_clause}
             AND s.geom && ST_Expand(lp.geom, :snap_degrees)
             AND ST_DWithin(s.geom::geography, lp.geom::geography, :snap_meters)
        ),
        line_station_unique AS (
            SELECT DISTINCT ON (line_id, part_index, station_id)
                line_id,
                part_index,
                station_id,
                fraction
            FROM line_station_candidates
            ORDER BY line_id, part_index, station_id, fraction
        ),
        ordered AS (
            SELECT
                line_id,
                part_index,
                station_id,
                fraction,
                LEAD(station_id) OVER (
                    PARTITION BY line_id, part_index
                    ORDER BY fraction, station_id
                ) AS next_station_id,
                LEAD(fraction) OVER (
                    PARTITION BY line_id, part_index
                    ORDER BY fraction, station_id
                ) AS next_fraction
            FROM line_station_unique
        )
        SELECT
            (SELECT COUNT(*) FROM visible_line_parts) AS visible_line_parts_count,
            (SELECT COUNT(*) FROM line_station_candidates) AS line_station_candidates_count,
            (SELECT COUNT(*) FROM line_station_unique) AS line_station_unique_count,
            (
                SELECT COUNT(*)
                FROM ordered
                WHERE
                    next_station_id IS NOT NULL
                    AND next_station_id <> station_id
                    AND next_fraction IS NOT NULL
                    AND next_fraction <> fraction
            ) AS adjacent_pairs_count;
    """)

    with engine.connect() as connection:
        row = connection.execute(query, params).first()

    if row is None:
        return {
            "snap_meters": snap_meters,
            "visible_line_parts_count": 0,
            "line_station_candidates_count": 0,
            "line_station_unique_count": 0,
            "adjacent_pairs_count": 0,
        }

    item = dict(row._mapping)
    item["snap_meters"] = snap_meters
    return item


def probe_stop_line_distances(
    stops: list[dict[str, Any]],
    candidates_per_stop: list[list[Any]],
    region_codes: list[str],
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    line_region_clause = build_region_filter_clause(
        region_codes,
        column_name="l.region_code",
        params=params,
        prefix="line_region",
    )

    results: list[dict[str, Any]] = []

    with engine.connect() as connection:
        for stop, candidates in zip(stops, candidates_per_stop):
            chosen_station_id = None
            chosen_reason = None

            stored_station_id = stop.get("stored_station_id")
            stored_station_visible = stop.get("stored_station_visible")

            if stored_station_id and stored_station_visible:
                chosen_station_id = int(stored_station_id)
                chosen_reason = "stored_visible_station"
            elif candidates:
                chosen_station_id = int(candidates[0].station_id)
                chosen_reason = "top_candidate"

            if chosen_station_id is None:
                results.append(
                    {
                        "stop_sequence": stop.get("stop_sequence"),
                        "station_name_raw": stop.get("station_name_raw"),
                        "probe_station_id": None,
                        "probe_station_name": None,
                        "probe_reason": "no_probe_station",
                        "nearest_line_distance_m": None,
                        "nearest_line_id": None,
                        "nearest_line_region_code": None,
                    }
                )
                continue

            query = text(f"""
                SELECT
                    s.id AS station_id,
                    s.name AS station_name,
                    l.id AS line_id,
                    l.region_code AS line_region_code,
                    ST_Distance(s.geom::geography, l.geom::geography) AS distance_m
                FROM stations s
                JOIN rail_lines l
                  ON COALESCE(l.is_service_line, FALSE) = FALSE
                 AND (
                    COALESCE(l.is_visible_default, FALSE) = TRUE
                    OR COALESCE(l.is_main_passenger_line, FALSE) = TRUE
                 )
                 {line_region_clause}
                WHERE s.id = :station_id
                ORDER BY s.geom <-> l.geom
                LIMIT 1;
            """)

            row = connection.execute(
                query,
                {
                    **params,
                    "station_id": chosen_station_id,
                },
            ).first()

            if row is None:
                results.append(
                    {
                        "stop_sequence": stop.get("stop_sequence"),
                        "station_name_raw": stop.get("station_name_raw"),
                        "probe_station_id": chosen_station_id,
                        "probe_station_name": None,
                        "probe_reason": chosen_reason,
                        "nearest_line_distance_m": None,
                        "nearest_line_id": None,
                        "nearest_line_region_code": None,
                    }
                )
                continue

            item = dict(row._mapping)
            results.append(
                {
                    "stop_sequence": stop.get("stop_sequence"),
                    "station_name_raw": stop.get("station_name_raw"),
                    "probe_station_id": item.get("station_id"),
                    "probe_station_name": item.get("station_name"),
                    "probe_reason": chosen_reason,
                    "nearest_line_distance_m": round(float(item.get("distance_m") or 0.0), 2),
                    "nearest_line_id": item.get("line_id"),
                    "nearest_line_region_code": item.get("line_region_code"),
                }
            )

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Проверка, почему graph edge query даёт 0 рёбер."
    )
    parser.add_argument("route_id", type=int, help="ID маршрута")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Путь к JSON файлу вывода",
    )
    args = parser.parse_args()

    print_section("START")
    print(f"route_id = {args.route_id}")

    route, stops, candidates_per_stop, payload = collect_candidates(args.route_id)
    inferred_region_codes = payload["inferred_region_codes"]
    region_diagnostics = payload["region_diagnostics"]

    print_section("ROUTE")
    print(f"route_id: {route.get('id')}")
    print(f"train_number: {route.get('train_number')}")
    print(f"route_name: {route.get('route_name')}")
    print(f"stops_count: {len(stops)}")

    print_section("INFERRED REGIONS")
    print(json.dumps(region_diagnostics, ensure_ascii=False, indent=2, default=json_default))

    graph_probe_results = []
    for snap_meters in [180, 300, 500, 1000]:
        graph_probe_results.append(probe_graph_counts(inferred_region_codes, snap_meters))

    print_section("GRAPH COUNTS BY SNAP RADIUS")
    for item in graph_probe_results:
        print(json.dumps(item, ensure_ascii=False, indent=2, default=json_default))

    stop_line_distances = probe_stop_line_distances(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
        region_codes=inferred_region_codes,
    )

    print_section("STOP → NEAREST LINE DISTANCES")
    for item in stop_line_distances:
        print(short(item, max_len=1000))

    output_payload = {
        "route": route,
        "inferred_region_codes": inferred_region_codes,
        "region_diagnostics": region_diagnostics,
        "graph_probe_results": graph_probe_results,
        "stop_line_distances": stop_line_distances,
        "stops_preview": [
            {
                "stop_sequence": stop.get("stop_sequence"),
                "station_name_raw": stop.get("station_name_raw"),
                "stored_station_id": stop.get("stored_station_id"),
                "stored_station_region_code": stop.get("stored_station_region_code"),
                "top_candidates": [
                    {
                        "station_id": candidate.station_id,
                        "station_name": candidate.name,
                        "region_code": candidate.region_code,
                        "effective_score": round(candidate.effective_score, 4),
                        "match_method": candidate.match_method,
                        "anchor": candidate.anchor,
                        "code_match": candidate.code_match,
                    }
                    for candidate in candidates[:3]
                ],
            }
            for stop, candidates in zip(stops, candidates_per_stop)
        ],
    }

    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = (Path.cwd() / output_path).resolve()
    else:
        output_dir = ensure_output_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"route_network_probe_{args.route_id}_{timestamp}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )

    print_section("FILE SAVED")
    print(str(output_path))


if __name__ == "__main__":
    main()