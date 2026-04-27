from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.db import engine
from app.route_graph_matcher import (
    build_candidates_for_stop,
    build_network_data,
    compute_transition_cost,
    dijkstra_topology_path,
    infer_route_region_codes,
    load_global_station_catalog,
    load_route,
    lock_route_stop_candidates,
    safe_float,
)


def print_section(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def unique_non_empty(values: list[str | None]) -> list[str]:
    result: list[str] = []
    seen = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def build_scope_key(region_codes: list[str]) -> str:
    return "|".join(sorted(unique_non_empty(region_codes)))


def load_nearby_edge_entry_candidates(
    *,
    scope_key: str,
    station_id: int,
    radius_m: int,
    limit: int = 12,
) -> list[dict[str, Any]]:
    query = text(
        """
        WITH target_station AS (
            SELECT geom
            FROM stations
            WHERE id = :station_id
              AND geom IS NOT NULL
            LIMIT 1
        ),
        nearby_edges AS (
            SELECT
                e.source_node_hash,
                e.target_node_hash,
                COALESCE(e.length_km, 0.0) AS length_km,
                ST_Distance(ts.geom::geography, e.geom::geography) AS station_to_edge_m,
                ST_LineLocatePoint(e.geom, ST_ClosestPoint(e.geom, ts.geom)) AS fraction
            FROM target_station ts
            JOIN rail_graph_edges e
              ON e.scope_key = :scope_key
             AND e.geom IS NOT NULL
            WHERE ST_DWithin(ts.geom::geography, e.geom::geography, :radius_m)
            ORDER BY station_to_edge_m, COALESCE(e.length_km, 0.0)
            LIMIT :limit
        )
        SELECT
            ne.source_node_hash,
            ne.target_node_hash,
            ne.length_km,
            ne.station_to_edge_m,
            ne.fraction,
            ns.lon AS source_lon,
            ns.lat AS source_lat,
            nt.lon AS target_lon,
            nt.lat AS target_lat
        FROM nearby_edges ne
        JOIN rail_graph_nodes ns
          ON ns.scope_key = :scope_key
         AND ns.node_hash = ne.source_node_hash
        JOIN rail_graph_nodes nt
          ON nt.scope_key = :scope_key
         AND nt.node_hash = ne.target_node_hash;
        """
    )

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "scope_key": scope_key,
                "station_id": station_id,
                "radius_m": radius_m,
                "limit": limit,
            },
        ).fetchall()

    result: list[dict[str, Any]] = []

    for row in rows:
        item = dict(row._mapping)
        length_km = safe_float(item.get("length_km")) or 0.0
        station_to_edge_km = (safe_float(item.get("station_to_edge_m")) or 0.0) / 1000.0
        fraction = safe_float(item.get("fraction"))
        if fraction is None:
            fraction = 0.5

        source_entry_km = station_to_edge_km + max(0.0, length_km * fraction)
        target_entry_km = station_to_edge_km + max(0.0, length_km * (1.0 - fraction))

        result.append(
            {
                "node_hash": str(item["source_node_hash"]),
                "link_distance_km": source_entry_km,
                "node_lon": float(item["source_lon"]),
                "node_lat": float(item["source_lat"]),
                "is_primary": False,
                "source": f"nearby_edge_{radius_m}m",
                "station_to_edge_m": round(station_to_edge_km * 1000.0, 2),
            }
        )
        result.append(
            {
                "node_hash": str(item["target_node_hash"]),
                "link_distance_km": target_entry_km,
                "node_lon": float(item["target_lon"]),
                "node_lat": float(item["target_lat"]),
                "is_primary": False,
                "source": f"nearby_edge_{radius_m}m",
                "station_to_edge_m": round(station_to_edge_km * 1000.0, 2),
            }
        )

    return result


def normalize_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    for item in options:
        try:
            normalized.append(
                {
                    "node_hash": str(item["node_hash"]),
                    "link_distance_km": float(item["link_distance_km"]),
                    "node_lon": float(item["node_lon"]),
                    "node_lat": float(item["node_lat"]),
                    "is_primary": bool(item.get("is_primary")),
                    "source": item.get("source") or "station_link",
                    "station_to_edge_m": item.get("station_to_edge_m"),
                }
            )
        except Exception:
            continue

    normalized.sort(
        key=lambda x: (
            0 if x["source"] == "station_link" else 1,
            0 if x["is_primary"] else 1,
            x["link_distance_km"],
            x["node_hash"],
        )
    )
    return normalized


