import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.rzd_client import RzdClient, RzdClientError  # noqa: E402


DEFAULT_OUTPUT_DIR = BASE_DIR / "data" / "rzd_samples"


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_station_code(args):
    client = RzdClient()
    result = client.search_station_codes(
        station_name_part=args.query,
        lang=args.lang,
        compact_mode=not args.no_compact,
    )

    print(f"Найдено станций: {result['count']}")
    for index, item in enumerate(result["items"][: args.limit], start=1):
        print(f"{index}. {item['name']} | code={item['code']}")

    if args.save:
        path = client.save_payload_to_file(result["raw"], DEFAULT_OUTPUT_DIR, "station_code")
        print(f"\nRaw сохранён: {path}")

    if args.full:
        print()
        print_json(result)


def cmd_routes(args):
    client = RzdClient()
    result = client.search_routes(
        code0=args.code0,
        code1=args.code1,
        dep_date=args.date,
        dir_value=args.dir,
        tfl=args.tfl,
        check_seats=args.check_seats,
        include_transfers=args.include_transfers,
    )

    print(f"Найдено маршрутов/поездов: {result['count']}")
    for index, item in enumerate(result["items"][: args.limit], start=1):
        print(
            f"{index}. № {item.get('number')} | "
            f"{item.get('time0')} -> {item.get('time1')} | "
            f"{item.get('route0')} -> {item.get('route1')} | "
            f"{item.get('brand') or 'без бренда'}"
        )

    if args.save:
        path = client.save_payload_to_file(result["raw"], DEFAULT_OUTPUT_DIR, "routes")
        print(f"\nRaw сохранён: {path}")

    if args.full:
        print()
        print_json(result)


def cmd_station_list(args):
    client = RzdClient()
    result = client.get_train_station_list(
        train_number=args.train_number,
        dep_date=args.date,
    )

    print(f"Остановок найдено: {result['count']}")
    print(f"Источник: {result['request']['source_mode']}")
    for index, item in enumerate(result["items"][: args.limit], start=1):
        print(
            f"{index}. {item.get('station_name')} | "
            f"code={item.get('station_code')} | "
            f"arr={item.get('arrival_time')} | "
            f"dep={item.get('departure_time')} | "
            f"dist={item.get('distance')}"
        )

    if args.save:
        path = client.save_payload_to_file(result["raw"], DEFAULT_OUTPUT_DIR, "station_list")
        print(f"\nRaw сохранён: {path}")

    if args.full:
        print()
        print_json(result)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Диагностический CLI для исследования неофициального RZD API"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    station_code_parser = subparsers.add_parser(
        "station-code",
        help="Поиск кодов станций по части названия",
    )
    station_code_parser.add_argument("query", help="Часть названия станции, например МОСК")
    station_code_parser.add_argument("--lang", default="ru", choices=["ru", "en"])
    station_code_parser.add_argument("--no-compact", action="store_true")
    station_code_parser.add_argument("--limit", type=int, default=20)
    station_code_parser.add_argument("--save", action="store_true")
    station_code_parser.add_argument("--full", action="store_true")
    station_code_parser.set_defaults(func=cmd_station_code)

    routes_parser = subparsers.add_parser(
        "routes",
        help="Поиск поездов/маршрутов между station codes на дату",
    )
    routes_parser.add_argument("--code0", required=True, help="Код станции отправления")
    routes_parser.add_argument("--code1", required=True, help="Код станции прибытия")
    routes_parser.add_argument("--date", required=True, help="Дата в формате d.m.Y, например 20.04.2026")
    routes_parser.add_argument("--dir", type=int, default=0, choices=[0, 1])
    routes_parser.add_argument("--tfl", type=int, default=3, choices=[1, 2, 3])
    routes_parser.add_argument("--check-seats", action="store_true")
    routes_parser.add_argument("--include-transfers", action="store_true")
    routes_parser.add_argument("--limit", type=int, default=20)
    routes_parser.add_argument("--save", action="store_true")
    routes_parser.add_argument("--full", action="store_true")
    routes_parser.set_defaults(func=cmd_routes)

    station_list_parser = subparsers.add_parser(
        "station-list",
        help="Список остановок конкретного поезда на дату",
    )
    station_list_parser.add_argument("--train-number", required=True, help="Номер поезда, например 054Г")
    station_list_parser.add_argument("--date", required=True, help="Дата в формате d.m.Y, например 20.04.2026")
    station_list_parser.add_argument("--limit", type=int, default=100)
    station_list_parser.add_argument("--save", action="store_true")
    station_list_parser.add_argument("--full", action="store_true")
    station_list_parser.set_defaults(func=cmd_station_list)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        args.func(args)
    except RzdClientError as exc:
        print(f"[RZD ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()