from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import text

from app.db import engine


DEFAULT_REPORT_DIR = BASE_DIR / "data" / "audit"
DEFAULT_RADII = [120.0, 180.0, 250.0]


@dataclass
class StationMembershipRow:
    id: int
    name: str | None
    region_code: str | None
    uic_ref: str | None
    is_visible_default: bool | None
    nearby_lines_count: int
    service_lines_count: int
    non_service_lines_count: int
    main_passenger_lines_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сравнение привязки станций к линиям на нескольких радиусах."
    )
    parser.add_argument(
        "--radii",
        nargs="+",
        type=float,
        default=DEFAULT_RADII,
        help="Список радиусов в метрах, например: --radii 120 180 250",
    )
    parser.add_argument(
        "--report-path",
        type=str,
        default="",
        help="Путь к JSON-отчёту. Если не указан, будет создан автоматически.",
    )
    return parser.parse_args()


def ensure_report_path(report_path_raw: str) -> Path:
    if report_path_raw:
        path = Path(report_path_raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_REPORT_DIR / f"station_line_membership_compare_{timestamp}.json"


def meters_to_degrees(meters: float) -> float:
    return meters / 111_320.0


def print_timing(label: str, started_at: float):
    duration = time.perf_counter() - started_at
    print(f"[TIME] {label}: {duration:.2f} s")


def load_basic_counts() -> dict:
    query = text("""
        SELECT
            (SELECT COUNT(*) FROM stations) AS stations_total,
            (SELECT COUNT(*) FROM rail_lines) AS rail_lines_total,
            (SELECT COUNT(*) FROM stations WHERE geom IS NOT NULL) AS stations_with_geom,
            (SELECT COUNT(*) FROM rail_lines WHERE geom IS NOT NULL) AS rail_lines_with_geom;
    """)

    with engine.connect() as connection:
        row = connection.execute(query).first()

    if row is None:
        return {
            "stations_total": 0,
            "rail_lines_total": 0,
            "stations_with_geom": 0,
            "rail_lines_with_geom": 0,
        }

    return dict(row._mapping)


def load_station_membership(snap_meters: float) -> dict[int, StationMembershipRow]:
    snap_degrees = meters_to_degrees(snap_meters)

    query = text("""
        SELECT
            s.id,
            s.name,
            s.region_code,
            NULLIF(BTRIM(s.uic_ref), '') AS uic_ref,
            s.is_visible_default,
            COALESCE(line_stats.nearby_lines_count, 0) AS nearby_lines_count,
            COALESCE(line_stats.service_lines_count, 0) AS service_lines_count,
            COALESCE(line_stats.non_service_lines_count, 0) AS non_service_lines_count,
            COALESCE(line_stats.main_passenger_lines_count, 0) AS main_passenger_lines_count
        FROM stations s
        LEFT JOIN LATERAL (
            SELECT
                COUNT(DISTINCT rl.id) AS nearby_lines_count,
                COUNT(DISTINCT CASE
                    WHEN COALESCE(rl.is_service_line, FALSE) = TRUE THEN rl.id
                    ELSE NULL
                END) AS service_lines_count,
                COUNT(DISTINCT CASE
                    WHEN COALESCE(rl.is_service_line, FALSE) = FALSE THEN rl.id
                    ELSE NULL
                END) AS non_service_lines_count,
                COUNT(DISTINCT CASE
                    WHEN COALESCE(rl.is_main_passenger_line, FALSE) = TRUE THEN rl.id
                    ELSE NULL
                END) AS main_passenger_lines_count
            FROM rail_lines rl
            WHERE s.geom IS NOT NULL
              AND rl.geom IS NOT NULL
              AND rl.geom && ST_Expand(s.geom, :snap_degrees)
              AND ST_DWithin(s.geom::geography, rl.geom::geography, :snap_meters)
        ) AS line_stats ON TRUE
        ORDER BY s.id;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "snap_meters": snap_meters,
                "snap_degrees": snap_degrees,
            },
        ).fetchall()

    result: dict[int, StationMembershipRow] = {}
    for row in rows:
        m = row._mapping
        result[int(m["id"])] = StationMembershipRow(
            id=int(m["id"]),
            name=m["name"],
            region_code=m["region_code"],
            uic_ref=m["uic_ref"],
            is_visible_default=m["is_visible_default"],
            nearby_lines_count=int(m["nearby_lines_count"] or 0),
            service_lines_count=int(m["service_lines_count"] or 0),
            non_service_lines_count=int(m["non_service_lines_count"] or 0),
            main_passenger_lines_count=int(m["main_passenger_lines_count"] or 0),
        )

    return result


def classify_station(row: StationMembershipRow) -> str:
    if row.nearby_lines_count == 0:
        return "without_nearby_lines"

    if row.non_service_lines_count == 0 and row.service_lines_count > 0:
        return "service_only"

    if row.main_passenger_lines_count > 0:
        return "main_passenger_connected"

    if row.non_service_lines_count > 0:
        return "non_service_connected"

    return "other"


def build_radius_summary(rows_by_station_id: dict[int, StationMembershipRow]) -> dict:
    summary = defaultdict(int)

    for row in rows_by_station_id.values():
        summary[classify_station(row)] += 1

    summary["stations_total"] = len(rows_by_station_id)
    return dict(sorted(summary.items()))


def build_transition_key_sequence(
    station_id: int,
    rows_by_radius: dict[float, dict[int, StationMembershipRow]],
    radii: list[float],
) -> str:
    parts = []
    for radius in radii:
        row = rows_by_radius[radius][station_id]
        parts.append(f"{int(radius)}:{classify_station(row)}")
    return " | ".join(parts)


def build_transition_groups(
    rows_by_radius: dict[float, dict[int, StationMembershipRow]],
    radii: list[float],
) -> list[dict]:
    first_radius = radii[0]
    station_ids = sorted(rows_by_radius[first_radius].keys())

    grouped: dict[str, list[dict]] = defaultdict(list)

    for station_id in station_ids:
        transition_key = build_transition_key_sequence(
            station_id=station_id,
            rows_by_radius=rows_by_radius,
            radii=radii,
        )

        base_row = rows_by_radius[first_radius][station_id]

        payload = {
            "station_id": base_row.id,
            "name": base_row.name,
            "region_code": base_row.region_code,
            "uic_ref": base_row.uic_ref,
            "per_radius": {
                str(int(radius)): {
                    "classification": classify_station(rows_by_radius[radius][station_id]),
                    "nearby_lines_count": rows_by_radius[radius][station_id].nearby_lines_count,
                    "service_lines_count": rows_by_radius[radius][station_id].service_lines_count,
                    "non_service_lines_count": rows_by_radius[radius][station_id].non_service_lines_count,
                    "main_passenger_lines_count": rows_by_radius[radius][station_id].main_passenger_lines_count,
                }
                for radius in radii
            },
        }

        grouped[transition_key].append(payload)

    result = []
    for transition_key, items in sorted(grouped.items(), key=lambda x: (-len(x[1]), x[0])):
        result.append(
            {
                "transition": transition_key,
                "count": len(items),
                "examples": items[:50],
            }
        )

    return result


def build_persistent_problem_groups(
    rows_by_radius: dict[float, dict[int, StationMembershipRow]],
    radii: list[float],
) -> dict:
    first_radius = radii[0]
    station_ids = sorted(rows_by_radius[first_radius].keys())

    persistent_without_lines = []
    persistent_service_only = []
    recovered_between_radii = []

    for station_id in station_ids:
        rows = [rows_by_radius[radius][station_id] for radius in radii]
        classes = [classify_station(row) for row in rows]
        base_row = rows[0]

        payload = {
            "station_id": base_row.id,
            "name": base_row.name,
            "region_code": base_row.region_code,
            "uic_ref": base_row.uic_ref,
            "classifications": {
                str(int(radius)): classify_station(rows_by_radius[radius][station_id])
                for radius in radii
            },
            "counts": {
                str(int(radius)): {
                    "nearby_lines_count": rows_by_radius[radius][station_id].nearby_lines_count,
                    "service_lines_count": rows_by_radius[radius][station_id].service_lines_count,
                    "non_service_lines_count": rows_by_radius[radius][station_id].non_service_lines_count,
                    "main_passenger_lines_count": rows_by_radius[radius][station_id].main_passenger_lines_count,
                }
                for radius in radii
            },
        }

        if all(cls == "without_nearby_lines" for cls in classes):
            persistent_without_lines.append(payload)

        if all(cls == "service_only" for cls in classes):
            persistent_service_only.append(payload)

        if classes[0] == "without_nearby_lines" and classes[-1] != "without_nearby_lines":
            recovered_between_radii.append(payload)

    return {
        "persistent_without_nearby_lines": persistent_without_lines[:300],
        "persistent_without_nearby_lines_count": len(persistent_without_lines),
        "persistent_service_only": persistent_service_only[:300],
        "persistent_service_only_count": len(persistent_service_only),
        "recovered_between_smallest_and_largest_radius": recovered_between_radii[:300],
        "recovered_between_smallest_and_largest_radius_count": len(recovered_between_radii),
    }


def main():
    args = parse_args()
    radii = sorted(set(float(value) for value in args.radii))
    report_path = ensure_report_path(args.report_path)

    print("[START] Проверяю базовые объёмы данных...")
    t0 = time.perf_counter()
    basic_counts = load_basic_counts()
    print_timing("load_basic_counts", t0)
    print(json.dumps(basic_counts, ensure_ascii=False, indent=2))

    rows_by_radius: dict[float, dict[int, StationMembershipRow]] = {}

    for radius in radii:
        print(f"[START] Загружаю membership для радиуса {int(radius)} м...")
        tx = time.perf_counter()
        rows_by_radius[radius] = load_station_membership(radius)
        print_timing(f"load_station_membership_{int(radius)}m", tx)
        print(f"[INFO] Станций обработано: {len(rows_by_radius[radius])}")

    radius_summaries = {
        str(int(radius)): build_radius_summary(rows_by_radius[radius])
        for radius in radii
    }

    print("[STEP] Строю transition groups...")
    t1 = time.perf_counter()
    transition_groups = build_transition_groups(rows_by_radius, radii)
    print_timing("build_transition_groups", t1)

    print("[STEP] Строю persistent problem groups...")
    t2 = time.perf_counter()
    persistent_problem_groups = build_persistent_problem_groups(rows_by_radius, radii)
    print_timing("build_persistent_problem_groups", t2)

    report = {
        "generated_at": datetime.now().isoformat(),
        "radii": radii,
        "basic_counts": basic_counts,
        "radius_summaries": radius_summaries,
        "top_transition_groups": transition_groups[:50],
        "persistent_problem_groups": persistent_problem_groups,
    }

    t3 = time.perf_counter()
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print_timing("write_report", t3)

    print("[SUMMARY]")
    print(json.dumps(radius_summaries, ensure_ascii=False, indent=2))
    print(f"[DONE] Отчёт сохранён: {report_path}")


if __name__ == "__main__":
    main()