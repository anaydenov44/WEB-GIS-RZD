import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.route_graph_matcher import resolve_route_for_map  # noqa: E402


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def ensure_output_dir() -> Path:
    output_dir = BASE_DIR / "debug_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def short(value: Any, max_len: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=json_default)
    if len(text) <= max_len:
        return text
    return text[:max_len] + " ..."


def extract_brief_summary(result: dict[str, Any]) -> dict[str, Any]:
    diagnostics = result.get("diagnostics") or {}
    fallback_mode = diagnostics.get("fallback_mode") or {}
    network = diagnostics.get("network") or {}
    network_stats = network.get("stats") or {}
    errors = diagnostics.get("errors") or []
    solver_notes = diagnostics.get("solver_notes") or []

    return {
        "route_id": result.get("route", {}).get("id"),
        "geometry_source": result.get("geometry_source"),
        "network_segments_count": len(result.get("network_segments") or []),
        "matched_stops_count": result.get("summary", {}).get("matched_stops_count"),
        "unresolved_stops_count": result.get("summary", {}).get("unresolved_stops_count"),
        "fallback_mode_used": fallback_mode.get("used"),
        "fallback_mode_reason": fallback_mode.get("reason"),
        "network_mode": network_stats.get("network_mode"),
        "visible_stations_count": network_stats.get("visible_stations_count"),
        "adjacency_node_count": network_stats.get("adjacency_node_count"),
        "directed_edge_count": network_stats.get("directed_edge_count"),
        "edge_query_failed": network.get("edge_query_failed"),
        "edge_query_timed_out": network.get("edge_query_timed_out"),
        "solver_notes": solver_notes,
        "errors_count": len(errors),
    }


def print_result_summary(result: dict[str, Any]) -> None:
    diagnostics = result.get("diagnostics") or {}
    summary = result.get("summary") or {}
    fallback_mode = diagnostics.get("fallback_mode") or {}
    network = diagnostics.get("network") or {}
    network_stats = network.get("stats") or {}
    errors = diagnostics.get("errors") or []
    solver_notes = diagnostics.get("solver_notes") or []
    transition_logs = diagnostics.get("transition_diagnostics") or []
    candidate_logs = diagnostics.get("candidate_logs") or []

    print_section("КРАТКАЯ СВОДКА")

    print(f"route_id: {result.get('route', {}).get('id')}")
    print(f"train_number: {result.get('route', {}).get('train_number')}")
    print(f"route_name: {result.get('route', {}).get('route_name')}")
    print(f"geometry_source: {result.get('geometry_source')}")
    print(f"network_segments_count: {len(result.get('network_segments') or [])}")
    print(f"matched_stops_count: {summary.get('matched_stops_count')}")
    print(f"unresolved_stops_count: {summary.get('unresolved_stops_count')}")
    print(f"fallback_mode_used: {fallback_mode.get('used')}")
    print(f"fallback_mode_reason: {fallback_mode.get('reason')}")

    print_section("СОСТОЯНИЕ ГРАФА")

    print(f"network_mode: {network_stats.get('network_mode')}")
    print(f"region_codes: {network_stats.get('region_codes')}")
    print(f"visible_stations_count: {network_stats.get('visible_stations_count')}")
    print(f"adjacency_node_count: {network_stats.get('adjacency_node_count')}")
    print(f"directed_edge_count: {network_stats.get('directed_edge_count')}")
    print(f"graph_snap_meters: {network_stats.get('graph_snap_meters')}")
    print(f"edge_query_timeout_ms: {network_stats.get('edge_query_timeout_ms')}")
    print(f"edge_query_failed: {network.get('edge_query_failed')}")
    print(f"edge_query_timed_out: {network.get('edge_query_timed_out')}")
    print(f"cache_hit: {network.get('cache_hit')}")
    print(f"regional_station_rows_count: {network.get('regional_station_rows_count')}")
    print(f"raw_edge_rows_count: {network.get('raw_edge_rows_count')}")

    if network.get("edge_query_exception"):
        print("edge_query_exception:")
        print(short(network.get("edge_query_exception"), max_len=1200))

    print_section("SOLVER")

    print(f"solver_notes: {solver_notes}")
    print(f"transition_logs_count: {len(transition_logs)}")
    print(f"candidate_logs_count: {len(candidate_logs)}")

    if transition_logs:
        print("Первые 5 transition logs:")
        for item in transition_logs[:5]:
            print(short(item, max_len=1200))

    if errors:
        print_section("ОШИБКИ")
        for index, error in enumerate(errors, start=1):
            print(f"[{index}] {short(error, max_len=2000)}")
    else:
        print_section("ОШИБКИ")
        print("Ошибок не зафиксировано.")

    print_section("КАНДИДАТЫ ПО ПЕРВЫМ ОСТАНОВКАМ")

    for item in candidate_logs[:5]:
        stop_sequence = item.get("stop_sequence")
        station_name_raw = item.get("station_name_raw")
        candidate_count = item.get("candidate_count")
        print(f"stop_sequence={stop_sequence} | station_name_raw={station_name_raw} | candidate_count={candidate_count}")
        for candidate in (item.get("candidates") or [])[:3]:
            print(f"  - {short(candidate, max_len=800)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Отладка route build без сохранения результата в БД."
    )
    parser.add_argument("route_id", type=int, help="ID маршрута из таблицы routes")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Путь к JSON-файлу для сохранения полного результата",
    )
    parser.add_argument(
        "--full-console",
        action="store_true",
        help="Печатать полный JSON результата в консоль",
    )

    args = parser.parse_args()

    print_section("START")
    print(f"route_id = {args.route_id}")
    print("persist = False")

    try:
        result = resolve_route_for_map(args.route_id, persist=False)

        brief = extract_brief_summary(result)
        print_result_summary(result)

        if args.output:
            output_path = Path(args.output)
            if not output_path.is_absolute():
                output_path = (Path.cwd() / output_path).resolve()
        else:
            output_dir = ensure_output_dir()
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = output_dir / f"route_debug_{args.route_id}_{timestamp}.json"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=json_default),
            encoding="utf-8",
        )

        print_section("ФАЙЛ СОХРАНЁН")
        print(str(output_path))

        print_section("BRIEF JSON")
        print(json.dumps(brief, ensure_ascii=False, indent=2, default=json_default))

        if args.full_console:
            print_section("ПОЛНЫЙ RESULT JSON")
            print(json.dumps(result, ensure_ascii=False, indent=2, default=json_default))

    except Exception as exc:
        print_section("EXCEPTION")
        print(f"{exc.__class__.__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()