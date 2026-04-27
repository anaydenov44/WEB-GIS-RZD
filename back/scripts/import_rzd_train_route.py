import argparse
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

        # пропускаем служебную строку типа ['МОСКВА КАЗ', 'АНАПА']
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


def main():
    parser = argparse.ArgumentParser(
        description="Импорт живого маршрута РЖД в route-layer БД"
    )
    parser.add_argument("--train-number", required=True, help="Например 012М")
    parser.add_argument("--date", required=True, help="Дата в формате d.m.Y, например 20.04.2026")
    parser.add_argument("--save-raw", action="store_true")
    args = parser.parse_args()

    client = RzdClient()

    print(f"[RZD] Получаю station-list для поезда {args.train_number} на {args.date}...")
    result = client.get_train_station_list(
        train_number=args.train_number,
        dep_date=args.date,
    )

    if args.save_raw:
        output_path = client.save_payload_to_file(
            result["raw"],
            BASE_DIR / "data" / "rzd_samples",
            f"live_train_{args.train_number}",
        )
        print(f"[RZD] Raw сохранён: {output_path}")

    stops = normalize_live_station_list(result)
    if len(stops) < 2:
        raise RuntimeError("После нормализации осталось меньше 2 остановок")

    origin = stops[0]["station_name_raw"]
    destination = stops[-1]["station_name_raw"]
    snapshot_date = to_iso_date(args.date)

    payload = {
        "source_system": "rzd",
        "external_route_id": f"rzd:{args.train_number}:{snapshot_date}",
        "train_number": args.train_number,
        "route_name": f"{origin} — {destination}",
        "origin_station_name": origin,
        "destination_station_name": destination,
        "origin_station_code": stops[0]["station_code_rzd"],
        "destination_station_code": stops[-1]["station_code_rzd"],
        "snapshot_date": snapshot_date,
        "is_active": True,
        "notes": f"Импортировано из live RZD station-list, source_mode={result['request']['source_mode']}",
        "stops": stops,
    }

    print("[RZD] Импортирую маршрут в БД...")
    import_result = import_route_payload(
        payload,
        source_name="rzd_live_import",
        requested_scope=f"{args.train_number}:{snapshot_date}",
    )

    print("[RZD] Готово.")
    print(f"route_id={import_result['route_id']}")
    print(f"route_sync_run_id={import_result['route_sync_run_id']}")
    print(f"matched_stops_count={import_result['matched_stops_count']}")
    print(f"unresolved_stops_count={import_result['unresolved_stops_count']}")


if __name__ == "__main__":
    main()