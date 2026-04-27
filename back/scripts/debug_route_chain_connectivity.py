import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.route_graph_matcher import (  # noqa: E402
    build_candidates_for_stop,
    build_network_data,
    compute_transition_cost,
    dijkstra_shortest_path,
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


def short(value: Any, max_len: int = 1000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=json_default)
    if len(text) <= max_len:
        return text
    return text[:max_len] + " ..."


def ensure_output_dir() -> Path:
    output_dir = BASE_DIR / "debug_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def pick_probe_candidate(stop: dict[str, Any], candidates: list[Any]) -> Any | None:
    stored_station_id = stop.get("stored_station_id")
    if stored_station_id is not None:
        for candidate in candidates:
            if int(candidate.station_id) == int(stored_station_id):
                return candidate

    if candidates:
        return candidates[0]

    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Диагностика solver connectivity по соседним остановкам маршрута."
    )
    parser.add_argument("route_id", type=int, help="ID маршрута")
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Путь к JSON-файлу для сохранения результата",
    )
    args = parser.parse_args()

    print_section("START")
    print(f"route_id = {args.route_id}")

    payload = load_route(args.route_id)
    route = payload["route"]
    stops = payload["stops"]

    print_section("ROUTE")
    print(f"route_id: {route.get('id')}")
    print(f"train_number: {route.get('train_number')}")
    print(f"route_name: {route.get('route_name')}")
    print(f"stops_count: {len(stops)}")

    catalog_payload = load_global_station_catalog()
    candidates_per_stop = [
        build_candidates_for_stop(stop, catalog_payload)
        for stop in stops
    ]

    diagnostics: dict[str, Any] = {}
    inferred_region_codes = infer_route_region_codes(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
        diagnostics=diagnostics,
    )

    print_section("INFERRED REGIONS")
    print(json.dumps(
        diagnostics.get("inferred_route_regions") or {},
        ensure_ascii=False,
        indent=2,
        default=json_default,
    ))

    network_diagnostics: dict[str, Any] = {}
    network = build_network_data(
        region_codes=inferred_region_codes,
        diagnostics=network_diagnostics,
    )
    network_stats = network.get("stats") or {}

    print_section("NETWORK STATS")
    print(json.dumps(network_stats, ensure_ascii=False, indent=2, default=json_default))

    adjacency = network["adjacency"]
    stations_by_id = network["stations_by_id"]
    path_cache: dict[tuple[int, int], dict[str, Any] | None] = {}

    segment_reports: list[dict[str, Any]] = []
    broken_segments: list[dict[str, Any]] = []

    for index in range(1, len(stops)):
        previous_stop = stops[index - 1]
        current_stop = stops[index]
        previous_candidates = candidates_per_stop[index - 1]
        current_candidates = candidates_per_stop[index]

        pairs_evaluated = 0
        reachable_pairs_count = 0
        viable_pairs_count = 0
        rejected_reason_counts: dict[str, int] = {}
        best_viable_pair: dict[str, Any] | None = None
        best_viable_cost: float | None = None

        for previous_candidate in previous_candidates:
            for current_candidate in current_candidates:
                pairs_evaluated += 1

                shortest_path = dijkstra_shortest_path(
                    adjacency=adjacency,
                    stations_by_id=stations_by_id,
                    start_station_id=previous_candidate.station_id,
                    end_station_id=current_candidate.station_id,
                    path_cache=path_cache,
                )

                if shortest_path is not None:
                    reachable_pairs_count += 1

                transition_cost, transition_diag = compute_transition_cost(
                    previous_stop=previous_stop,
                    next_stop=current_stop,
                    shortest_path=shortest_path,
                )

                if transition_cost is None:
                    reason = transition_diag.get("rejected_reason") or "unknown_reject_reason"
                    rejected_reason_counts[reason] = rejected_reason_counts.get(reason, 0) + 1
                    continue

                viable_pairs_count += 1

                pair_payload = {
                    "from_station_id": previous_candidate.station_id,
                    "from_station_name": previous_candidate.name,
                    "from_region_code": previous_candidate.region_code,
                    "from_effective_score": round(previous_candidate.effective_score, 4),
                    "to_station_id": current_candidate.station_id,
                    "to_station_name": current_candidate.name,
                    "to_region_code": current_candidate.region_code,
                    "to_effective_score": round(current_candidate.effective_score, 4),
                    "transition_cost": round(float(transition_cost), 4),
                    "graph_distance_km": (
                        round(float(shortest_path["distance_km"]), 3)
                        if shortest_path is not None
                        else None
                    ),
                    "hop_count": shortest_path.get("hop_count") if shortest_path is not None else None,
                    "edge_count": len(shortest_path.get("edge_chain") or []) if shortest_path is not None else None,
                    "diagnostic": transition_diag,
                }

                if best_viable_cost is None or float(transition_cost) < best_viable_cost:
                    best_viable_cost = float(transition_cost)
                    best_viable_pair = pair_payload

        previous_probe = pick_probe_candidate(previous_stop, previous_candidates)
        current_probe = pick_probe_candidate(current_stop, current_candidates)

        probe_path = None
        probe_transition_cost = None
        probe_transition_diag = None

        if previous_probe is not None and current_probe is not None:
            probe_path = dijkstra_shortest_path(
                adjacency=adjacency,
                stations_by_id=stations_by_id,
                start_station_id=previous_probe.station_id,
                end_station_id=current_probe.station_id,
                path_cache=path_cache,
            )
            probe_transition_cost, probe_transition_diag = compute_transition_cost(
                previous_stop=previous_stop,
                next_stop=current_stop,
                shortest_path=probe_path,
            )

        segment_report = {
            "segment_index": index,
            "from_stop_sequence": previous_stop.get("stop_sequence"),
            "to_stop_sequence": current_stop.get("stop_sequence"),
            "from_station_name_raw": previous_stop.get("station_name_raw"),
            "to_station_name_raw": current_stop.get("station_name_raw"),
            "delta_rzd_km": (
                (current_stop.get("distance_km") or 0) - (previous_stop.get("distance_km") or 0)
                if previous_stop.get("distance_km") is not None and current_stop.get("distance_km") is not None
                else None
            ),
            "previous_candidates_count": len(previous_candidates),
            "current_candidates_count": len(current_candidates),
            "pairs_evaluated": pairs_evaluated,
            "reachable_pairs_count": reachable_pairs_count,
            "viable_pairs_count": viable_pairs_count,
            "rejected_reason_counts": rejected_reason_counts,
            "best_viable_pair": best_viable_pair,
            "probe_pair": {
                "from_station_id": previous_probe.station_id if previous_probe else None,
                "from_station_name": previous_probe.name if previous_probe else None,
                "to_station_id": current_probe.station_id if current_probe else None,
                "to_station_name": current_probe.name if current_probe else None,
                "path_exists": probe_path is not None,
                "graph_distance_km": (
                    round(float(probe_path["distance_km"]), 3)
                    if probe_path is not None
                    else None
                ),
                "hop_count": probe_path.get("hop_count") if probe_path is not None else None,
                "edge_count": len(probe_path.get("edge_chain") or []) if probe_path is not None else None,
                "transition_cost": (
                    round(float(probe_transition_cost), 4)
                    if probe_transition_cost is not None
                    else None
                ),
                "transition_diag": probe_transition_diag,
            },
            "from_top_candidates": [
                {
                    "station_id": candidate.station_id,
                    "station_name": candidate.name,
                    "region_code": candidate.region_code,
                    "effective_score": round(candidate.effective_score, 4),
                    "match_method": candidate.match_method,
                    "anchor": candidate.anchor,
                    "code_match": candidate.code_match,
                }
                for candidate in previous_candidates[:3]
            ],
            "to_top_candidates": [
                {
                    "station_id": candidate.station_id,
                    "station_name": candidate.name,
                    "region_code": candidate.region_code,
                    "effective_score": round(candidate.effective_score, 4),
                    "match_method": candidate.match_method,
                    "anchor": candidate.anchor,
                    "code_match": candidate.code_match,
                }
                for candidate in current_candidates[:3]
            ],
        }

        segment_reports.append(segment_report)

        if viable_pairs_count == 0:
            broken_segments.append(segment_report)

    print_section("SEGMENT SUMMARY")
    for item in segment_reports:
        print(
            f"[segment {item['segment_index']}] "
            f"{item['from_station_name_raw']} -> {item['to_station_name_raw']} | "
            f"reachable_pairs={item['reachable_pairs_count']} | "
            f"viable_pairs={item['viable_pairs_count']} | "
            f"probe_path_exists={item['probe_pair']['path_exists']}"
        )

    print_section("BROKEN SEGMENTS")
    if not broken_segments:
        print("Нет сегментов с viable_pairs_count = 0")
    else:
        for item in broken_segments:
            print(short(item, max_len=4000))

    output_payload = {
        "route": route,
        "inferred_route_regions": diagnostics.get("inferred_route_regions") or {},
        "network_stats": network_stats,
        "segment_reports": segment_reports,
        "broken_segments": broken_segments,
    }

    if args.output:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = (Path.cwd() / output_path).resolve()
    else:
        output_dir = ensure_output_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = output_dir / f"route_chain_connectivity_{args.route_id}_{timestamp}.json"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output_payload, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )

    print_section("FILE SAVED")
    print(str(output_path))


if __name__ == "__main__":
    main()