def merge_options(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best_by_node: dict[str, dict[str, Any]] = {}

    def rank(item: dict[str, Any]) -> tuple[int, int, float, str]:
        source = item.get("source") or ""
        if source == "station_link":
            source_rank = 0
        elif source.startswith("nearby_edge_"):
            source_rank = 1
        elif source == "local_rescue_node":
            source_rank = 2
        else:
            source_rank = 3

        primary_rank = 0 if item.get("is_primary") else 1
        return (source_rank, primary_rank, float(item["link_distance_km"]), str(item["node_hash"]))

    for group in groups:
        for item in group:
            node_hash = str(item["node_hash"])
            existing = best_by_node.get(node_hash)
            if existing is None or rank(item) < rank(existing):
                best_by_node[node_hash] = item

    merged = list(best_by_node.values())
    merged.sort(key=rank)
    return merged


def build_connected_components(
    adjacency: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, int], dict[int, int]]:
    node_to_component: dict[str, int] = {}
    component_sizes: dict[int, int] = {}
    component_id = 0

    for start_node in adjacency.keys():
        if start_node in node_to_component:
            continue

        component_id += 1
        queue = deque([start_node])
        node_to_component[start_node] = component_id
        size = 0

        while queue:
            node = queue.popleft()
            size += 1

            for edge in adjacency.get(node, []):
                nxt = str(edge["to_node_hash"])
                if nxt in node_to_component:
                    continue
                node_to_component[nxt] = component_id
                queue.append(nxt)

        component_sizes[component_id] = size

    return node_to_component, component_sizes


def component_payload(
    option: dict[str, Any],
    node_to_component: dict[str, int],
    component_sizes: dict[int, int],
) -> dict[str, Any]:
    component_id = node_to_component.get(str(option["node_hash"]))
    return {
        **option,
        "component_id": component_id,
        "component_size": component_sizes.get(component_id),
    }


def evaluate_mode_pairs(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Any,
    current_candidate: Any,
    previous_options: list[dict[str, Any]],
    current_options: list[dict[str, Any]],
    adjacency: dict[str, list[dict[str, Any]]],
    node_coords: dict[str, dict[str, float]],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
) -> dict[str, Any]:
    checked_pairs = 0
    successful_pairs = 0
    rejected_reason_counts: dict[str, int] = defaultdict(int)
    best_pair: dict[str, Any] | None = None
    best_score = float("inf")

    for prev_option in previous_options:
        for curr_option in current_options:
            checked_pairs += 1

            graph_path = dijkstra_topology_path(
                adjacency=adjacency,
                node_coords=node_coords,
                start_node_hash=str(prev_option["node_hash"]),
                end_node_hash=str(curr_option["node_hash"]),
                path_cache=path_cache,
            )
            if graph_path is None:
                rejected_reason_counts["no_graph_path"] += 1
                continue

            render_total_distance_km = (
                float(graph_path["distance_km"])
                + float(prev_option["link_distance_km"])
                + float(curr_option["link_distance_km"])
            )

            transition_cost, transition_diag = compute_transition_cost(
                previous_stop=previous_stop,
                next_stop=current_stop,
                render_total_distance_km=render_total_distance_km,
                hop_count=int(graph_path.get("hop_count") or 0),
            )

            if transition_cost is None:
                rejected_reason = transition_diag.get("rejected_reason") or "unknown"
                rejected_reason_counts[str(rejected_reason)] += 1
                continue

            successful_pairs += 1

            connector_penalty = (
                float(prev_option["link_distance_km"]) + float(curr_option["link_distance_km"])
            ) * 4.0

            source_penalty = 0.0
            if prev_option.get("source") != "station_link":
                source_penalty += 0.8
            elif not prev_option.get("is_primary"):
                source_penalty += 0.15

            if curr_option.get("source") != "station_link":
                source_penalty += 0.8
            elif not curr_option.get("is_primary"):
                source_penalty += 0.15

            final_score = float(transition_cost) + connector_penalty + source_penalty

            if final_score < best_score:
                best_score = final_score
                best_pair = {
                    "from_node_hash": prev_option["node_hash"],
                    "from_source": prev_option.get("source"),
                    "from_entry_km": round(float(prev_option["link_distance_km"]), 4),
                    "to_node_hash": curr_option["node_hash"],
                    "to_source": curr_option.get("source"),
                    "to_entry_km": round(float(curr_option["link_distance_km"]), 4),
                    "graph_distance_km": round(float(graph_path["distance_km"]), 4),
                    "render_total_distance_km": round(render_total_distance_km, 4),
                    "hop_count": int(graph_path.get("hop_count") or 0),
                    "transition_cost": round(float(transition_cost), 4),
                    "connector_penalty": round(connector_penalty, 4),
                    "source_penalty": round(source_penalty, 4),
                    "final_score": round(final_score, 4),
                    "transition_diag": transition_diag,
                }

    return {
        "pairs_checked": checked_pairs,
        "successful_pairs_count": successful_pairs,
        "rejected_reason_counts": dict(rejected_reason_counts),
        "best_pair": best_pair,
    }


