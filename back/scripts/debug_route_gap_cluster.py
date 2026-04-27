from __future__ import annotations

import argparse
import importlib
import json
import math
import pkgutil
from collections import Counter
from typing import Any


def import_route_matcher_module():
    import app

    required_attrs = [
        "load_route",
        "load_global_station_catalog",
        "build_candidates_for_stop",
        "infer_route_region_codes",
        "build_network_data",
        "lock_route_stop_candidates",
        "build_topology_path_between_candidates",
        "get_station_link_options_for_candidate",
        "get_nearby_edge_link_options_for_candidate",
        "get_local_rescue_node_options_for_candidate",
        "merge_link_options",
        "annotate_link_options_with_components",
        "dijkstra_topology_path",
        "compute_transition_cost",
    ]

    tried_modules: list[str] = []

    for module_info in pkgutil.walk_packages(app.__path__, prefix="app."):
        module_name = module_info.name

        if any(skip in module_name for skip in [
            ".tests",
            ".test",
            ".migrations",
            ".alembic",
            ".venv",
            "__pycache__",
        ]):
            continue

        tried_modules.append(module_name)

        try:
            module = importlib.import_module(module_name)
        except Exception:
            continue

        if all(hasattr(module, attr) for attr in required_attrs):
            return module

    raise ImportError(
        "Не удалось автоматически найти модуль route_matcher.\n"
        f"Проверено модулей app.*: {len(tried_modules)}"
    )


rm = import_route_matcher_module()


def print_header(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(1e-12, 1.0 - a)))
    return radius_km * c


def compute_delta_rzd(previous_stop: dict[str, Any], current_stop: dict[str, Any]) -> float | None:
    prev_distance = safe_float(previous_stop.get("distance_km"))
    curr_distance = safe_float(current_stop.get("distance_km"))
    if prev_distance is None or curr_distance is None:
        return None
    return max(0.0, curr_distance - prev_distance)


def compute_source_penalty(link: dict[str, Any]) -> float:
    source = str(link.get("source") or "")

    if source == "local_rescue_node":
        return float(getattr(rm, "LOCAL_RESCUE_EXTRA_PENALTY", 1.25))
    if source.startswith("edge_") or source.startswith("nearby_edge_"):
        return 0.45
    if source != "station_link":
        return 0.8
    if not bool(link.get("is_primary")):
        return 0.15
    return 0.0


