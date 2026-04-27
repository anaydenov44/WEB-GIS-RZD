import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.route_import_service import import_route_payload  # noqa: E402
from app.rzd_client import RzdClient  # noqa: E402


def to_iso_date(date_str: str) -> str:
    return datetime.strptime(date_str, "%d.%m.%Y").date().isoformat()


def normalize_live_station_list(result: dict) -> list[dict]:
    raw_items = result.get("items") or []
    normalized = []
    sequence = 1

    for item in raw_items:
        station_name = item.get("station_name")
        station_code = item.get("station_code")

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
        raise ValueError("Реестр поездов должен быть непустым JSON-массивом")
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Batch-импорт выбранных живых маршрутов RZD в БД"
    )
    parser.add_argument("registry_json", help="Путь к JSON-реестру поездов")
    parser.add_argument("--save-raw", action="store_true")
    args = parser.parse_args()

    registry_path = Path(args.registry_json).resolve()
    if not registry_path.exists():
        raise FileNotFoundError(f"Не найден реестр: {registry_path}")

    registry = load_registry(registry_path)
    client = RzdClient()

    imported = 0
    failed = 0

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
                    f"batch_train_{train_number}",
                )
                print(f"[RAW] {raw_path}")

            stops = normalize_live_station_list(result)
            if len(stops) < 2:
                raise RuntimeError("После нормализации осталось меньше 2 остановок")

            snapshot_date = to_iso_date(dep_date)
            origin = stops[0]["station_name_raw"]
            destination = stops[-1]["station_name_raw"]

            payload = {
                "source_system": "rzd",
                "external_route_id": f"rzd:{train_number}:{snapshot_date}",
                "train_number": train_number,
                "route_name": f"{origin} — {destination}",
                "origin_station_name": origin,
                "destination_station_name": destination,
                "origin_station_code": stops[0]["station_code_rzd"],
                "destination_station_code": stops[-1]["station_code_rzd"],
                "snapshot_date": snapshot_date,
                "is_active": True,
                "notes": f"Batch import из live RZD station-list, source_mode={result['request']['source_mode']}",
                "stops": stops,
            }

            import_result = import_route_payload(
                payload,
                source_name="rzd_batch_import",
                requested_scope=f"{train_number}:{snapshot_date}",
            )

            imported += 1
            print(
                f"[OK] {train_number} | route_id={import_result['route_id']} | "
                f"matched={import_result['matched_stops_count']} | "
                f"unresolved={import_result['unresolved_stops_count']}"
            )

        except Exception as exc:
            failed += 1
            print(f"[FAIL] {train_number} | {dep_date} | {exc}")

    print("-" * 72)
    print(f"[DONE] imported={imported}, failed={failed}")


if __name__ == "__main__":
    main()