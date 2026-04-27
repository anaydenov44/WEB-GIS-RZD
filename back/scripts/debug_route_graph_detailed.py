import argparse
import importlib
import json
import pkgutil
from datetime import date, datetime
from pathlib import Path
from typing import Any
import heapq
import math
from collections import defaultdict
import app


def _module_has_required_api(module: Any) -> bool:
    required_attrs = (
        "load_route",
        "load_global_station_catalog",
        "build_candidates_for_stop",
        "infer_route_region_codes",
        "build_network_data",
        "lock_route_stop_candidates",
        "get_station_link_options_for_candidate",
        "get_local_rescue_node_options_for_candidate",
        "merge_link_options",
        "dijkstra_topology_path",
        "compute_transition_cost",
        "try_isolated_component_bridge_rescue",
        "build_connected_components_cache",
        "compute_name_similarity",
    )
    return all(hasattr(module, attr) for attr in required_attrs)


def _import_route_matcher():
    explicit_candidates = [
        "app.route_matcher",
        "app.services.route_matcher",
        "app.services.routes.route_matcher",
        "app.core.route_matcher",
        "app.matching.route_matcher",
        "app.map.route_matcher",
        "app.routes.route_matcher",
        "app.utils.route_matcher",
    ]

    checked: list[str] = []

    for module_name in explicit_candidates:
        try:
            module = importlib.import_module(module_name)
            checked.append(module_name)
            if _module_has_required_api(module):
                return module
        except Exception:
            checked.append(module_name)
            continue

    for module_info in pkgutil.walk_packages(app.__path__, prefix="app."):
        module_name = module_info.name

        lowered = module_name.lower()
        if "route" not in lowered and "match" not in lowered:
            continue

        if module_name in checked:
            continue

        try:
            module = importlib.import_module(module_name)
            if _module_has_required_api(module):
                return module
        except Exception:
            continue

    raise ImportError(
        "Не удалось автоматически найти модуль route_matcher внутри app. "
        "Проверь, где лежит твой основной мэтчер, и пропиши точный import вручную."
    )


rm = _import_route_matcher()


