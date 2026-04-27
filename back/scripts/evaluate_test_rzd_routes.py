import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.db import engine  # noqa: E402
from app.route_import_service import resolve_station_match  # noqa: E402
from app.rzd_client import RzdClient  # noqa: E402


def normalize_live_station_list(result: dict) -> list[dict]:
    raw_items = result.get("items") or []
    normalized = []
    sequence = 1

    for item in raw_items:
        station_name = item.get("station_name")
        station_code = item.get("station_code")

        # пропускаем служебную строку вида ['МОСКВА КАЗ', 'АНАПА']
        if isinstance(station_name, list):
            continue

        if station_name is not None:
            station_name = str(station_name).strip()

        if station_code is not None:
            station_code = str(station_code).strip()

        if not station_name and not station_code:
            continue

        distance = item.get("distance")
        distance_km = float(distance) if distance is not None else None

        normalized.append(
            {
                "stop_sequence": sequence,
                "station_name_raw": station_name,
                "station_code_rzd": station_code,
                "arrival_time": item.get("arrival_time"),
                "departure_time": item.get("departure_time"),
                "distance_km": distance_km,
            }
        )
        sequence += 1

    return normalized


def load_registry(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("Реестр тестовых поездов должен быть непустым JSON-массивом")
    return data


def print_train_summary_row(item: dict) -> None:
    print(
        f"[OK] {item['train_number']:>6} | "
        f"stops={item['stops_total']:>2} | "
        f"matched={item['matched_stops_count']:>2} | "
        f"unresolved={item['unresolved_stops_count']:>2} | "
        f"ratio={item['matched_ratio_pct']:>6.2f}% | "
        f"{item['origin_station_name']} -> {item['destination_station_name']}"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Dry-run проверка 5-10 тестовых маршрутов RZD без импорта в routes"
    )
    parser.add_argument(
        "registry_json",
        help="Путь к JSON-реестру тестовых поездов",
    )
    parser.add_argument(
        "--save-raw",
        action="store_true",
        help="Сохранять raw-ответы station-list в data/rzd_samples",
    )
    parser.add_argument(
        "--output",
        default="data/routes/test_routes_evaluation_report.json",
        help="Путь для итогового JSON-отчёта",
    )
    parser.add_argument(
        "--unresolved-limit",
        type=int,
        default=10,
        help="Сколько unresolved остановок сохранять по каждому маршруту в отчёт",
    )
    args = parser.parse_args()

    registry_path = Path(args.registry_json).resolve()
    if not registry_path.exists():
        raise FileNotFoundError(f"Не найден реестр: {registry_path}")

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    registry = load_registry(registry_path)
    client = RzdClient()

    report_items = []
    unresolved_counter = Counter()
    processed_count = 0
    failed_count = 0

    with engine.connect() as connection:
        for entry in registry:
            train_number = str(entry["train_number"]).strip()
            dep_date = str(entry["date"]).strip()
            label = entry.get("label")

            print(f"[START] {train_number} | {dep_date} | {label or '-'}")

            try:
                result = client.get_train_station_list(
                    train_number=train_number,
                    dep_date=dep_date,
                )

                if args.save_raw:
                    raw_path = client.save_payload_to_file(
                        result["raw"],
                        BASE_DIR / "data" / "rzd_samples",
                        f"test_train_{train_number}",
                    )
                else:
                    raw_path = None

                stops = normalize_live_station_list(result)
                if len(stops) < 2:
                    raise RuntimeError("После нормализации осталось меньше 2 остановок")

                matched_count = 0
                unresolved_count = 0
                unresolved_samples = []

                for stop in stops:
                    match_result = resolve_station_match(
                        connection=connection,
                        station_name_raw=stop.get("station_name_raw"),
                        station_code_rzd=stop.get("station_code_rzd"),
                        explicit_station_id=None,
                    )

                    if match_result["station_id"] is not None:
                        matched_count += 1
                    else:
                        unresolved_count += 1
                        unresolved_key = (
                            stop.get("station_code_rzd") or "",
                            stop.get("station_name_raw") or "",
                        )
                        unresolved_counter[unresolved_key] += 1

                        if len(unresolved_samples) < args.unresolved_limit:
                            unresolved_samples.append(
                                {
                                    "stop_sequence": stop["stop_sequence"],
                                    "station_name_raw": stop.get("station_name_raw"),
                                    "station_code_rzd": stop.get("station_code_rzd"),
                                }
                            )

                ratio = (matched_count / len(stops)) * 100 if stops else 0.0

                item = {
                    "train_number": train_number,
                    "date": dep_date,
                    "label": label,
                    "source_mode": result["request"]["source_mode"],
                    "origin_station_name": stops[0]["station_name_raw"],
                    "destination_station_name": stops[-1]["station_name_raw"],
                    "stops_total": len(stops),
                    "matched_stops_count": matched_count,
                    "unresolved_stops_count": unresolved_count,
                    "matched_ratio_pct": round(ratio, 2),
                    "raw_saved_path": str(raw_path) if raw_path else None,
                    "unresolved_samples": unresolved_samples,
                    "status": "ok",
                }

                report_items.append(item)
                processed_count += 1
                print_train_summary_row(item)

            except Exception as exc:
                failed_count += 1
                failed_item = {
                    "train_number": train_number,
                    "date": dep_date,
                    "label": label,
                    "status": "failed",
                    "error": str(exc),
                }
                report_items.append(failed_item)
                print(f"[FAIL] {train_number} | {dep_date} | {exc}")

    unresolved_top = []
    for (station_code_rzd, station_name_raw), count in unresolved_counter.most_common(100):
        unresolved_top.append(
            {
                "station_code_rzd": station_code_rzd or None,
                "station_name_raw": station_name_raw or None,
                "count": count,
            }
        )

    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "registry_path": str(registry_path),
        "processed_count": processed_count,
        "failed_count": failed_count,
        "items_total": len(registry),
        "report_items_ok": sum(1 for item in report_items if item.get("status") == "ok"),
        "report_items_failed": sum(1 for item in report_items if item.get("status") == "failed"),
    }

    payload = {
        "summary": summary,
        "routes": report_items,
        "unresolved_top": unresolved_top,
    }

    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("-" * 72)
    print(f"[DONE] Отчёт сохранён: {output_path}")
    print(f"[DONE] processed={processed_count}, failed={failed_count}")
    print("[DONE] Топ unresolved station codes:")
    for item in unresolved_top[:15]:
        print(
            f"  - code={item['station_code_rzd']} | "
            f"name={item['station_name_raw']} | "
            f"count={item['count']}"
        )


if __name__ == "__main__":
    main()