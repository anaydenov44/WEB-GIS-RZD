import argparse
import json
from collections import deque

from app.route_graph_matcher import (
    build_candidates_for_stop,
    build_network_data,
    get_local_rescue_node_options_for_candidate,
    get_station_link_options_for_candidate,
    infer_route_region_codes,
    load_global_station_catalog,
    load_route,
    lock_route_stop_candidates,
    merge_link_options,
    _evaluate_topology_link_pair_options,
)


def build_component_index(adjacency: dict[str, list[dict]]) -> tuple[dict[str, int], dict[int, int]]:
    component_by_node: dict[str, int] = {}
    component_sizes: dict[int, int] = {}
    component_id = 0

    for node_hash in adjacency.keys():
        if node_hash in component_by_node:
            continue

        component_id += 1
        queue = deque([node_hash])
        component_by_node[node_hash] = component_id
        size = 0

        while queue:
            current = queue.popleft()
            size += 1

            for edge in adjacency.get(current, []):
                nxt = str(edge["to_node_hash"])
                if nxt in component_by_node:
                    continue
                component_by_node[nxt] = component_id
                queue.append(nxt)

        component_sizes[component_id] = size

    return component_by_node, component_sizes


def serialize_link_options(
    options: list[dict],
    component_by_node: dict[str, int],
    component_sizes: dict[int, int],
) -> list[dict]:
    result = []

    for item in options:
        node_hash = str(item["node_hash"])
        component_id = component_by_node.get(node_hash)

        result.append(
            {
                "node_hash": node_hash,
                "source": item.get("source"),
                "link_distance_km": round(float(item.get("link_distance_km") or 0.0), 4),
                "is_primary": bool(item.get("is_primary")),
                "node_lon": float(item["node_lon"]),
                "node_lat": float(item["node_lat"]),
                "component_id": component_id,
                "component_size": component_sizes.get(component_id),
            }
        )

    return result