def describe_options(
    *,
    title: str,
    options: list[dict[str, Any]],
    node_to_component: dict[str, int],
    component_sizes: dict[int, int],
) -> None:
    print(f"\n{title}: count={len(options)}")
    for item in options[:20]:
        payload = component_payload(item, node_to_component, component_sizes)
        print("  " + json.dumps(payload, ensure_ascii=False))


def analyze_segment(
    *,
    segment_index: int,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Any,
    current_candidate: Any,
    network: dict[str, Any],
    node_to_component: dict[str, int],
    component_sizes: dict[int, int],
) -> dict[str, Any]:
    scope_key = network["stats"]["scope_key"]
    adjacency = network["adjacency"]
    node_coords = network["node_coords"]
    station_links = network.get("station_links") or {}

    prev_direct = normalize_options(station_links.get(previous_candidate.station_id) or [])
    curr_direct = normalize_options(station_links.get(current_candidate.station_id) or [])

    prev_edge_400 = normalize_options(
        load_nearby_edge_entry_candidates(
            scope_key=scope_key,
            station_id=previous_candidate.station_id,
            radius_m=400,
            limit=10,
        )
    )
    curr_edge_400 = normalize_options(
        load_nearby_edge_entry_candidates(
            scope_key=scope_key,
            station_id=current_candidate.station_id,
            radius_m=400,
            limit=10,
        )
    )

    prev_edge_600 = normalize_options(
        load_nearby_edge_entry_candidates(
            scope_key=scope_key,
            station_id=previous_candidate.station_id,
            radius_m=600,
            limit=12,
        )
    )
    curr_edge_600 = normalize_options(
        load_nearby_edge_entry_candidates(
            scope_key=scope_key,
            station_id=current_candidate.station_id,
            radius_m=600,
            limit=12,
        )
    )

    prev_local_rescue = normalize_options(
        [
            {
                "node_hash": node_hash,
                "link_distance_km": distance_km,
                "node_lon": node_coords[node_hash]["lon"],
                "node_lat": node_coords[node_hash]["lat"],
                "is_primary": False,
                "source": "local_rescue_node",
            }
            for node_hash, distance_km in sorted(
                [
                    (
                        node_hash,
                        (
                            (
                                (previous_candidate.lon - coord["lon"]) ** 2
                                + (previous_candidate.lat - coord["lat"]) ** 2
                            )
                            ** 0.5
                        ),
                    )
                    for node_hash, coord in node_coords.items()
                ],
                key=lambda x: x[1],
            )[:10]
        ]
    )

    curr_local_rescue = normalize_options(
        [
            {
                "node_hash": node_hash,
                "link_distance_km": distance_km,
                "node_lon": node_coords[node_hash]["lon"],
                "node_lat": node_coords[node_hash]["lat"],
                "is_primary": False,
                "source": "local_rescue_node",
            }
            for node_hash, distance_km in sorted(
                [
                    (
                        node_hash,
                        (
                            (
                                (current_candidate.lon - coord["lon"]) ** 2
                                + (current_candidate.lat - coord["lat"]) ** 2
                            )
                            ** 0.5
                        ),
                    )
                    for node_hash, coord in node_coords.items()
                ],
                key=lambda x: x[1],
            )[:10]
        ]
    )

    mode_prev = {
        "station_links_only": prev_direct,
        "station_links_plus_nearby_edges_400m": merge_options(prev_direct, prev_edge_400),
        "station_links_plus_nearby_edges_600m": merge_options(prev_direct, prev_edge_600),
        "station_links_plus_local_rescue": merge_options(prev_direct, prev_local_rescue),
    }
    mode_curr = {
        "station_links_only": curr_direct,
        "station_links_plus_nearby_edges_400m": merge_options(curr_direct, curr_edge_400),
        "station_links_plus_nearby_edges_600m": merge_options(curr_direct, curr_edge_600),
        "station_links_plus_local_rescue": merge_options(curr_direct, curr_local_rescue),
    }

    path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    mode_results: dict[str, Any] = {}
    chosen_search_mode = None

    for mode_name in (
        "station_links_only",
        "station_links_plus_nearby_edges_400m",
        "station_links_plus_nearby_edges_600m",
        "station_links_plus_local_rescue",
    ):
        result = evaluate_mode_pairs(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            previous_options=mode_prev[mode_name],
            current_options=mode_curr[mode_name],
            adjacency=adjacency,
            node_coords=node_coords,
            path_cache=path_cache,
        )
        mode_results[mode_name] = result

        if chosen_search_mode is None and result["best_pair"] is not None:
            chosen_search_mode = mode_name

    prev_components = {
        node_to_component.get(str(option["node_hash"]))
        for option in merge_options(prev_direct, prev_edge_400, prev_edge_600)
    }
    curr_components = {
        node_to_component.get(str(option["node_hash"]))
        for option in merge_options(curr_direct, curr_edge_400, curr_edge_600)
    }
    shared_components = sorted(
        comp_id for comp_id in (prev_components & curr_components) if comp_id is not None
    )

    delta_rzd_km = None
    prev_distance = safe_float(previous_stop.get("distance_km"))
    curr_distance = safe_float(current_stop.get("distance_km"))
    if prev_distance is not None and curr_distance is not None:
        delta_rzd_km = round(max(0.0, curr_distance - prev_distance), 4)

    result_payload = {
        "segment_index": segment_index,
        "from_stop_sequence": previous_stop.get("stop_sequence"),
        "to_stop_sequence": current_stop.get("stop_sequence"),
        "from_station_name_raw": previous_stop.get("station_name_raw"),
        "to_station_name_raw": current_stop.get("station_name_raw"),
        "from_station_id": previous_candidate.station_id,
        "from_station_name": previous_candidate.name,
        "to_station_id": current_candidate.station_id,
        "to_station_name": current_candidate.name,
        "delta_rzd_km": delta_rzd_km,
        "chosen_search_mode": chosen_search_mode,
        "path_found": chosen_search_mode is not None,
        "shared_components": shared_components,
        "shared_components_sizes": {
            str(comp_id): component_sizes.get(comp_id) for comp_id in shared_components
        },
        "previous_station_links": [
            component_payload(item, node_to_component, component_sizes) for item in prev_direct
        ],
        "current_station_links": [
            component_payload(item, node_to_component, component_sizes) for item in curr_direct
        ],
        "previous_nearby_edges_400m": [
            component_payload(item, node_to_component, component_sizes) for item in prev_edge_400
        ],
        "current_nearby_edges_400m": [
            component_payload(item, node_to_component, component_sizes) for item in curr_edge_400
        ],
        "previous_nearby_edges_600m": [
            component_payload(item, node_to_component, component_sizes) for item in prev_edge_600
        ],
        "current_nearby_edges_600m": [
            component_payload(item, node_to_component, component_sizes) for item in curr_edge_600
        ],
        "mode_results": mode_results,
    }

    print(
        f"[segment {segment_index}] "
        f"{previous_stop.get('station_name_raw')} -> {current_stop.get('station_name_raw')} "
        f"| chosen_search_mode={chosen_search_mode} | path_found={chosen_search_mode is not None}"
    )
    for mode_name, result in mode_results.items():
        print(
            f"  {mode_name}: pairs_checked={result['pairs_checked']} "
            f"| successful_pairs_count={result['successful_pairs_count']} "
            f"| rejected_reason_counts={result['rejected_reason_counts']}"
        )
        if result["best_pair"] is not None:
            print("    best_pair=" + json.dumps(result["best_pair"], ensure_ascii=False))

    if chosen_search_mode is None:
        print("  shared_components=" + json.dumps(shared_components, ensure_ascii=False))

    return result_payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("route_id", type=int)
    parser.add_argument("--segment-index", type=int, default=None)
    args = parser.parse_args()

    route_id = args.route_id
    only_segment = args.segment_index

    payload = load_route(route_id)
    route = payload["route"]
    stops = payload["stops"]

    catalog_payload = load_global_station_catalog()
    candidates_per_stop = [build_candidates_for_stop(stop, catalog_payload) for stop in stops]
    inferred_region_codes = infer_route_region_codes(stops, candidates_per_stop)
    network = build_network_data(region_codes=inferred_region_codes)
    locked_candidates, lock_logs = lock_route_stop_candidates(stops, candidates_per_stop)

    node_to_component, component_sizes = build_connected_components(network["adjacency"])

    print_section("START")
    print(f"route_id = {route_id}")

    print_section("ROUTE")
    print(f"route_id: {route['id']}")
    print(f"train_number: {route.get('train_number')}")
    print(f"route_name: {route.get('route_name')}")
    print(f"stops_count: {len(stops)}")

    print_section("INFERRED REGIONS")
    print(json_dump({"inferred_region_codes": inferred_region_codes}))

    print_section("NETWORK")
    print(json_dump(network["stats"]))

    print_section("CONNECTED COMPONENTS")
    print(
        json_dump(
            {
                "components_count": len(component_sizes),
                "largest_components": sorted(
                    [
                        {"component_id": comp_id, "size": size}
                        for comp_id, size in component_sizes.items()
                    ],
                    key=lambda x: x["size"],
                    reverse=True,
                )[:20],
            }
        )
    )

    print_section("LOCKED STOPS")
    for item in lock_logs:
        print(
            f"stop_sequence={item['stop_sequence']} | "
            f"raw={item['station_name_raw']} | "
            f"locked_station_id={item['locked_station_id']} | "
            f"locked_station_name={item['locked_station_name']} | "
            f"lock_reason={item['lock_reason']}"
        )

    print_section("ALL STOP CANDIDATES")
    for stop, candidates, locked in zip(stops, candidates_per_stop, locked_candidates):
        print(
            f"stop_sequence={stop.get('stop_sequence')} | "
            f"station_name_raw={stop.get('station_name_raw')} | "
            f"candidate_count={len(candidates)} | "
            f"locked_station_id={locked.station_id if locked else None}"
        )
        for candidate in candidates:
            prefix = "* " if locked and candidate.station_id == locked.station_id else "- "
            print(
                "  "
                + prefix
                + json.dumps(
                    {
                        "station_id": candidate.station_id,
                        "station_name": candidate.name,
                        "region_code": candidate.region_code,
                        "effective_score": round(candidate.effective_score, 4),
                        "name_score": round(candidate.name_score, 4),
                        "code_match": candidate.code_match,
                        "anchor": candidate.anchor,
                        "is_main_rail_station": candidate.is_main_rail_station,
                        "match_method": candidate.match_method,
                        "match_reason": candidate.match_reason,
                        "locked": bool(locked and candidate.station_id == locked.station_id),
                    },
                    ensure_ascii=False,
                )
            )

    print_section("SEGMENT DEBUG")
    segment_results: list[dict[str, Any]] = []

    for idx in range(1, len(stops)):
        if only_segment is not None and idx != only_segment:
            continue

        previous_stop = stops[idx - 1]
        current_stop = stops[idx]
        previous_candidate = locked_candidates[idx - 1]
        current_candidate = locked_candidates[idx]

        if previous_candidate is None or current_candidate is None:
            continue

        segment_result = analyze_segment(
            segment_index=idx,
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            node_to_component=node_to_component,
            component_sizes=component_sizes,
        )
        segment_results.append(segment_result)

    output_dir = ROOT_DIR / "debug_output"
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"route_graph_components_{route_id}_{timestamp}.json"

    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "route": route,
                "inferred_region_codes": inferred_region_codes,
                "network": network["stats"],
                "components_count": len(component_sizes),
                "largest_components": sorted(
                    [
                        {"component_id": comp_id, "size": size}
                        for comp_id, size in component_sizes.items()
                    ],
                    key=lambda x: x["size"],
                    reverse=True,
                )[:50],
                "locked_stops": lock_logs,
                "segment_results": segment_results,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )

    print_section("FILE SAVED")
    print(str(output_path))


if __name__ == "__main__":
    main()