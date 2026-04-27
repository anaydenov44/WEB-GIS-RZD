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
DEFAULT_SNAP_METERS = 180.0
DUPLICATE_SCORE_GAP_THRESHOLD = 25.0


@dataclass
class StationAuditRow:
    id: int
    name: str | None
    region_code: str | None
    uic_ref: str | None
    esr_user: str | None
    station_type: str | None
    is_main_rail_station: bool | None
    is_visible_default: bool | None
    exclude_reason: str | None
    nearby_lines_count: int
    service_lines_count: int
    non_service_lines_count: int
    main_passenger_lines_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Аудит качества станций и мягкая очистка видимости."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить изменения в БД. Без флага работает как dry-run.",
    )
    parser.add_argument(
        "--snap-meters",
        type=float,
        default=DEFAULT_SNAP_METERS,
        help=f"Радиус привязки станции к линии, по умолчанию {DEFAULT_SNAP_METERS} м.",
    )
    parser.add_argument(
        "--report-path",
        type=str,
        default="",
        help="Путь к JSON-отчёту. Если не указан, будет создан автоматически.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Печатать дополнительные тайминги и служебную информацию.",
    )
    return parser.parse_args()


def ensure_report_path(report_path_raw: str) -> Path:
    if report_path_raw:
        path = Path(report_path_raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_REPORT_DIR / f"station_quality_audit_{timestamp}.json"


def meters_to_degrees(meters: float) -> float:
    # Грубая оценка для bbox-prefilter в EPSG:4326.
    # Используется только как предварительное ограничение,
    # точная проверка всё равно делается через geography.
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


def load_station_rows(snap_meters: float) -> list[StationAuditRow]:
    snap_degrees = meters_to_degrees(snap_meters)

    query = text("""
        SELECT
            s.id,
            s.name,
            s.region_code,
            NULLIF(BTRIM(s.uic_ref), '') AS uic_ref,
            NULLIF(BTRIM(s.esr_user), '') AS esr_user,
            s.station_type,
            s.is_main_rail_station,
            s.is_visible_default,
            s.exclude_reason,
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

    result: list[StationAuditRow] = []
    for row in rows:
        m = row._mapping
        result.append(
            StationAuditRow(
                id=int(m["id"]),
                name=m["name"],
                region_code=m["region_code"],
                uic_ref=m["uic_ref"],
                esr_user=m["esr_user"],
                station_type=m["station_type"],
                is_main_rail_station=m["is_main_rail_station"],
                is_visible_default=m["is_visible_default"],
                exclude_reason=m["exclude_reason"],
                nearby_lines_count=int(m["nearby_lines_count"] or 0),
                service_lines_count=int(m["service_lines_count"] or 0),
                non_service_lines_count=int(m["non_service_lines_count"] or 0),
                main_passenger_lines_count=int(m["main_passenger_lines_count"] or 0),
            )
        )

    return result


def station_quality_score(row: StationAuditRow) -> float:
    score = 0.0

    if row.non_service_lines_count > 0:
        score += 100.0
    if row.main_passenger_lines_count > 0:
        score += 60.0
    if row.is_main_rail_station:
        score += 35.0
    if row.uic_ref:
        score += 20.0
    if row.esr_user:
        score += 12.0
    if row.station_type:
        score += 5.0
    if row.name:
        score += min(len(row.name.strip()) * 0.2, 8.0)

    score += min(row.non_service_lines_count * 6.0, 18.0)
    score += min(row.main_passenger_lines_count * 10.0, 20.0)

    if row.service_lines_count > 0 and row.non_service_lines_count == 0:
        score -= 40.0
    if row.nearby_lines_count == 0:
        score -= 70.0

    return score


def build_station_decisions(rows: list[StationAuditRow]) -> dict[int, dict]:
    decisions: dict[int, dict] = {}

    for row in rows:
        decision = {
            "station_id": row.id,
            "name": row.name,
            "region_code": row.region_code,
            "uic_ref": row.uic_ref,
            "current_is_visible_default": row.is_visible_default,
            "current_exclude_reason": row.exclude_reason,
            "nearby_lines_count": row.nearby_lines_count,
            "service_lines_count": row.service_lines_count,
            "non_service_lines_count": row.non_service_lines_count,
            "main_passenger_lines_count": row.main_passenger_lines_count,
            "quality_score": round(station_quality_score(row), 3),
            "suggested_action": "keep",
            "suggested_exclude_reason": None,
            "notes": [],
        }

        if row.nearby_lines_count == 0:
            decision["suggested_action"] = "hide_default"
            decision["suggested_exclude_reason"] = "station_without_nearby_lines"
            decision["notes"].append("Станция не привязана ни к одной линии в заданном радиусе.")
        elif row.non_service_lines_count == 0 and row.service_lines_count > 0:
            decision["suggested_action"] = "hide_default"
            decision["suggested_exclude_reason"] = "station_connected_only_to_service_lines"
            decision["notes"].append("Станция привязана только к служебным линиям.")
        elif not row.uic_ref and row.non_service_lines_count == 0:
            decision["suggested_action"] = "review"
            decision["suggested_exclude_reason"] = None
            decision["notes"].append("Нет UIC и нет подтверждённой привязки к неслужебной линии.")
        elif not row.uic_ref and row.non_service_lines_count > 0:
            decision["notes"].append("UIC не указан, но станция сидит на неслужебной линии — оставляем.")
        else:
            decision["notes"].append("Критичных проблем не найдено.")

        decisions[row.id] = decision

    return decisions


def apply_duplicate_analysis(rows: list[StationAuditRow], decisions: dict[int, dict]) -> list[dict]:
    groups: dict[str, list[StationAuditRow]] = defaultdict(list)

    for row in rows:
        if row.uic_ref:
            groups[row.uic_ref].append(row)

    duplicate_reports: list[dict] = []

    for uic_ref, group_rows in groups.items():
        if len(group_rows) <= 1:
            continue

        ranked = sorted(
            group_rows,
            key=lambda item: (
                station_quality_score(item),
                1 if item.is_visible_default else 0,
                -(item.id),
            ),
            reverse=True,
        )

        top = ranked[0]
        second = ranked[1]
        top_score = station_quality_score(top)
        second_score = station_quality_score(second)
        score_gap = top_score - second_score

        group_payload = {
            "uic_ref": uic_ref,
            "stations": [
                {
                    "station_id": item.id,
                    "name": item.name,
                    "region_code": item.region_code,
                    "nearby_lines_count": item.nearby_lines_count,
                    "service_lines_count": item.service_lines_count,
                    "non_service_lines_count": item.non_service_lines_count,
                    "main_passenger_lines_count": item.main_passenger_lines_count,
                    "quality_score": round(station_quality_score(item), 3),
                }
                for item in ranked
            ],
            "decision": "review",
            "canonical_station_id": None,
        }

        clear_winner = (
            top_score >= 90.0
            and score_gap >= DUPLICATE_SCORE_GAP_THRESHOLD
            and top.non_service_lines_count > 0
        )

        if clear_winner:
            group_payload["decision"] = "canonical_resolved"
            group_payload["canonical_station_id"] = top.id

            decisions[top.id]["notes"].append(
                f"Выбрана как каноническая станция для duplicate UIC={uic_ref}."
            )

            for duplicate_row in ranked[1:]:
                duplicate_decision = decisions[duplicate_row.id]
                duplicate_decision["suggested_action"] = "hide_default"
                duplicate_decision["suggested_exclude_reason"] = "duplicate_uic_ref_noncanonical"
                duplicate_decision["notes"].append(
                    f"Неканоническая станция в группе duplicate UIC={uic_ref}; canonical station_id={top.id}."
                )
        else:
            group_payload["decision"] = "review"
            group_payload["canonical_station_id"] = top.id

            for duplicate_row in ranked:
                decisions[duplicate_row.id]["notes"].append(
                    f"Есть duplicate UIC={uic_ref}, но случай спорный — оставлено на review."
                )
                if decisions[duplicate_row.id]["suggested_action"] == "keep":
                    decisions[duplicate_row.id]["suggested_action"] = "review"

        duplicate_reports.append(group_payload)

    return duplicate_reports


def apply_changes(decisions: dict[int, dict]) -> dict:
    updates_to_hide = [
        item
        for item in decisions.values()
        if item["suggested_action"] == "hide_default"
        and item["suggested_exclude_reason"]
    ]

    updated_count = 0

    if not updates_to_hide:
        return {
            "updated_count": 0,
            "updated_station_ids": [],
        }

    query = text("""
        UPDATE stations
        SET
            is_visible_default = FALSE,
            exclude_reason = :exclude_reason
        WHERE id = :station_id;
    """)

    with engine.begin() as connection:
        for item in updates_to_hide:
            connection.execute(
                query,
                {
                    "station_id": item["station_id"],
                    "exclude_reason": item["suggested_exclude_reason"],
                },
            )
            updated_count += 1

    return {
        "updated_count": updated_count,
        "updated_station_ids": [item["station_id"] for item in updates_to_hide],
    }


def build_summary(rows: list[StationAuditRow], decisions: dict[int, dict], duplicate_reports: list[dict]) -> dict:
    action_counts: dict[str, int] = defaultdict(int)
    reason_counts: dict[str, int] = defaultdict(int)

    for decision in decisions.values():
        action_counts[decision["suggested_action"]] += 1
        if decision["suggested_exclude_reason"]:
            reason_counts[decision["suggested_exclude_reason"]] += 1

    return {
        "stations_total": len(rows),
        "actions": dict(sorted(action_counts.items())),
        "exclude_reasons": dict(sorted(reason_counts.items())),
        "duplicate_groups_count": len(duplicate_reports),
    }


def main():
    args = parse_args()
    report_path = ensure_report_path(args.report_path)

    print("[START] Проверяю базовые объёмы данных...")
    t0 = time.perf_counter()
    basic_counts = load_basic_counts()
    print_timing("load_basic_counts", t0)
    print(json.dumps(basic_counts, ensure_ascii=False, indent=2))

    print("[START] Загружаю станции и статистику привязки к линиям...")
    t1 = time.perf_counter()
    rows = load_station_rows(args.snap_meters)
    print_timing("load_station_rows", t1)
    print(f"[INFO] Загружено станций: {len(rows)}")

    print("[STEP] Формирую базовые решения по качеству станций...")
    t2 = time.perf_counter()
    decisions = build_station_decisions(rows)
    print_timing("build_station_decisions", t2)

    print("[STEP] Анализирую дубликаты UIC...")
    t3 = time.perf_counter()
    duplicate_reports = apply_duplicate_analysis(rows, decisions)
    print_timing("apply_duplicate_analysis", t3)

    t4 = time.perf_counter()
    summary = build_summary(rows, decisions, duplicate_reports)
    print_timing("build_summary", t4)

    apply_result = {
        "updated_count": 0,
        "updated_station_ids": [],
    }

    if args.apply:
        print("[STEP] Применяю изменения в БД...")
        t5 = time.perf_counter()
        apply_result = apply_changes(decisions)
        print_timing("apply_changes", t5)
        print(f"[DONE] Обновлено станций: {apply_result['updated_count']}")
    else:
        print("[INFO] Режим dry-run: изменения в БД не применялись.")

    suspicious_examples = [
        item
        for item in decisions.values()
        if item["suggested_action"] in {"hide_default", "review"}
    ][:200]

    report = {
        "generated_at": datetime.now().isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "snap_meters": args.snap_meters,
        "basic_counts": basic_counts,
        "summary": summary,
        "apply_result": apply_result,
        "duplicate_reports": duplicate_reports,
        "suspicious_examples": suspicious_examples,
    }

    t6 = time.perf_counter()
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print_timing("write_report", t6)

    print("[SUMMARY]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[DONE] Отчёт сохранён: {report_path}")


if __name__ == "__main__":
    main()