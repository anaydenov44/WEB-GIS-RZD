from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import text

from app.db import engine


DEFAULT_REPORT_DIR = BASE_DIR / "data" / "audit"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Безопасное применение очистки станций: stable service-only и опционально duplicate_uic_ref_noncanonical."
    )
    parser.add_argument(
        "--compare-report",
        required=True,
        help="Путь к JSON-отчёту compare_station_line_membership.py",
    )
    parser.add_argument(
        "--audit-report",
        default="",
        help="Путь к JSON-отчёту audit_station_quality.py. Нужен только если включены duplicates.",
    )
    parser.add_argument(
        "--include-duplicates",
        action="store_true",
        help="Дополнительно скрыть duplicate_uic_ref_noncanonical из audit-report.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить изменения в БД. Без флага работает как dry-run.",
    )
    parser.add_argument(
        "--output-report",
        default="",
        help="Куда сохранить отчёт о применении. Если не указано, создаётся автоматически.",
    )
    return parser.parse_args()


def ensure_output_report_path(output_report_raw: str) -> Path:
    if output_report_raw:
        path = Path(output_report_raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_REPORT_DIR / f"safe_station_cleanup_apply_{timestamp}.json"


def load_json(path_raw: str) -> dict:
    path = Path(path_raw)
    if not path.exists():
        raise FileNotFoundError(f"Не найден файл отчёта: {path}")

    return json.loads(path.read_text(encoding="utf-8"))


def extract_stable_service_only_station_ids(compare_report: dict) -> set[int]:
    ppg = compare_report.get("persistent_problem_groups", {})
    items = ppg.get("persistent_service_only", [])

    result: set[int] = set()

    for item in items:
        station_id = item.get("station_id")
        if isinstance(station_id, int):
            result.add(station_id)

    return result


def extract_duplicate_noncanonical_station_ids(audit_report: dict) -> set[int]:
    suspicious_examples = audit_report.get("suspicious_examples", [])
    result: set[int] = set()

    for item in suspicious_examples:
        if item.get("suggested_action") != "hide_default":
            continue
        if item.get("suggested_exclude_reason") != "duplicate_uic_ref_noncanonical":
            continue

        station_id = item.get("station_id")
        if isinstance(station_id, int):
            result.add(station_id)

    return result


def load_current_station_state(station_ids: set[int]) -> list[dict]:
    if not station_ids:
        return []

    station_ids_sorted = sorted(station_ids)

    placeholders = []
    params = {}

    for index, station_id in enumerate(station_ids_sorted):
        key = f"station_id_{index}"
        placeholders.append(f":{key}")
        params[key] = station_id

    query = text(f"""
        SELECT
            id,
            name,
            region_code,
            is_visible_default,
            exclude_reason
        FROM stations
        WHERE id IN ({", ".join(placeholders)})
        ORDER BY id;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query, params).fetchall()

    return [dict(row._mapping) for row in rows]


def build_update_plan(
    compare_report: dict,
    audit_report: dict | None,
    include_duplicates: bool,
) -> dict:
    stable_service_only_ids = extract_stable_service_only_station_ids(compare_report)

    duplicate_noncanonical_ids: set[int] = set()
    if include_duplicates:
        if audit_report is None:
            raise ValueError(
                "Для --include-duplicates необходимо передать --audit-report."
            )
        duplicate_noncanonical_ids = extract_duplicate_noncanonical_station_ids(audit_report)

    planned_reasons: dict[int, str] = {}

    for station_id in stable_service_only_ids:
        planned_reasons[station_id] = "station_connected_only_to_service_lines"

    for station_id in duplicate_noncanonical_ids:
        if station_id not in planned_reasons:
            planned_reasons[station_id] = "duplicate_uic_ref_noncanonical"

    current_rows = load_current_station_state(set(planned_reasons.keys()))

    updates = []
    unchanged = []
    missing_station_ids = set(planned_reasons.keys())

    for row in current_rows:
        station_id = row["id"]
        missing_station_ids.discard(station_id)

        target_reason = planned_reasons[station_id]
        current_visible = row.get("is_visible_default")
        current_reason = row.get("exclude_reason")

        item = {
            "station_id": station_id,
            "name": row.get("name"),
            "region_code": row.get("region_code"),
            "current_is_visible_default": current_visible,
            "current_exclude_reason": current_reason,
            "target_exclude_reason": target_reason,
        }

        if current_visible is False and current_reason == target_reason:
            unchanged.append({
                **item,
                "status": "already_applied",
            })
            continue

        updates.append({
            **item,
            "status": "to_update",
        })

    return {
        "stable_service_only_station_ids": sorted(stable_service_only_ids),
        "duplicate_noncanonical_station_ids": sorted(duplicate_noncanonical_ids),
        "updates": updates,
        "unchanged": unchanged,
        "missing_station_ids": sorted(missing_station_ids),
    }


def apply_updates(updates: list[dict]) -> dict:
    if not updates:
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

    updated_station_ids = []

    with engine.begin() as connection:
        for item in updates:
            connection.execute(
                query,
                {
                    "station_id": item["station_id"],
                    "exclude_reason": item["target_exclude_reason"],
                },
            )
            updated_station_ids.append(item["station_id"])

    return {
        "updated_count": len(updated_station_ids),
        "updated_station_ids": updated_station_ids,
    }


def main():
    args = parse_args()

    compare_report_path = Path(args.compare_report)
    compare_report = load_json(str(compare_report_path))

    audit_report = None
    audit_report_path = None
    if args.audit_report:
        audit_report_path = Path(args.audit_report)
        audit_report = load_json(str(audit_report_path))

    output_report_path = ensure_output_report_path(args.output_report)

    print("[START] Загружаю compare-report...")
    print(f"[INFO] compare-report: {compare_report_path}")

    if args.include_duplicates:
        if audit_report_path is None:
            raise ValueError("Для --include-duplicates требуется --audit-report")
        print("[INFO] duplicates mode: ON")
        print(f"[INFO] audit-report: {audit_report_path}")
    else:
        print("[INFO] duplicates mode: OFF")

    print("[STEP] Формирую безопасный план очистки...")
    plan = build_update_plan(
        compare_report=compare_report,
        audit_report=audit_report,
        include_duplicates=args.include_duplicates,
    )

    updates = plan["updates"]
    unchanged = plan["unchanged"]
    missing_station_ids = plan["missing_station_ids"]

    summary = {
        "stable_service_only_count": len(plan["stable_service_only_station_ids"]),
        "duplicate_noncanonical_count": len(plan["duplicate_noncanonical_station_ids"]),
        "to_update_count": len(updates),
        "already_applied_count": len(unchanged),
        "missing_station_ids_count": len(missing_station_ids),
    }

    print("[SUMMARY]")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    apply_result = {
        "updated_count": 0,
        "updated_station_ids": [],
    }

    if args.apply:
        print("[STEP] Применяю изменения в БД...")
        apply_result = apply_updates(updates)
        print(f"[DONE] Обновлено станций: {apply_result['updated_count']}")
    else:
        print("[INFO] Режим dry-run: изменения в БД не применялись.")

    report = {
        "generated_at": datetime.now().isoformat(),
        "mode": "apply" if args.apply else "dry_run",
        "compare_report_path": str(compare_report_path),
        "audit_report_path": str(audit_report_path) if audit_report_path else None,
        "include_duplicates": args.include_duplicates,
        "summary": summary,
        "apply_result": apply_result,
        "updates_preview": updates[:300],
        "unchanged_preview": unchanged[:100],
        "missing_station_ids": missing_station_ids,
    }

    output_report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"[DONE] Отчёт сохранён: {output_report_path}")


if __name__ == "__main__":
    main()