def serialize_best_result(best_result: dict | None) -> dict | None:
    if best_result is None:
        return None

    start_link = best_result.get("start_link") or {}
    end_link = best_result.get("end_link") or {}
    diag = best_result.get("transition_diag") or {}

    return {
        "search_mode": best_result.get("search_mode"),
        "from_node_hash": start_link.get("node_hash"),
        "from_source": start_link.get("source"),
        "from_entry_km": round(float(best_result.get("connector_start_km") or 0.0), 4),
        "to_node_hash": end_link.get("node_hash"),
        "to_source": end_link.get("source"),
        "to_entry_km": round(float(best_result.get("connector_end_km") or 0.0), 4),
        "graph_distance_km": round(float(best_result.get("graph_distance_km") or 0.0), 4),
        "render_total_distance_km": round(float(best_result.get("total_score_km") or 0.0), 4),
        "hop_count": int(best_result.get("graph_edge_count") or 0),
        "final_score": round(float(best_result.get("final_score") or 0.0), 4),
        "transition_diag": diag,
    }


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("route_id", type=int)
    parser.add_argument("--segment", type=int, required=True, help="segment index, например 12 для ПРОТОКА -> АНАПА")
    args = parser.parse_args()

    route_id = args.route_id
    segment_index = args.segment

    payload = load_route(route_id)
    route = payload["route"]
    stops = payload["stops"]

    if segment_index < 1 or segment_index >= len(stops):
        raise ValueError("segment index out of range")

    catalog_payload = load_global_station_catalog()
    candidates_per_stop = [build_candidates_for_stop(stop, catalog_payload) for stop in stops]
    inferred_region_codes = infer_route_region_codes(stops, candidates_per_stop)
    network = build_network_data(region_codes=inferred_region_codes)
    locked_candidates, lock_logs = lock_route_stop_candidates(stops, candidates_per_stop)

    adjacency = network.get("adjacency") or {}
    if not adjacency:
        raise RuntimeError("Topology graph is empty")

    component_by_node, component_sizes = build_component_index(adjacency)

    previous_stop = stops[segment_index - 1]
    current_stop = stops[segment_index]
    previous_candidate = locked_candidates[segment_index - 1]
    current_candidate = locked_candidates[segment_index]

    if previous_candidate is None or current_candidate is None:
        raise RuntimeError("One of locked candidates is missing")

    fallback_node_cache: dict[int, list[dict]] = {}
    rescue_node_cache: dict[int, list[dict]] = {}
    path_cache: dict[tuple[str, str], dict | None] = {}

    base_start_links = get_station_link_options_for_candidate(
        previous_candidate,
        network,
        fallback_node_cache,
    )
    base_end_links = get_station_link_options_for_candidate(
        current_candidate,
        network,
        fallback_node_cache,
    )

    rescue_start_links = merge_link_options(
        base_start_links,
        get_local_rescue_node_options_for_candidate(
            previous_candidate,
            network,
            rescue_node_cache,
        ),
    )
    rescue_end_links = merge_link_options(
        base_end_links,
        get_local_rescue_node_options_for_candidate(
            current_candidate,
            network,
            rescue_node_cache,
        ),
    )

    station_links_only_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=base_start_links,
        end_links=base_end_links,
        adjacency=adjacency,
        node_coords=network["node_coords"],
        path_cache=path_cache,
        search_mode="station_links_only",
    )

    local_rescue_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=rescue_start_links,
        end_links=rescue_end_links,
        adjacency=adjacency,
        node_coords=network["node_coords"],
        path_cache=path_cache,
        search_mode="station_links_plus_local_rescue",
    )

    base_start_components = sorted(
        {component_by_node.get(str(item["node_hash"])) for item in base_start_links if component_by_node.get(str(item["node_hash"])) is not None}
    )
    base_end_components = sorted(
        {component_by_node.get(str(item["node_hash"])) for item in base_end_links if component_by_node.get(str(item["node_hash"])) is not None}
    )
    rescue_start_components = sorted(
        {component_by_node.get(str(item["node_hash"])) for item in rescue_start_links if component_by_node.get(str(item["node_hash"])) is not None}
    )
    rescue_end_components = sorted(
        {component_by_node.get(str(item["node_hash"])) for item in rescue_end_links if component_by_node.get(str(item["node_hash"])) is not None}
    )

    shared_base_components = sorted(set(base_start_components) & set(base_end_components))
    shared_rescue_components = sorted(set(rescue_start_components) & set(rescue_end_components))

    print_section("START")
    print(f"route_id = {route_id}")
    print(f"segment_index = {segment_index}")

    print_section("ROUTE")
    print(json.dumps(
        {
            "route_id": route["id"],
            "train_number": route.get("train_number"),
            "route_name": route.get("route_name"),
            "from_stop_sequence": previous_stop.get("stop_sequence"),
            "to_stop_sequence": current_stop.get("stop_sequence"),
            "from_station_name_raw": previous_stop.get("station_name_raw"),
            "to_station_name_raw": current_stop.get("station_name_raw"),
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))

    print_section("LOCKED STATIONS")
    print(json.dumps(
        {
            "from_station": {
                "station_id": previous_candidate.station_id,
                "station_name": previous_candidate.name,
                "region_code": previous_candidate.region_code,
            },
            "to_station": {
                "station_id": current_candidate.station_id,
                "station_name": current_candidate.name,
                "region_code": current_candidate.region_code,
            },
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))

    print_section("BASE LINK OPTIONS")
    print(json.dumps(
        {
            "from_options": serialize_link_options(base_start_links, component_by_node, component_sizes),
            "to_options": serialize_link_options(base_end_links, component_by_node, component_sizes),
            "shared_components": shared_base_components,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))

    print_section("LOCAL RESCUE OPTIONS")
    print(json.dumps(
        {
            "from_options": serialize_link_options(rescue_start_links, component_by_node, component_sizes),
            "to_options": serialize_link_options(rescue_end_links, component_by_node, component_sizes),
            "shared_components": shared_rescue_components,
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))

    print_section("RESULTS")
    print(json.dumps(
        {
            "station_links_only_best": serialize_best_result(station_links_only_result),
            "local_rescue_best": serialize_best_result(local_rescue_result),
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))

    output = {
        "route": route,
        "segment_index": segment_index,
        "from_stop": previous_stop,
        "to_stop": current_stop,
        "locked_from_candidate": {
            "station_id": previous_candidate.station_id,
            "station_name": previous_candidate.name,
            "region_code": previous_candidate.region_code,
        },
        "locked_to_candidate": {
            "station_id": current_candidate.station_id,
            "station_name": current_candidate.name,
            "region_code": current_candidate.region_code,
        },
        "base_link_options": {
            "from_options": serialize_link_options(base_start_links, component_by_node, component_sizes),
            "to_options": serialize_link_options(base_end_links, component_by_node, component_sizes),
            "shared_components": shared_base_components,
        },
        "local_rescue_options": {
            "from_options": serialize_link_options(rescue_start_links, component_by_node, component_sizes),
            "to_options": serialize_link_options(rescue_end_links, component_by_node, component_sizes),
            "shared_components": shared_rescue_components,
        },
        "results": {
            "station_links_only_best": serialize_best_result(station_links_only_result),
            "local_rescue_best": serialize_best_result(local_rescue_result),
        },
    }

    output_path = f"debug_output/route_gap_inspector_{route_id}_segment_{segment_index}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print_section("FILE SAVED")
    print(output_path)


if __name__ == "__main__":
    main()