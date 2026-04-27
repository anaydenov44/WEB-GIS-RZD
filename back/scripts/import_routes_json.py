import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.route_import_service import import_route_payload  # noqa: E402


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python scripts/import_routes_json.py <path_to_json>")

    json_path = Path(sys.argv[1]).resolve()
    if not json_path.exists():
        raise FileNotFoundError(f"Не найден файл: {json_path}")

    payload = json.loads(json_path.read_text(encoding="utf-8"))

    if isinstance(payload, list):
        results = []
        for index, item in enumerate(payload, start=1):
            print(f"[{index}/{len(payload)}] Импортирую маршрут...")
            result = import_route_payload(
                item,
                source_name="json_file",
                requested_scope=str(json_path.name),
            )
            results.append(result)
            print(
                f"  route_id={result['route_id']}, "
                f"matched={result['matched_stops_count']}, "
                f"unresolved={result['unresolved_stops_count']}"
            )

        print("Готово.")
        print(f"Импортировано маршрутов: {len(results)}")
        return

    if not isinstance(payload, dict):
        raise ValueError("JSON должен содержать объект маршрута или массив маршрутов")

    result = import_route_payload(
        payload,
        source_name="json_file",
        requested_scope=str(json_path.name),
    )

    print("Готово.")
    print(f"route_id={result['route_id']}")
    print(f"matched_stops_count={result['matched_stops_count']}")
    print(f"unresolved_stops_count={result['unresolved_stops_count']}")


if __name__ == "__main__":
    main()