def safe_json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def print_section(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def dump_pretty(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        default=safe_json_default,
    )


def round_opt(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except Exception:
        return None


def serialize_candidate(
    stop: dict[str, Any],
    candidate: Any,
    *,
    locked_station_id: int | None = None,
) -> dict[str, Any]:
    return {
        "station_id": candidate.station_id,
        "station_name": candidate.name,
        "region_code": candidate.region_code,
        "effective_score": round_opt(candidate.effective_score),
        "name_score": round_opt(candidate.name_score),
        "name_similarity_to_raw": round_opt(
            rm.compute_name_similarity(stop.get("station_name_raw"), candidate.name)
        ),
        "code_match": bool(candidate.code_match),
        "anchor": bool(candidate.anchor),
        "is_main_rail_station": bool(candidate.is_main_rail_station),
        "match_method": candidate.match_method,
        "match_reason": candidate.match_reason,
        "locked": candidate.station_id == locked_station_id,
    }


def serialize_locked_stop(
    stop: dict[str, Any],
    locked_candidate: Any | None,
) -> dict[str, Any]:
    return {
        "stop_sequence": stop.get("stop_sequence"),
        "station_name_raw": stop.get("station_name_raw"),
        "station_code_rzd": stop.get("station_code_rzd"),
        "stored_station_id": stop.get("stored_station_id"),
        "locked_station_id": locked_candidate.station_id if locked_candidate else None,
        "locked_station_name": locked_candidate.name if locked_candidate else None,
        "locked_station_region_code": locked_candidate.region_code if locked_candidate else None,
        "locked_match_method": locked_candidate.match_method if locked_candidate else None,
        "locked_score": round_opt(locked_candidate.effective_score) if locked_candidate else None,
    }


def _debug_merge_coordinate_sequences(
    sequences: list[list[list[float]]],
) -> list[list[float]]:
    merged: list[list[float]] = []

    for sequence in sequences:
        if not sequence:
            continue

        if not merged:
            merged.extend(sequence)
            continue

        if merged[-1] == sequence[0]:
            merged.extend(sequence[1:])
        else:
            merged.extend(sequence)

    return merged


def _debug_annotate_link_options_with_components(
    options: list[dict[str, Any]],
    network: dict[str, Any],
) -> list[dict[str, Any]]:
    components_cache = rm.build_connected_components_cache(network)
    component_id_by_node = components_cache["component_id_by_node"]
    component_sizes = components_cache["component_sizes"]

    result: list[dict[str, Any]] = []

    for item in options:
        node_hash = str(item["node_hash"])
        component_id = component_id_by_node.get(node_hash)

        result.append(
            {
                **item,
                "component_id": component_id,
                "component_size": component_sizes.get(component_id),
            }
        )

    return result


def _debug_source_penalty(link: dict[str, Any]) -> float:
    source = str(link.get("source") or "")

    if source == "local_rescue_node":
        return getattr(rm, "LOCAL_RESCUE_EXTRA_PENALTY", 1.25)

    if source.startswith("edge_") or source.startswith("nearby_edge_"):
        return 0.45

    if source != "station_link":
        return 0.8

    if not link.get("is_primary"):
        return 0.15

    return 0.0


def _debug_choose_best_rejected_pair(
    rejected_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not rejected_items:
        return None

    def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        diag = item.get("transition_diag") or {}

        has_graph_path = item.get("graph_distance_km") is not None

        relative_error = diag.get("relative_error")
        if relative_error is None:
            relative_error = 999999.0

        distance_error = diag.get("distance_error_km")
        if distance_error is None:
            distance_error = 999999.0

        render_total_distance_km = item.get("render_total_distance_km")
        if render_total_distance_km is None:
            render_total_distance_km = 999999.0

        connector_penalty = item.get("connector_penalty")
        if connector_penalty is None:
            connector_penalty = 999999.0

        return (
            0 if has_graph_path else 1,
            float(relative_error),
            float(distance_error),
            float(render_total_distance_km),
            float(connector_penalty),
            str(item.get("from_node_hash") or ""),
            str(item.get("to_node_hash") or ""),
        )

    return min(rejected_items, key=sort_key)


def _debug_get_nearby_options(
    *,
    candidate: Any,
    network: dict[str, Any],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]] | None,
    radius_m: int,
) -> list[dict[str, Any]]:
    attach_fn = getattr(rm, "get_nearby_edge_attach_options_for_candidate", None)

    if attach_fn is not None and nearby_edge_cache is not None:
        try:
            return attach_fn(candidate, network, nearby_edge_cache, radius_m)
        except TypeError:
            pass

    return rm.get_nearby_edge_link_options_for_candidate(
        candidate,
        network,
        radius_m=radius_m,
        fallback_node_cache=fallback_node_cache,
    )


def _debug_build_pair_payload(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Any,
    current_candidate: Any,
    start_link: dict[str, Any],
    end_link: dict[str, Any],
    adjacency: dict[str, list[dict[str, Any]]],
    node_coords: dict[str, dict[str, float]],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
) -> tuple[bool, dict[str, Any]]:
    graph_path = rm.dijkstra_topology_path(
        adjacency=adjacency,
        node_coords=node_coords,
        start_node_hash=str(start_link["node_hash"]),
        end_node_hash=str(end_link["node_hash"]),
        path_cache=path_cache,
    )

    base_payload = {
        "from_node_hash": str(start_link["node_hash"]),
        "from_source": start_link.get("source"),
        "from_entry_km": round_opt(start_link.get("link_distance_km")),
        "from_component_id": start_link.get("component_id"),
        "from_component_size": start_link.get("component_size"),
        "to_node_hash": str(end_link["node_hash"]),
        "to_source": end_link.get("source"),
        "to_entry_km": round_opt(end_link.get("link_distance_km")),
        "to_component_id": end_link.get("component_id"),
        "to_component_size": end_link.get("component_size"),
    }

    if graph_path is None:
        return False, {
            **base_payload,
            "rejected_reason": "no_graph_path",
            "transition_diag": {
                "delta_rzd_km": None,
                "graph_distance_km": None,
                "distance_error_km": None,
                "relative_error": None,
                "hop_count": None,
                "rejected_reason": "no_graph_path",
            },
            "geometry": None,
        }

    render_total_distance_km = (
        float(graph_path["distance_km"])
        + float(start_link["link_distance_km"])
        + float(end_link["link_distance_km"])
    )

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

    source_penalty = (
        _debug_source_penalty(start_link)
        + _debug_source_penalty(end_link)
    )

    sequences: list[list[list[float]]] = []

    connector_start = [
        [float(previous_candidate.lon), float(previous_candidate.lat)],
        [float(start_link["node_lon"]), float(start_link["node_lat"])],
    ]
    if connector_start[0] != connector_start[1]:
        sequences.append(connector_start)

    graph_coords = graph_path.get("coordinates") or []
    if graph_coords:
        sequences.append(graph_coords)

    connector_end = [
        [float(end_link["node_lon"]), float(end_link["node_lat"])],
        [float(current_candidate.lon), float(current_candidate.lat)],
    ]
    if connector_end[0] != connector_end[1]:
        sequences.append(connector_end)

    coordinates = _debug_merge_coordinate_sequences(sequences)
    geometry = None
    if len(coordinates) >= 2:
        geometry = {
            "type": "LineString",
            "coordinates": coordinates,
        }

    common_payload = {
        **base_payload,
        "graph_distance_km": round_opt(graph_path.get("distance_km")),
        "render_total_distance_km": round_opt(render_total_distance_km),
        "hop_count": int(graph_path.get("hop_count") or 0),
        "connector_penalty": round_opt(connector_penalty),
        "source_penalty": round_opt(source_penalty),
        "transition_diag": transition_diag,
        "geometry": geometry,
    }

    if transition_cost is None:
        return False, {
            **common_payload,
            "rejected_reason": (transition_diag or {}).get("rejected_reason") or "transition_rejected",
        }

    final_score = float(transition_cost) + connector_penalty + source_penalty

    return True, {
        **common_payload,
        "transition_cost": round_opt(transition_cost),
        "final_score": round_opt(final_score),
    }


def _debug_evaluate_mode(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Any,
    current_candidate: Any,
    start_links: list[dict[str, Any]],
    end_links: list[dict[str, Any]],
    adjacency: dict[str, list[dict[str, Any]]],
    node_coords: dict[str, dict[str, float]],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
) -> dict[str, Any]:
    pairs_checked = 0
    rejected_reason_counts: dict[str, int] = {}
    successful_items: list[dict[str, Any]] = []
    rejected_items: list[dict[str, Any]] = []

    seen_pairs: set[tuple[str, str]] = set()

    for start_link in start_links:
        for end_link in end_links:
            pair_key = (str(start_link["node_hash"]), str(end_link["node_hash"]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            pairs_checked += 1

            is_success, payload = _debug_build_pair_payload(
                previous_stop=previous_stop,
                current_stop=current_stop,
                previous_candidate=previous_candidate,
                current_candidate=current_candidate,
                start_link=start_link,
                end_link=end_link,
                adjacency=adjacency,
                node_coords=node_coords,
                path_cache=path_cache,
            )

            if is_success:
                successful_items.append(payload)
            else:
                reason = str(payload.get("rejected_reason") or "unknown_rejection")
                rejected_reason_counts[reason] = rejected_reason_counts.get(reason, 0) + 1
                rejected_items.append(payload)

    best_pair = None
    if successful_items:
        best_pair = min(
            successful_items,
            key=lambda item: (
                float(item.get("final_score") or 999999.0),
                float(item.get("connector_penalty") or 999999.0),
                float(item.get("source_penalty") or 999999.0),
                float(item.get("render_total_distance_km") or 999999.0),
                int(item.get("hop_count") or 999999),
            ),
        )

    best_rejected_pair = _debug_choose_best_rejected_pair(rejected_items)

    shared_components = sorted(
        {
            int(start_item["component_id"])
            for start_item in start_links
            if start_item.get("component_id") is not None
        }
        & {
            int(end_item["component_id"])
            for end_item in end_links
            if end_item.get("component_id") is not None
        }
    )

    return {
        "pairs_checked": pairs_checked,
        "successful_pairs_count": len(successful_items),
        "rejected_reason_counts": rejected_reason_counts,
        "best_pair": best_pair,
        "best_rejected_pair": best_rejected_pair,
        "shared_components": shared_components,
    }


def build_segment_debug(
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
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    adjacency = network["adjacency"]
    node_coords = network["node_coords"]

    if previous_candidate is None or current_candidate is None:
        empty_mode = {
            "pairs_checked": 0,
            "successful_pairs_count": 0,
            "rejected_reason_counts": {"missing_locked_candidates": 1},
            "best_pair": None,
            "best_rejected_pair": None,
            "shared_components": [],
        }
        return {
            "segment_index": segment_index,
            "from_stop_sequence": previous_stop.get("stop_sequence"),
            "to_stop_sequence": current_stop.get("stop_sequence"),
            "from_station_name_raw": previous_stop.get("station_name_raw"),
            "to_station_name_raw": current_stop.get("station_name_raw"),
            "from_selected_station_id": getattr(previous_candidate, "station_id", None),
            "to_selected_station_id": getattr(current_candidate, "station_id", None),
            "chosen_search_mode": None,
            "path_found": False,
            "reason": "missing_locked_candidates",
            "station_links_only": empty_mode,
            "station_links_plus_nearby_edges_400m": empty_mode,
            "station_links_plus_nearby_edges_600m": empty_mode,
            "station_links_plus_local_rescue": empty_mode,
            "bridge_last_resort_best": None,
        }

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

    base_start_links = _debug_annotate_link_options_with_components(base_start_links, network)
    base_end_links = _debug_annotate_link_options_with_components(base_end_links, network)

    nearby_400_start_raw = rm.merge_link_options(
        base_start_links,
        _debug_get_nearby_options(
            candidate=previous_candidate,
            network=network,
            fallback_node_cache=fallback_node_cache,
            nearby_edge_cache=nearby_edge_cache,
            radius_m=400,
        ),
    )
    nearby_400_end_raw = rm.merge_link_options(
        base_end_links,
        _debug_get_nearby_options(
            candidate=current_candidate,
            network=network,
            fallback_node_cache=fallback_node_cache,
            nearby_edge_cache=nearby_edge_cache,
            radius_m=400,
        ),
    )
    nearby_400_start = _debug_annotate_link_options_with_components(nearby_400_start_raw, network)
    nearby_400_end = _debug_annotate_link_options_with_components(nearby_400_end_raw, network)

    nearby_600_start_raw = rm.merge_link_options(
        nearby_400_start,
        _debug_get_nearby_options(
            candidate=previous_candidate,
            network=network,
            fallback_node_cache=fallback_node_cache,
            nearby_edge_cache=nearby_edge_cache,
            radius_m=600,
        ),
    )
    nearby_600_end_raw = rm.merge_link_options(
        nearby_400_end,
        _debug_get_nearby_options(
            candidate=current_candidate,
            network=network,
            fallback_node_cache=fallback_node_cache,
            nearby_edge_cache=nearby_edge_cache,
            radius_m=600,
        ),
    )
    nearby_600_start = _debug_annotate_link_options_with_components(nearby_600_start_raw, network)
    nearby_600_end = _debug_annotate_link_options_with_components(nearby_600_end_raw, network)

    rescue_start_raw = rm.merge_link_options(
        nearby_600_start,
        rm.get_local_rescue_node_options_for_candidate(
            previous_candidate,
            network,
            rescue_node_cache,
        ),
    )
    rescue_end_raw = rm.merge_link_options(
        nearby_600_end,
        rm.get_local_rescue_node_options_for_candidate(
            current_candidate,
            network,
            rescue_node_cache,
        ),
    )
    rescue_start = _debug_annotate_link_options_with_components(rescue_start_raw, network)
    rescue_end = _debug_annotate_link_options_with_components(rescue_end_raw, network)

    station_links_only = _debug_evaluate_mode(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=base_start_links,
        end_links=base_end_links,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
    )

    station_links_plus_nearby_edges_400m = _debug_evaluate_mode(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=nearby_400_start,
        end_links=nearby_400_end,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
    )

    station_links_plus_nearby_edges_600m = _debug_evaluate_mode(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=nearby_600_start,
        end_links=nearby_600_end,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
    )

    station_links_plus_local_rescue = _debug_evaluate_mode(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=rescue_start,
        end_links=rescue_end,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
    )

    chosen_search_mode = None
    path_found = False

    if station_links_only.get("best_pair") is not None:
        chosen_search_mode = "station_links_only"
        path_found = True
    elif station_links_plus_nearby_edges_400m.get("best_pair") is not None:
        chosen_search_mode = "station_links_plus_nearby_edges_400m"
        path_found = True
    elif station_links_plus_nearby_edges_600m.get("best_pair") is not None:
        chosen_search_mode = "station_links_plus_nearby_edges_600m"
        path_found = True
    elif station_links_plus_local_rescue.get("best_pair") is not None:
        chosen_search_mode = "station_links_plus_local_rescue"
        path_found = True

    bridge_last_resort_best = None
    if not path_found:
        bridge_result = rm.try_isolated_component_bridge_rescue(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            all_start_links=rescue_start,
            all_end_links=rescue_end,
        )

        if bridge_result is not None:
            chosen_search_mode = "isolated_component_bridge_last_resort"
            path_found = True
            bridge_last_resort_best = {
                "render_method": bridge_result.get("render_method"),
                "search_mode": bridge_result.get("search_mode"),
                "from_node_hash": (bridge_result.get("start_link") or {}).get("node_hash"),
                "from_source": (bridge_result.get("start_link") or {}).get("source"),
                "from_entry_km": round_opt(bridge_result.get("connector_start_km")),
                "to_node_hash": (bridge_result.get("end_link") or {}).get("node_hash"),
                "to_source": (bridge_result.get("end_link") or {}).get("source"),
                "to_entry_km": round_opt(bridge_result.get("connector_end_km")),
                "graph_distance_km": round_opt(bridge_result.get("graph_distance_km")),
                "render_total_distance_km": round_opt(bridge_result.get("total_score_km")),
                "bridge_gap_km": round_opt(bridge_result.get("bridge_gap_km")),
                "hop_count": int(bridge_result.get("graph_edge_count") or 0),
                "transition_diag": bridge_result.get("transition_diag"),
                "geometry": {
                    "type": "LineString",
                    "coordinates": bridge_result.get("coordinates") or [],
                } if bridge_result.get("coordinates") else None,
            }

    return {
        "segment_index": segment_index,
        "from_stop_sequence": previous_stop.get("stop_sequence"),
        "to_stop_sequence": current_stop.get("stop_sequence"),
        "from_station_name_raw": previous_stop.get("station_name_raw"),
        "to_station_name_raw": current_stop.get("station_name_raw"),
        "from_selected_station_id": getattr(previous_candidate, "station_id", None),
        "to_selected_station_id": getattr(current_candidate, "station_id", None),
        "chosen_search_mode": chosen_search_mode,
        "path_found": path_found,
        "station_links_only": station_links_only,
        "station_links_plus_nearby_edges_400m": station_links_plus_nearby_edges_400m,
        "station_links_plus_nearby_edges_600m": station_links_plus_nearby_edges_600m,
        "station_links_plus_local_rescue": station_links_plus_local_rescue,
        "bridge_last_resort_best": bridge_last_resort_best,
    }


def _console_pair_summary(pair: dict[str, Any]) -> dict[str, Any]:
    return {
        "from_node_hash": pair.get("from_node_hash"),
        "from_source": pair.get("from_source"),
        "from_entry_km": pair.get("from_entry_km"),
        "from_component_id": pair.get("from_component_id"),
        "from_component_size": pair.get("from_component_size"),
        "to_node_hash": pair.get("to_node_hash"),
        "to_source": pair.get("to_source"),
        "to_entry_km": pair.get("to_entry_km"),
        "to_component_id": pair.get("to_component_id"),
        "to_component_size": pair.get("to_component_size"),
        "graph_distance_km": pair.get("graph_distance_km"),
        "render_total_distance_km": pair.get("render_total_distance_km"),
        "hop_count": pair.get("hop_count"),
        "transition_cost": pair.get("transition_cost"),
        "connector_penalty": pair.get("connector_penalty"),
        "source_penalty": pair.get("source_penalty"),
        "final_score": pair.get("final_score"),
        "rejected_reason": pair.get("rejected_reason"),
        "transition_diag": pair.get("transition_diag"),
    }


def print_segment_debug(segment_debug: dict[str, Any]) -> None:
    print(
        f"[segment {segment_debug['segment_index']}] "
        f"{segment_debug['from_station_name_raw']} -> {segment_debug['to_station_name_raw']} | "
        f"chosen_search_mode={segment_debug.get('chosen_search_mode')} | "
        f"path_found={segment_debug.get('path_found')}"
    )

    mode_names = [
        "station_links_only",
        "station_links_plus_nearby_edges_400m",
        "station_links_plus_nearby_edges_600m",
        "station_links_plus_local_rescue",
    ]

    for mode_name in mode_names:
        mode_payload = segment_debug.get(mode_name) or {}
        print(
            f"  {mode_name}: "
            f"pairs_checked={mode_payload.get('pairs_checked', 0)} | "
            f"successful_pairs_count={mode_payload.get('successful_pairs_count', 0)} | "
            f"rejected_reason_counts={mode_payload.get('rejected_reason_counts', {})}"
        )

        best_pair = mode_payload.get("best_pair")
        if best_pair is not None:
            print(
                "    best_pair="
                + json.dumps(
                    _console_pair_summary(best_pair),
                    ensure_ascii=False,
                    default=safe_json_default,
                )
            )

        best_rejected_pair = mode_payload.get("best_rejected_pair")
        if best_rejected_pair is not None:
            print(
                "    best_rejected_pair="
                + json.dumps(
                    _console_pair_summary(best_rejected_pair),
                    ensure_ascii=False,
                    default=safe_json_default,
                )
            )

        shared_components = mode_payload.get("shared_components")
        if shared_components == [] and mode_name == "station_links_plus_local_rescue":
            print("  shared_components=[]")

    bridge_best = segment_debug.get("bridge_last_resort_best")
    if bridge_best is not None:
        print(
            "  isolated_component_bridge_last_resort: best_pair="
            + json.dumps(
                {
                    "from_node_hash": bridge_best.get("from_node_hash"),
                    "from_source": bridge_best.get("from_source"),
                    "from_entry_km": bridge_best.get("from_entry_km"),
                    "to_node_hash": bridge_best.get("to_node_hash"),
                    "to_source": bridge_best.get("to_source"),
                    "to_entry_km": bridge_best.get("to_entry_km"),
                    "graph_distance_km": bridge_best.get("graph_distance_km"),
                    "render_total_distance_km": bridge_best.get("render_total_distance_km"),
                    "bridge_gap_km": bridge_best.get("bridge_gap_km"),
                    "hop_count": bridge_best.get("hop_count"),
                    "transition_diag": bridge_best.get("transition_diag"),
                },
                ensure_ascii=False,
                default=safe_json_default,
            )
        )

def _edge_target_and_cost(raw_edge):
    if not isinstance(raw_edge, dict):
        return None, None

    target = (
        raw_edge.get("to_node_hash")
        or raw_edge.get("to_hash")
        or raw_edge.get("to_node")
        or raw_edge.get("neighbor_hash")
        or raw_edge.get("target_node_hash")
    )
    if not target:
        return None, None

    cost = None
    for key in (
        "distance_km",
        "weight_km",
        "length_km",
        "km",
        "cost",
        "weight",
    ):
        value = raw_edge.get(key)
        if value is not None:
            try:
                cost = float(value)
                break
            except (TypeError, ValueError):
                pass

    if cost is None:
        return None, None

    return str(target), cost


def _normalize_edge(to_node_hash, distance_km, original_edge=None):
    payload = {
        "to_node_hash": str(to_node_hash),
        "distance_km": float(distance_km),
    }
    if isinstance(original_edge, dict):
        payload["_original_edge"] = original_edge
    return payload


def _iter_out_edges(adjacency, node_hash):
    for raw_edge in adjacency.get(node_hash, []) or []:
        to_node_hash, distance_km = _edge_target_and_cost(raw_edge)
        if to_node_hash is None or distance_km is None:
            continue
        yield to_node_hash, distance_km, raw_edge


def _build_reverse_adjacency(adjacency):
    reverse_adj = defaultdict(list)

    for from_node_hash, edges in (adjacency or {}).items():
        for to_node_hash, distance_km, raw_edge in _iter_out_edges(adjacency, from_node_hash):
            reverse_adj[to_node_hash].append(
                _normalize_edge(
                    to_node_hash=from_node_hash,
                    distance_km=distance_km,
                    original_edge=raw_edge,
                )
            )

    return dict(reverse_adj)


def _build_undirected_adjacency(adjacency):
    undirected = defaultdict(list)

    for from_node_hash, edges in (adjacency or {}).items():
        for to_node_hash, distance_km, raw_edge in _iter_out_edges(adjacency, from_node_hash):
            undirected[from_node_hash].append(
                _normalize_edge(
                    to_node_hash=to_node_hash,
                    distance_km=distance_km,
                    original_edge=raw_edge,
                )
            )
            undirected[to_node_hash].append(
                _normalize_edge(
                    to_node_hash=from_node_hash,
                    distance_km=distance_km,
                    original_edge=raw_edge,
                )
            )

    return dict(undirected)


def _dijkstra_with_path(adjacency, start_node_hash, goal_node_hash, max_visits=500000):
    if not start_node_hash or not goal_node_hash:
        return None

    start_node_hash = str(start_node_hash)
    goal_node_hash = str(goal_node_hash)

    heap = [(0.0, start_node_hash)]
    dist = {start_node_hash: 0.0}
    prev = {}
    prev_edge = {}
    visited = set()
    visits = 0

    while heap:
        current_dist, node_hash = heapq.heappop(heap)
        if node_hash in visited:
            continue
        visited.add(node_hash)

        visits += 1
        if visits > max_visits:
            return {
                "found": False,
                "reason": "max_visits_exceeded",
                "visited_count": visits,
            }

        if node_hash == goal_node_hash:
            break

        for to_node_hash, edge_cost, raw_edge in _iter_out_edges(adjacency, node_hash):
            new_dist = current_dist + edge_cost
            old_dist = dist.get(to_node_hash)
            if old_dist is None or new_dist < old_dist:
                dist[to_node_hash] = new_dist
                prev[to_node_hash] = node_hash
                prev_edge[to_node_hash] = {
                    "from_node_hash": node_hash,
                    "to_node_hash": to_node_hash,
                    "distance_km": edge_cost,
                    "raw_edge": raw_edge,
                }
                heapq.heappush(heap, (new_dist, to_node_hash))

    if goal_node_hash not in dist:
        return {
            "found": False,
            "reason": "no_path",
            "visited_count": visits,
        }

    node_path = [goal_node_hash]
    edge_path = []
    cursor = goal_node_hash

    while cursor != start_node_hash:
        edge_info = prev_edge[cursor]
        edge_path.append(edge_info)
        cursor = prev[cursor]
        node_path.append(cursor)

    node_path.reverse()
    edge_path.reverse()

    return {
        "found": True,
        "start_node_hash": start_node_hash,
        "goal_node_hash": goal_node_hash,
        "total_distance_km": dist[goal_node_hash],
        "hop_count": len(edge_path),
        "visited_count": visits,
        "node_path": node_path,
        "edge_path": edge_path,
    }


def _safe_ratio(a, b):
    try:
        if a is None or b in (None, 0):
            return None
        return float(a) / float(b)
    except Exception:
        return None


def _fmt_float(value, digits=4):
    if value is None:
        return "None"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


def _get_pair_relative_error(pair):
    diag = (pair or {}).get("transition_diag", {}) or {}
    value = diag.get("relative_error")
    if value is None:
        return math.inf
    try:
        return abs(float(value))
    except Exception:
        return math.inf


def _get_pair_distance_error(pair):
    diag = (pair or {}).get("transition_diag", {}) or {}
    value = diag.get("distance_error_km")
    if value is None:
        return math.inf
    try:
        return abs(float(value))
    except Exception:
        return math.inf


def _pick_reference_pair(segment_debug):
    modes = (segment_debug or {}).get("modes", {}) or {}
    chosen_search_mode = (segment_debug or {}).get("chosen_search_mode")

    candidates = []

    for mode_name, mode_payload in modes.items():
        if not isinstance(mode_payload, dict):
            continue

        for key_name, pair_kind in (
            ("best_pair", "success"),
            ("best_success", "success"),
            ("best_rejected_pair", "rejected"),
            ("best_rejected", "rejected"),
        ):
            pair = mode_payload.get(key_name)
            if not isinstance(pair, dict):
                continue

            candidates.append(
                {
                    "mode_name": mode_name,
                    "pair_kind": pair_kind,
                    "pair": pair,
                    "sort_key": (
                        0 if pair_kind == "success" else 1,
                        0 if mode_name == chosen_search_mode else 1,
                        _get_pair_relative_error(pair),
                        _get_pair_distance_error(pair),
                    ),
                }
            )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item["sort_key"])
    return candidates[0]


def _print_path_preview(title, path_result, limit=20):
    print(title)

    if not path_result:
        print("  path_result = None")
        return

    if not path_result.get("found"):
        print(
            f"  found=False | reason={path_result.get('reason')} "
            f"| visited_count={path_result.get('visited_count')}"
        )
        return

    print(
        f"  found=True | total_distance_km={_fmt_float(path_result.get('total_distance_km'))} "
        f"| hop_count={path_result.get('hop_count')} "
        f"| visited_count={path_result.get('visited_count')}"
    )

    edge_path = path_result.get("edge_path", []) or []
    if not edge_path:
        return

    head_count = min(limit, len(edge_path))
    tail_start = max(head_count, len(edge_path) - limit)

    print("  first_edges:")
    for idx in range(head_count):
        edge = edge_path[idx]
        print(
            f"    [{idx + 1}] "
            f"{edge.get('from_node_hash')} -> {edge.get('to_node_hash')} "
            f"| edge_km={_fmt_float(edge.get('distance_km'))}"
        )

    if tail_start > head_count:
        print("    ...")

    if tail_start < len(edge_path):
        print("  last_edges:")
        for idx in range(tail_start, len(edge_path)):
            edge = edge_path[idx]
            print(
                f"    [{idx + 1}] "
                f"{edge.get('from_node_hash')} -> {edge.get('to_node_hash')} "
                f"| edge_km={_fmt_float(edge.get('distance_km'))}"
            )


def _print_node_degree_info(adjacency, reverse_adjacency, node_hash, label):
    out_degree = 0
    in_degree = 0

    if node_hash:
        out_degree = sum(1 for _ in _iter_out_edges(adjacency, node_hash))
        in_degree = sum(1 for _ in _iter_out_edges(reverse_adjacency, node_hash))

    print(
        f"{label}: node_hash={node_hash} | out_degree={out_degree} | in_degree={in_degree}"
    )


def print_corridor_topology_diagnostics(
    segment_debug: dict[str, Any],
    adjacency: dict[str, list[dict[str, Any]]],
) -> None:
    print_section("CORRIDOR TOPOLOGY DIAGNOSTICS")

    def _extract_reference_pair(seg: dict[str, Any]) -> dict[str, Any] | None:
        raw_best = seg.get("raw_best_overall")
        if isinstance(raw_best, dict):
            return raw_best

        for mode in seg.get("search_modes", []):
            best_success = mode.get("best_success")
            if isinstance(best_success, dict):
                return best_success

        for mode in seg.get("search_modes", []):
            best_rejected = mode.get("best_rejected_pair") or mode.get("best_rejected")
            if isinstance(best_rejected, dict):
                return best_rejected

        return None

    def _short_edge_payload(edge: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "to_node_hash": edge.get("to_node_hash"),
            "distance_km": edge.get("distance_km"),
            "edge_type": edge.get("edge_type"),
            "line_id": edge.get("line_id"),
            "track_id": edge.get("track_id"),
            "is_bidirectional": edge.get("is_bidirectional"),
        }
        return payload

    def _print_node_neighborhood(node_hash: str, title: str) -> None:
        edges = adjacency.get(node_hash) or []
        print(f"{title}: node_hash={node_hash} | outgoing_edges={len(edges)}")

        if not edges:
            print("  []")
            return

        sorted_edges = sorted(
            edges,
            key=lambda x: (
                float(x.get("distance_km") or 0.0),
                str(x.get("to_node_hash") or ""),
            ),
        )

        for edge in sorted_edges[:15]:
            print(f"  - {json.dumps(_short_edge_payload(edge), ensure_ascii=False)}")

    reference_pair = _extract_reference_pair(segment_debug)
    if not reference_pair:
        print("Не удалось выбрать reference pair для дополнительной диагностики.")
        return

    from_node_hash = reference_pair.get("from_node_hash")
    to_node_hash = reference_pair.get("to_node_hash")

    print(
        json.dumps(
            {
                "segment_index": segment_debug.get("segment_index"),
                "from_station_name_raw": segment_debug.get("from_station_name_raw"),
                "to_station_name_raw": segment_debug.get("to_station_name_raw"),
                "path_found": segment_debug.get("path_found"),
                "chosen_search_mode": segment_debug.get("chosen_search_mode"),
                "delta_rzd_km": segment_debug.get("delta_rzd_km"),
                "geo_distance_km": segment_debug.get("geo_distance_km"),
                "ratio_geo_to_rzd": segment_debug.get("ratio_geo_to_rzd"),
                "ratio_graph_to_rzd": segment_debug.get("ratio_graph_to_rzd"),
                "reference_pair": reference_pair,
            },
            ensure_ascii=False,
            indent=2,
            default=safe_json_default,
        )
    )

    if not from_node_hash or not to_node_hash:
        print("У reference pair нет from_node_hash/to_node_hash.")
        return

    _print_node_neighborhood(from_node_hash, "FROM NODE")
    _print_node_neighborhood(to_node_hash, "TO NODE")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("route_id", type=int)
    parser.add_argument("--segment", type=int, default=None)
    args = parser.parse_args()

    route_id = args.route_id
    only_segment = args.segment

    print_section("START")
    print(f"route_id = {route_id}")
    if only_segment is not None:
        print(f"segment = {only_segment}")

    payload = rm.load_route(route_id)
    route = payload["route"]
    stops = payload["stops"]

    print_section("ROUTE")
    print(f"route_id: {route.get('id')}")
    print(f"train_number: {route.get('train_number')}")
    print(f"route_name: {route.get('route_name')}")
    print(f"stops_count: {len(stops)}")

    total_segments = max(0, len(stops) - 1)
    if only_segment is not None and not (1 <= only_segment <= total_segments):
        raise SystemExit(
            f"--segment должен быть в диапазоне 1..{total_segments}, "
            f"получено: {only_segment}"
        )

    catalog_payload = rm.load_global_station_catalog()
    candidates_per_stop: list[list[Any]] = [
        rm.build_candidates_for_stop(stop, catalog_payload)
        for stop in stops
    ]

    inferred_region_codes = rm.infer_route_region_codes(
        stops,
        candidates_per_stop,
    )

    print_section("INFERRED REGIONS")
    print(
        dump_pretty(
            {
                "inferred_region_codes": inferred_region_codes,
            }
        )
    )

    network = rm.build_network_data(region_codes=inferred_region_codes)
    adjacency = network.get("adjacency") or {}

    print_section("NETWORK")
    print(dump_pretty(network.get("stats") or {}))

    locked_candidates, lock_logs = rm.lock_route_stop_candidates(
        stops,
        candidates_per_stop,
    )

    print_section("LOCKED STOPS")
    for stop, locked_candidate, lock_log in zip(stops, locked_candidates, lock_logs):
        print(
            f"stop_sequence={stop.get('stop_sequence')} | "
            f"raw={stop.get('station_name_raw')} | "
            f"locked_station_id={locked_candidate.station_id if locked_candidate else None} | "
            f"locked_station_name={locked_candidate.name if locked_candidate else None} | "
            f"lock_reason={lock_log.get('lock_reason')}"
        )

    print_section("ALL STOP CANDIDATES")
    all_stop_candidates_payload: list[dict[str, Any]] = []

    for stop, candidates, locked_candidate in zip(stops, candidates_per_stop, locked_candidates):
        locked_station_id = locked_candidate.station_id if locked_candidate else None
        print(
            f"stop_sequence={stop.get('stop_sequence')} | "
            f"station_name_raw={stop.get('station_name_raw')} | "
            f"candidate_count={len(candidates)} | "
            f"locked_station_id={locked_station_id}"
        )

        candidate_payloads: list[dict[str, Any]] = []

        for candidate in candidates:
            serialized = serialize_candidate(
                stop,
                candidate,
                locked_station_id=locked_station_id,
            )
            candidate_payloads.append(serialized)
            marker = "*" if serialized["locked"] else "-"
            print(f"  {marker} {json.dumps(serialized, ensure_ascii=False)}")

        all_stop_candidates_payload.append(
            {
                "stop_sequence": stop.get("stop_sequence"),
                "station_name_raw": stop.get("station_name_raw"),
                "station_code_rzd": stop.get("station_code_rzd"),
                "locked_station_id": locked_station_id,
                "candidates": candidate_payloads,
            }
        )

    print_section("SEGMENT DEBUG")

    path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    fallback_node_cache: dict[int, list[dict[str, Any]]] = {}
    rescue_node_cache: dict[int, list[dict[str, Any]]] = {}
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}

    segment_debug_payloads: list[dict[str, Any]] = []

    segment_indices = (
        [only_segment]
        if only_segment is not None
        else list(range(1, len(stops)))
    )

    for index in segment_indices:
        previous_stop = stops[index - 1]
        current_stop = stops[index]
        previous_candidate = locked_candidates[index - 1]
        current_candidate = locked_candidates[index]

        segment_debug = build_segment_debug(
            segment_index=index,
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
            nearby_edge_cache=nearby_edge_cache,
        )

        segment_debug_payloads.append(segment_debug)
        print_segment_debug(segment_debug)

        if only_segment is not None:
            print_corridor_topology_diagnostics(
                segment_debug=segment_debug,
                adjacency=adjacency,
            )

    output_dir = Path("debug_output")
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"route_graph_detailed_{route_id}_{timestamp}.json"

    output_payload = {
        "route": {
            "id": route.get("id"),
            "train_number": route.get("train_number"),
            "route_name": route.get("route_name"),
            "snapshot_date": route.get("snapshot_date"),
            "stops_count": len(stops),
        },
        "inferred_regions": {
            "inferred_region_codes": inferred_region_codes,
        },
        "network": network.get("stats") or {},
        "locked_stops": [
            serialize_locked_stop(stop, locked_candidate)
            for stop, locked_candidate in zip(stops, locked_candidates)
        ],
        "all_stop_candidates": all_stop_candidates_payload,
        "segment_debug": segment_debug_payloads,
    }

    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(
            output_payload,
            fh,
            ensure_ascii=False,
            indent=2,
            default=safe_json_default,
        )

    print_section("FILE SAVED")
    print(str(output_path.resolve()))

if __name__ == "__main__":
    main()