def build_mode_evaluation(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Any,
    current_candidate: Any,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    start_links: list[dict[str, Any]],
    end_links: list[dict[str, Any]],
    mode_name: str,
) -> dict[str, Any]:
    adjacency = network["adjacency"]
    node_coords = network["node_coords"]

    annotated_start_links = rm.annotate_link_options_with_components(start_links, network)
    annotated_end_links = rm.annotate_link_options_with_components(end_links, network)

    seen_pairs: set[tuple[str, str]] = set()
    successful_pairs: list[dict[str, Any]] = []
    rejected_pairs: list[dict[str, Any]] = []
    rejected_reason_counts: Counter[str] = Counter()

    raw_best_pair: dict[str, Any] | None = None
    raw_best_distance = math.inf

    for start_link in annotated_start_links:
        for end_link in annotated_end_links:
            pair_key = (str(start_link["node_hash"]), str(end_link["node_hash"]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            graph_path = rm.dijkstra_topology_path(
                adjacency=adjacency,
                node_coords=node_coords,
                start_node_hash=str(start_link["node_hash"]),
                end_node_hash=str(end_link["node_hash"]),
                path_cache=path_cache,
            )

            if graph_path is None:
                rejected_reason_counts["no_graph_path"] += 1
                rejected_pairs.append(
                    {
                        "from_node_hash": start_link["node_hash"],
                        "from_source": start_link.get("source"),
                        "from_entry_km": round(float(start_link["link_distance_km"]), 4),
                        "from_component_id": start_link.get("component_id"),
                        "from_component_size": start_link.get("component_size"),
                        "to_node_hash": end_link["node_hash"],
                        "to_source": end_link.get("source"),
                        "to_entry_km": round(float(end_link["link_distance_km"]), 4),
                        "to_component_id": end_link.get("component_id"),
                        "to_component_size": end_link.get("component_size"),
                        "rejected_reason": "no_graph_path",
                    }
                )
                continue

            render_total_distance_km = (
                float(graph_path["distance_km"])
                + float(start_link["link_distance_km"])
                + float(end_link["link_distance_km"])
            )

            if render_total_distance_km < raw_best_distance:
                raw_best_distance = render_total_distance_km
                raw_best_pair = {
                    "from_node_hash": start_link["node_hash"],
                    "from_source": start_link.get("source"),
                    "from_entry_km": round(float(start_link["link_distance_km"]), 4),
                    "from_component_id": start_link.get("component_id"),
                    "from_component_size": start_link.get("component_size"),
                    "to_node_hash": end_link["node_hash"],
                    "to_source": end_link.get("source"),
                    "to_entry_km": round(float(end_link["link_distance_km"]), 4),
                    "to_component_id": end_link.get("component_id"),
                    "to_component_size": end_link.get("component_size"),
                    "graph_distance_km": round(float(graph_path["distance_km"]), 4),
                    "render_total_distance_km": round(render_total_distance_km, 4),
                    "hop_count": int(graph_path.get("hop_count") or 0),
                }

            transition_cost, transition_diag = rm.compute_transition_cost(
                previous_stop=previous_stop,
                next_stop=current_stop,
                render_total_distance_km=render_total_distance_km,
                hop_count=int(graph_path.get("hop_count") or 0),
            )

            connector_penalty = (
                float(start_link["link_distance_km"])
                + float(end_link["link_distance_km"])
            ) * 4.0

            source_penalty = compute_source_penalty(start_link) + compute_source_penalty(end_link)

            if transition_cost is None:
                rejected_reason = str(
                    (transition_diag or {}).get("rejected_reason") or "rejected_without_reason"
                )
                rejected_reason_counts[rejected_reason] += 1
                rejected_pairs.append(
                    {
                        "from_node_hash": start_link["node_hash"],
                        "from_source": start_link.get("source"),
                        "from_entry_km": round(float(start_link["link_distance_km"]), 4),
                        "from_component_id": start_link.get("component_id"),
                        "from_component_size": start_link.get("component_size"),
                        "to_node_hash": end_link["node_hash"],
                        "to_source": end_link.get("source"),
                        "to_entry_km": round(float(end_link["link_distance_km"]), 4),
                        "to_component_id": end_link.get("component_id"),
                        "to_component_size": end_link.get("component_size"),
                        "graph_distance_km": round(float(graph_path["distance_km"]), 4),
                        "render_total_distance_km": round(render_total_distance_km, 4),
                        "hop_count": int(graph_path.get("hop_count") or 0),
                        "transition_cost": None,
                        "connector_penalty": round(connector_penalty, 4),
                        "source_penalty": round(source_penalty, 4),
                        "final_score": None,
                        "rejected_reason": rejected_reason,
                        "transition_diag": transition_diag,
                    }
                )
                continue

            final_score = float(transition_cost) + connector_penalty + source_penalty

            successful_pairs.append(
                {
                    "from_node_hash": start_link["node_hash"],
                    "from_source": start_link.get("source"),
                    "from_entry_km": round(float(start_link["link_distance_km"]), 4),
                    "from_component_id": start_link.get("component_id"),
                    "from_component_size": start_link.get("component_size"),
                    "to_node_hash": end_link["node_hash"],
                    "to_source": end_link.get("source"),
                    "to_entry_km": round(float(end_link["link_distance_km"]), 4),
                    "to_component_id": end_link.get("component_id"),
                    "to_component_size": end_link.get("component_size"),
                    "graph_distance_km": round(float(graph_path["distance_km"]), 4),
                    "render_total_distance_km": round(render_total_distance_km, 4),
                    "hop_count": int(graph_path.get("hop_count") or 0),
                    "transition_cost": round(float(transition_cost), 4),
                    "connector_penalty": round(connector_penalty, 4),
                    "source_penalty": round(source_penalty, 4),
                    "final_score": round(final_score, 4),
                    "transition_diag": transition_diag,
                }
            )

    best_success = None
    if successful_pairs:
        best_success = min(
            successful_pairs,
            key=lambda item: (
                float(item["final_score"]),
                float(item["render_total_distance_km"]),
                int(item["hop_count"]),
            ),
        )

    best_rejected = None
    if rejected_pairs:
        def rejected_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
            diag = item.get("transition_diag") or {}
            relative_error = diag.get("relative_error")
            if relative_error is None:
                relative_error = 999999.0
            graph_distance_km = item.get("render_total_distance_km")
            if graph_distance_km is None:
                graph_distance_km = 999999.0
            return (
                float(relative_error),
                float(graph_distance_km),
                int(item.get("hop_count") or 999999),
            )

        best_rejected = min(rejected_pairs, key=rejected_sort_key)

    return {
        "mode_name": mode_name,
        "pairs_checked": len(seen_pairs),
        "successful_pairs_count": len(successful_pairs),
        "rejected_reason_counts": dict(rejected_reason_counts),
        "best_success": best_success,
        "best_rejected": best_rejected,
        "raw_best_pair": raw_best_pair,
    }


def build_segment_probe(
    *,
    segment_index: int,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Any,
    current_candidate: Any,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    delta_rzd_km = compute_delta_rzd(previous_stop, current_stop)
    geo_distance_km = haversine_km(
        float(previous_candidate.lon),
        float(previous_candidate.lat),
        float(current_candidate.lon),
        float(current_candidate.lat),
    )

    base_start_links = rm.get_station_link_options_for_candidate(
        previous_candidate,
        network,
        fallback_node_cache,
    )
    base_end_links = rm.get_station_link_options_for_candidate(
        current_candidate,
        network,
        fallback_node_cache,
    )

    nearby_400_start = rm.merge_link_options(
        base_start_links,
        rm.get_nearby_edge_link_options_for_candidate(
            previous_candidate,
            network,
            radius_m=400,
            fallback_node_cache=fallback_node_cache,
        ),
    )
    nearby_400_end = rm.merge_link_options(
        base_end_links,
        rm.get_nearby_edge_link_options_for_candidate(
            current_candidate,
            network,
            radius_m=400,
            fallback_node_cache=fallback_node_cache,
        ),
    )

    nearby_600_start = rm.merge_link_options(
        nearby_400_start,
        rm.get_nearby_edge_link_options_for_candidate(
            previous_candidate,
            network,
            radius_m=600,
            fallback_node_cache=fallback_node_cache,
        ),
    )
    nearby_600_end = rm.merge_link_options(
        nearby_400_end,
        rm.get_nearby_edge_link_options_for_candidate(
            current_candidate,
            network,
            radius_m=600,
            fallback_node_cache=fallback_node_cache,
        ),
    )

    rescue_start_links = rm.merge_link_options(
        nearby_600_start,
        rm.get_local_rescue_node_options_for_candidate(
            previous_candidate,
            network,
            rescue_node_cache,
        ),
    )
    rescue_end_links = rm.merge_link_options(
        nearby_600_end,
        rm.get_local_rescue_node_options_for_candidate(
            current_candidate,
            network,
            rescue_node_cache,
        ),
    )

    mode_results = [
        build_mode_evaluation(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            start_links=base_start_links,
            end_links=base_end_links,
            mode_name="station_links_only",
        ),
        build_mode_evaluation(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            start_links=nearby_400_start,
            end_links=nearby_400_end,
            mode_name="station_links_plus_nearby_edges_400m",
        ),
        build_mode_evaluation(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            start_links=nearby_600_start,
            end_links=nearby_600_end,
            mode_name="station_links_plus_nearby_edges_600m",
        ),
        build_mode_evaluation(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            start_links=rescue_start_links,
            end_links=rescue_end_links,
            mode_name="station_links_plus_local_rescue",
        ),
    ]

    chosen = rm.build_topology_path_between_candidates(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        network=network,
        path_cache=path_cache,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
    )

    raw_best_overall = None
    raw_best_distance = math.inf
    aggregated_rejected_reasons: Counter[str] = Counter()

    for mode_result in mode_results:
        aggregated_rejected_reasons.update(mode_result["rejected_reason_counts"])
        raw_best_pair = mode_result.get("raw_best_pair")
        if raw_best_pair is None:
            continue
        dist = float(raw_best_pair["render_total_distance_km"])
        if dist < raw_best_distance:
            raw_best_distance = dist
            raw_best_overall = {
                **raw_best_pair,
                "mode_name": mode_result["mode_name"],
            }

    ratio_graph_to_rzd = None
    if delta_rzd_km is not None and raw_best_overall is not None and delta_rzd_km > 0:
        ratio_graph_to_rzd = raw_best_overall["render_total_distance_km"] / delta_rzd_km

    return {
        "segment_index": segment_index,
        "from_stop_sequence": previous_stop.get("stop_sequence"),
        "to_stop_sequence": current_stop.get("stop_sequence"),
        "from_station_name_raw": previous_stop.get("station_name_raw"),
        "to_station_name_raw": current_stop.get("station_name_raw"),
        "from_station_id": previous_candidate.station_id,
        "to_station_id": current_candidate.station_id,
        "from_station_name": previous_candidate.name,
        "to_station_name": current_candidate.name,
        "delta_rzd_km": round(delta_rzd_km, 4) if delta_rzd_km is not None else None,
        "geo_distance_km": round(geo_distance_km, 4),
        "ratio_geo_to_rzd": (
            round(geo_distance_km / delta_rzd_km, 4)
            if delta_rzd_km not in (None, 0)
            else None
        ),
        "chosen_search_mode": chosen.get("search_mode") if chosen else None,
        "path_found": chosen is not None,
        "raw_best_overall": raw_best_overall,
        "ratio_graph_to_rzd": round(ratio_graph_to_rzd, 4) if ratio_graph_to_rzd is not None else None,
        "aggregated_rejected_reasons": dict(aggregated_rejected_reasons),
        "mode_results": mode_results,
    }


def print_segment_probe(probe: dict[str, Any]) -> None:
    idx = probe["segment_index"]
    print(
        f"[segment {idx}] "
        f'{probe["from_station_name_raw"]} -> {probe["to_station_name_raw"]} | '
        f'path_found={probe["path_found"]} | '
        f'chosen_search_mode={probe["chosen_search_mode"]}'
    )
    print(
        f'  delta_rzd_km={probe["delta_rzd_km"]} | '
        f'geo_distance_km={probe["geo_distance_km"]} | '
        f'ratio_geo_to_rzd={probe["ratio_geo_to_rzd"]} | '
        f'ratio_graph_to_rzd={probe["ratio_graph_to_rzd"]}'
    )

    raw_best = probe.get("raw_best_overall")
    if raw_best:
        print(
            "  raw_best_overall="
            + json.dumps(raw_best, ensure_ascii=False)
        )

    print(
        "  aggregated_rejected_reasons="
        + json.dumps(probe["aggregated_rejected_reasons"], ensure_ascii=False)
    )

    for mode_result in probe["mode_results"]:
        print(
            f'    {mode_result["mode_name"]}: '
            f'pairs_checked={mode_result["pairs_checked"]} | '
            f'successful_pairs_count={mode_result["successful_pairs_count"]} | '
            f'rejected_reason_counts={mode_result["rejected_reason_counts"]}'
        )
        if mode_result.get("best_success"):
            print(
                "      best_success="
                + json.dumps(mode_result["best_success"], ensure_ascii=False)
            )
        elif mode_result.get("best_rejected"):
            print(
                "      best_rejected="
                + json.dumps(mode_result["best_rejected"], ensure_ascii=False)
            )


def build_cluster_summary(probes: list[dict[str, Any]]) -> dict[str, Any]:
    total_segments = len(probes)
    failed_segments = [item for item in probes if not item["path_found"]]
    graph_long_segments = [
        item for item in probes
        if item.get("ratio_graph_to_rzd") is not None and item["ratio_graph_to_rzd"] >= 1.5
    ]
    geo_ok_but_graph_bad_segments = [
        item for item in probes
        if item.get("ratio_graph_to_rzd") is not None
        and item["ratio_graph_to_rzd"] >= 1.5
        and item.get("ratio_geo_to_rzd") is not None
        and item["ratio_geo_to_rzd"] <= 1.15
    ]

    rejected_reason_counts: Counter[str] = Counter()
    for probe in probes:
        rejected_reason_counts.update(probe.get("aggregated_rejected_reasons") or {})

    worst_segments = sorted(
        [
            item for item in probes
            if item.get("ratio_graph_to_rzd") is not None
        ],
        key=lambda item: item["ratio_graph_to_rzd"],
        reverse=True,
    )[:10]

    return {
        "total_segments": total_segments,
        "failed_segments_count": len(failed_segments),
        "graph_long_segments_count": len(graph_long_segments),
        "geo_ok_but_graph_bad_segments_count": len(geo_ok_but_graph_bad_segments),
        "rejected_reason_counts": dict(rejected_reason_counts),
        "failed_segments": [
            {
                "segment_index": item["segment_index"],
                "from_station_name_raw": item["from_station_name_raw"],
                "to_station_name_raw": item["to_station_name_raw"],
                "delta_rzd_km": item["delta_rzd_km"],
                "geo_distance_km": item["geo_distance_km"],
                "ratio_geo_to_rzd": item["ratio_geo_to_rzd"],
                "ratio_graph_to_rzd": item["ratio_graph_to_rzd"],
                "raw_best_overall": item["raw_best_overall"],
                "aggregated_rejected_reasons": item["aggregated_rejected_reasons"],
            }
            for item in failed_segments
        ],
        "worst_ratio_segments": [
            {
                "segment_index": item["segment_index"],
                "from_station_name_raw": item["from_station_name_raw"],
                "to_station_name_raw": item["to_station_name_raw"],
                "delta_rzd_km": item["delta_rzd_km"],
                "geo_distance_km": item["geo_distance_km"],
                "ratio_geo_to_rzd": item["ratio_geo_to_rzd"],
                "ratio_graph_to_rzd": item["ratio_graph_to_rzd"],
                "raw_best_overall": item["raw_best_overall"],
                "path_found": item["path_found"],
            }
            for item in worst_segments
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Кластерный дебаг проблем расстояний между станциями на маршруте."
    )
    parser.add_argument("route_id", type=int, help="ID маршрута")
    parser.add_argument(
        "--only-problematic",
        action="store_true",
        help="Печатать только проблемные сегменты",
    )
    args = parser.parse_args()

    print_header("START")
    print(f"route_id = {args.route_id}")
    print(f"route_matcher_module = {rm.__name__}")

    payload = rm.load_route(args.route_id)
    route = payload["route"]
    stops = payload["stops"]

    print_header("ROUTE")
    print(f'route_id: {route["id"]}')
    print(f'train_number: {route.get("train_number")}')
    print(f'route_name: {route.get("route_name")}')
    print(f"stops_count: {len(stops)}")

    catalog_payload = rm.load_global_station_catalog()
    candidates_per_stop = [
        rm.build_candidates_for_stop(stop, catalog_payload)
        for stop in stops
    ]

    inferred_region_codes = rm.infer_route_region_codes(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
    )

    print_header("INFERRED REGIONS")
    print_json({"inferred_region_codes": inferred_region_codes})

    network = rm.build_network_data(region_codes=inferred_region_codes)

    print_header("NETWORK")
    print_json(network.get("stats") or {})

    locked_candidates, _ = rm.lock_route_stop_candidates(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
    )

    print_header("LOCKED STOPS")
    for stop, candidate in zip(stops, locked_candidates):
        if candidate is None:
            print(
                f'stop_sequence={stop.get("stop_sequence")} | '
                f'raw={stop.get("station_name_raw")} | locked_station_id=None'
            )
            continue

        print(
            f'stop_sequence={stop.get("stop_sequence")} | '
            f'raw={stop.get("station_name_raw")} | '
            f'locked_station_id={candidate.station_id} | '
            f'locked_station_name={candidate.name}'
        )

    path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    fallback_node_cache: dict[int, list[dict[str, Any]]] = {}
    rescue_node_cache: dict[int, list[dict[str, Any]]] = {}

    probes: list[dict[str, Any]] = []

    print_header("SEGMENT CLUSTER DEBUG")

    for index in range(1, len(stops)):
        previous_stop = stops[index - 1]
        current_stop = stops[index]
        previous_candidate = locked_candidates[index - 1]
        current_candidate = locked_candidates[index]

        if previous_candidate is None or current_candidate is None:
            continue

        probe = build_segment_probe(
            segment_index=index,
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
        )
        probes.append(probe)

        is_problematic = (
            not probe["path_found"]
            or (
                probe.get("ratio_graph_to_rzd") is not None
                and probe["ratio_graph_to_rzd"] >= 1.5
            )
        )

        if not args.only_problematic or is_problematic:
            print_segment_probe(probe)

    summary = build_cluster_summary(probes)

    print_header("CLUSTER SUMMARY")
    print_json(summary)


if __name__ == "__main__":
    main()