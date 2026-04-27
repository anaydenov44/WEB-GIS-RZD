"""Live anchor repair patch for app.route_graph_matcher.

What it does:
- keeps the main matcher module intact;
- monkey-patches only the runtime pieces related to topology anchoring;
- prints anchor-repair work to console;
- uses real nearby-edge attach options from DB, not only nearest graph nodes;
- tries a middle-station candidate swap when an incoming segment cannot be rendered.

Usage:
    from route_graph_matcher_live_anchor_patch import apply_live_anchor_patch
    apply_live_anchor_patch()

    # then import/use app.route_graph_matcher.resolve_route_for_map as usual
"""

from __future__ import annotations

import json
import math
import threading
from typing import Any

import app.route_graph_matcher as rgm

_PATCH_THREAD_CTX = threading.local()

ANCHOR_REPAIR_MAX_STATION_CANDIDATES = 5
ANCHOR_REPAIR_MIN_NAME_SCORE = 0.40
ANCHOR_REPAIR_IMPROVEMENT_MARGIN = 0.50
ANCHOR_REPAIR_PRINT_JSON = True


def _anchor_console(title: str, payload: dict[str, Any] | None = None, *, enabled: bool = True) -> None:
    if not enabled:
        return

    print("\n[anchor-repair] " + title, flush=True)
    if payload is None:
        return

    try:
        if ANCHOR_REPAIR_PRINT_JSON:
            print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        else:
            print(str(payload), flush=True)
    except Exception:
        print(str(payload), flush=True)


def _set_ctx_value(name: str, value: Any) -> None:
    setattr(_PATCH_THREAD_CTX, name, value)


def _get_ctx_value(name: str, default: Any = None) -> Any:
    return getattr(_PATCH_THREAD_CTX, name, default)


def _get_candidates_per_stop() -> list[list[rgm.Candidate]]:
    return _get_ctx_value("candidates_per_stop", []) or []


def _candidate_short(candidate: rgm.Candidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "station_id": candidate.station_id,
        "station_name": candidate.name,
        "region_code": candidate.region_code,
        "effective_score": round(candidate.effective_score, 4),
        "name_score": round(candidate.name_score, 4),
        "code_match": candidate.code_match,
        "anchor": candidate.anchor,
        "match_method": candidate.match_method,
    }


def _pair_path_quality_score(pair_path: dict[str, Any] | None) -> float:
    if pair_path is None:
        return math.inf

    diag = pair_path.get("transition_diag") or {}
    distance_error = float(diag.get("distance_error_km") or 0.0)
    relative_error = float(diag.get("relative_error") or 0.0)
    connector_km = float(pair_path.get("connector_start_km") or 0.0) + float(pair_path.get("connector_end_km") or 0.0)
    bridge_gap_km = float(pair_path.get("bridge_gap_km") or 0.0)
    graph_distance_km = float(pair_path.get("graph_distance_km") or 0.0)

    search_mode_penalty = {
        "station_links_only": 0.0,
        "station_links_plus_nearby_edges_400m": 0.8,
        "station_links_plus_nearby_edges_600m": 1.3,
        "station_links_plus_local_rescue": 2.8,
        "isolated_component_bridge_last_resort": 8.0,
    }.get(str(pair_path.get("search_mode") or ""), 3.0)

    render_penalty = 0.0
    if pair_path.get("render_method") == "topology_component_bridge":
        render_penalty += 10.0

    return (
        distance_error
        + relative_error * 12.0
        + connector_km * 4.0
        + bridge_gap_km * 10.0
        + graph_distance_km * 0.015
        + search_mode_penalty
        + render_penalty
    )


def _collect_middle_station_candidates(
    stop: dict[str, Any],
    stop_index: int,
    locked_candidates: list[rgm.Candidate | None],
) -> list[rgm.Candidate]:
    raw_candidates = _get_candidates_per_stop()
    stop_candidates = raw_candidates[stop_index] if stop_index < len(raw_candidates) else []

    seen_station_ids: set[int] = set()
    result: list[rgm.Candidate] = []

    locked = locked_candidates[stop_index] if stop_index < len(locked_candidates) else None
    if locked is not None:
        seen_station_ids.add(int(locked.station_id))
        result.append(locked)

    for candidate in stop_candidates:
        station_id = int(candidate.station_id)
        if station_id in seen_station_ids:
            continue

        name_similarity = rgm.candidate_name_similarity_for_stop(stop, candidate)
        if not (
            candidate.code_match
            or candidate.anchor
            or name_similarity >= ANCHOR_REPAIR_MIN_NAME_SCORE
        ):
            continue

        seen_station_ids.add(station_id)
        result.append(candidate)
        if len(result) >= ANCHOR_REPAIR_MAX_STATION_CANDIDATES:
            break

    return result


def _try_middle_station_candidate_repair(
    *,
    segment_index: int,
    stops: list[dict[str, Any]],
    locked_candidates: list[rgm.Candidate | None],
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]],
) -> dict[str, Any] | None:
    if segment_index <= 0 or segment_index >= len(stops) - 1:
        return None

    previous_candidate = locked_candidates[segment_index - 1]
    current_candidate = locked_candidates[segment_index]
    next_candidate = locked_candidates[segment_index + 1]

    if previous_candidate is None or next_candidate is None:
        return None

    current_stop = stops[segment_index]
    previous_stop = stops[segment_index - 1]
    next_stop = stops[segment_index + 1]

    candidate_options = _collect_middle_station_candidates(current_stop, segment_index, locked_candidates)
    if not candidate_options:
        return None

    baseline_incoming = _patched_build_topology_path_between_candidates(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        network=network,
        path_cache=path_cache,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
        nearby_edge_cache=nearby_edge_cache,
        console=False,
    ) if current_candidate is not None else None

    baseline_outgoing = _patched_build_topology_path_between_candidates(
        previous_stop=current_stop,
        current_stop=next_stop,
        previous_candidate=current_candidate,
        current_candidate=next_candidate,
        network=network,
        path_cache=path_cache,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
        nearby_edge_cache=nearby_edge_cache,
        console=False,
    ) if current_candidate is not None else None

    baseline_score = (
        _pair_path_quality_score(baseline_incoming)
        + _pair_path_quality_score(baseline_outgoing)
        + (rgm.compute_lock_candidate_cost(current_stop, current_candidate) if current_candidate is not None else 0.0)
    )

    _anchor_console(
        "middle-station repair probe started",
        {
            "segment_index": segment_index,
            "stop_sequence": current_stop.get("stop_sequence"),
            "station_name_raw": current_stop.get("station_name_raw"),
            "baseline_middle_candidate": _candidate_short(current_candidate),
            "candidate_options": [_candidate_short(item) for item in candidate_options],
            "baseline_score": None if math.isinf(baseline_score) else round(baseline_score, 4),
        },
    )

    best_payload: dict[str, Any] | None = None
    best_score = baseline_score

    for candidate in candidate_options:
        incoming_path = _patched_build_topology_path_between_candidates(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=candidate,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
            nearby_edge_cache=nearby_edge_cache,
            console=False,
        )
        if incoming_path is None:
            continue

        outgoing_path = _patched_build_topology_path_between_candidates(
            previous_stop=current_stop,
            current_stop=next_stop,
            previous_candidate=candidate,
            current_candidate=next_candidate,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
            nearby_edge_cache=nearby_edge_cache,
            console=False,
        )
        if outgoing_path is None:
            continue

        total_score = (
            _pair_path_quality_score(incoming_path)
            + _pair_path_quality_score(outgoing_path)
            + rgm.compute_lock_candidate_cost(current_stop, candidate)
        )

        if total_score + ANCHOR_REPAIR_IMPROVEMENT_MARGIN < best_score:
            best_score = total_score
            best_payload = {
                "middle_candidate": candidate,
                "incoming_path": incoming_path,
                "outgoing_path": outgoing_path,
                "score": total_score,
            }

    if best_payload is None:
        _anchor_console(
            "middle-station repair probe finished: no better candidate",
            {
                "segment_index": segment_index,
                "stop_sequence": current_stop.get("stop_sequence"),
                "station_name_raw": current_stop.get("station_name_raw"),
            },
        )
        return None

    _anchor_console(
        "middle-station repair chosen",
        {
            "segment_index": segment_index,
            "stop_sequence": current_stop.get("stop_sequence"),
            "station_name_raw": current_stop.get("station_name_raw"),
            "selected_middle_candidate": _candidate_short(best_payload["middle_candidate"]),
            "baseline_middle_candidate": _candidate_short(current_candidate),
            "score": round(best_payload["score"], 4),
            "incoming_search_mode": best_payload["incoming_path"].get("search_mode"),
            "outgoing_search_mode": best_payload["outgoing_path"].get("search_mode"),
        },
    )
    return best_payload


def _patched_lock_route_stop_candidates(
    stops: list[dict[str, Any]],
    candidates_per_stop: list[list[rgm.Candidate]],
    *,
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
):
    _set_ctx_value("stops", stops)
    _set_ctx_value("candidates_per_stop", candidates_per_stop)
    return _ORIGINAL_LOCK_ROUTE_STOP_CANDIDATES(
        stops,
        candidates_per_stop,
        diagnostics=diagnostics,
        logger_context=logger_context,
    )


def _patched_build_topology_path_between_candidates(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: rgm.Candidate,
    current_candidate: rgm.Candidate,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]] | None = None,
    console: bool = True,
) -> dict[str, Any] | None:
    adjacency = network["adjacency"]
    node_coords = network["node_coords"]
    nearby_edge_cache = nearby_edge_cache if nearby_edge_cache is not None else network.setdefault("_live_anchor_nearby_edge_cache", {})

    base_start_links = rgm.get_station_link_options_for_candidate(
        previous_candidate,
        network,
        fallback_node_cache,
    )
    base_end_links = rgm.get_station_link_options_for_candidate(
        current_candidate,
        network,
        fallback_node_cache,
    )

    _anchor_console(
        "segment anchor search started",
        {
            "from_stop_sequence": previous_stop.get("stop_sequence"),
            "to_stop_sequence": current_stop.get("stop_sequence"),
            "from_station_name_raw": previous_stop.get("station_name_raw"),
            "to_station_name_raw": current_stop.get("station_name_raw"),
            "from_candidate": _candidate_short(previous_candidate),
            "to_candidate": _candidate_short(current_candidate),
            "base_start_links": len(base_start_links),
            "base_end_links": len(base_end_links),
        },
        enabled=console,
    )

    stages: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]]]] = []
    stages.append(("station_links_only", base_start_links, base_end_links))

    attach_400_start = rgm.merge_link_options(
        base_start_links,
        rgm.get_nearby_edge_attach_options_for_candidate(
            previous_candidate,
            network,
            nearby_edge_cache,
            400,
        ),
    )
    attach_400_start = rgm.merge_link_options(
        attach_400_start,
        rgm.get_nearby_edge_link_options_for_candidate(
            previous_candidate,
            network,
            radius_m=400,
            fallback_node_cache=fallback_node_cache,
        ),
    )

    attach_400_end = rgm.merge_link_options(
        base_end_links,
        rgm.get_nearby_edge_attach_options_for_candidate(
            current_candidate,
            network,
            nearby_edge_cache,
            400,
        ),
    )
    attach_400_end = rgm.merge_link_options(
        attach_400_end,
        rgm.get_nearby_edge_link_options_for_candidate(
            current_candidate,
            network,
            radius_m=400,
            fallback_node_cache=fallback_node_cache,
        ),
    )
    stages.append(("station_links_plus_nearby_edges_400m", attach_400_start, attach_400_end))

    attach_600_start = rgm.merge_link_options(
        attach_400_start,
        rgm.get_nearby_edge_attach_options_for_candidate(
            previous_candidate,
            network,
            nearby_edge_cache,
            600,
        ),
    )
    attach_600_start = rgm.merge_link_options(
        attach_600_start,
        rgm.get_nearby_edge_link_options_for_candidate(
            previous_candidate,
            network,
            radius_m=600,
            fallback_node_cache=fallback_node_cache,
        ),
    )

    attach_600_end = rgm.merge_link_options(
        attach_400_end,
        rgm.get_nearby_edge_attach_options_for_candidate(
            current_candidate,
            network,
            nearby_edge_cache,
            600,
        ),
    )
    attach_600_end = rgm.merge_link_options(
        attach_600_end,
        rgm.get_nearby_edge_link_options_for_candidate(
            current_candidate,
            network,
            radius_m=600,
            fallback_node_cache=fallback_node_cache,
        ),
    )
    stages.append(("station_links_plus_nearby_edges_600m", attach_600_start, attach_600_end))

    rescue_start_links = rgm.merge_link_options(
        attach_600_start,
        rgm.get_local_rescue_node_options_for_candidate(
            previous_candidate,
            network,
            rescue_node_cache,
        ),
    )
    rescue_end_links = rgm.merge_link_options(
        attach_600_end,
        rgm.get_local_rescue_node_options_for_candidate(
            current_candidate,
            network,
            rescue_node_cache,
        ),
    )
    stages.append(("station_links_plus_local_rescue", rescue_start_links, rescue_end_links))

    for search_mode, start_links, end_links in stages:
        _anchor_console(
            f"trying {search_mode}",
            {
                "start_links": len(start_links),
                "end_links": len(end_links),
            },
            enabled=console,
        )

        result = rgm._evaluate_topology_link_pair_options(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            start_links=start_links,
            end_links=end_links,
            adjacency=adjacency,
            node_coords=node_coords,
            path_cache=path_cache,
            search_mode=search_mode,
        )
        if result is not None:
            _anchor_console(
                f"success in {search_mode}",
                {
                    "graph_distance_km": round(float(result.get("graph_distance_km") or 0.0), 4),
                    "connector_start_km": round(float(result.get("connector_start_km") or 0.0), 4),
                    "connector_end_km": round(float(result.get("connector_end_km") or 0.0), 4),
                    "search_mode": result.get("search_mode"),
                    "render_method": result.get("render_method"),
                },
                enabled=console,
            )
            return result

    bridge_result = rgm.try_isolated_component_bridge_rescue(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        network=network,
        path_cache=path_cache,
        all_start_links=rescue_start_links,
        all_end_links=rescue_end_links,
    )
    if bridge_result is not None:
        _anchor_console(
            "success in isolated_component_bridge_last_resort",
            {
                "bridge_gap_km": round(float(bridge_result.get("bridge_gap_km") or 0.0), 4),
                "graph_distance_km": round(float(bridge_result.get("graph_distance_km") or 0.0), 4),
                "render_method": bridge_result.get("render_method"),
            },
            enabled=console,
        )
        return bridge_result

    _anchor_console(
        "segment anchor search failed",
        {
            "from_stop_sequence": previous_stop.get("stop_sequence"),
            "to_stop_sequence": current_stop.get("stop_sequence"),
        },
        enabled=console,
    )
    return None


def _patched_build_route_geometry_between_locked_candidates(
    stops: list[dict[str, Any]],
    locked_candidates: list[rgm.Candidate | None],
    network: dict[str, Any],
    *,
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
):
    logger_context = logger_context or {}

    path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    fallback_node_cache: dict[int, list[dict[str, Any]]] = {}
    rescue_node_cache: dict[int, list[dict[str, Any]]] = {}
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}

    locked_candidates = list(locked_candidates)
    segment_path_overrides: dict[int, dict[str, Any]] = {}
    anchor_repairs: list[dict[str, Any]] = []

    segment_coordinate_groups: list[list[list[float]]] = []
    segment_items: list[dict[str, Any]] = []
    network_segments: list[dict[str, Any]] = []
    transition_logs: list[dict[str, Any]] = []

    current_group: list[list[float]] = []

    for index in range(1, len(stops)):
        previous_stop = stops[index - 1]
        current_stop = stops[index]

        previous_candidate = locked_candidates[index - 1] if index - 1 < len(locked_candidates) else None
        current_candidate = locked_candidates[index] if index < len(locked_candidates) else None

        if previous_candidate is None or current_candidate is None:
            transition_log = {
                "segment_index": index,
                "from_stop_sequence": previous_stop.get("stop_sequence"),
                "to_stop_sequence": current_stop.get("stop_sequence"),
                "from_station_name_raw": previous_stop.get("station_name_raw"),
                "to_station_name_raw": current_stop.get("station_name_raw"),
                "from_selected_station_id": previous_candidate.station_id if previous_candidate else None,
                "from_selected_station_name": previous_candidate.name if previous_candidate else None,
                "to_selected_station_id": current_candidate.station_id if current_candidate else None,
                "to_selected_station_name": current_candidate.name if current_candidate else None,
                "segment_render_method": "missing_locked_station",
                "path_found": False,
                "fallback_used": False,
                "reason": "one_or_both_locked_candidates_missing",
            }
            transition_logs.append(transition_log)
            continue

        pair_path = segment_path_overrides.get(index)
        repair_applied = False
        repaired_middle_candidate = None

        if pair_path is None:
            pair_path = _patched_build_topology_path_between_candidates(
                previous_stop=previous_stop,
                current_stop=current_stop,
                previous_candidate=previous_candidate,
                current_candidate=current_candidate,
                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
                nearby_edge_cache=nearby_edge_cache,
                console=True,
            )

        if pair_path is None:
            repair_payload = _try_middle_station_candidate_repair(
                segment_index=index,
                stops=stops,
                locked_candidates=locked_candidates,
                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
                nearby_edge_cache=nearby_edge_cache,
            )
            if repair_payload is not None:
                repair_applied = True
                repaired_middle_candidate = repair_payload["middle_candidate"]
                locked_candidates[index] = repaired_middle_candidate
                current_candidate = repaired_middle_candidate
                pair_path = repair_payload["incoming_path"]
                segment_path_overrides[index + 1] = repair_payload["outgoing_path"]

                repair_log = {
                    "segment_index": index,
                    "stop_sequence": current_stop.get("stop_sequence"),
                    "station_name_raw": current_stop.get("station_name_raw"),
                    "old_candidate": _candidate_short(locked_candidates[index] if index < len(locked_candidates) else None),
                    "new_candidate": _candidate_short(repaired_middle_candidate),
                    "incoming_search_mode": repair_payload["incoming_path"].get("search_mode"),
                    "outgoing_search_mode": repair_payload["outgoing_path"].get("search_mode"),
                    "score": round(float(repair_payload["score"]), 4),
                }
                anchor_repairs.append(repair_log)

        if pair_path is not None:
            coords = pair_path.get("coordinates") or []

            if len(coords) >= 2:
                if not current_group:
                    current_group = list(coords)
                else:
                    if current_group[-1] == coords[0]:
                        current_group.extend(coords[1:])
                    else:
                        segment_coordinate_groups.append(current_group)
                        current_group = list(coords)

            segment_items.append(
                {
                    "segment_index": index,
                    "from_station_id": previous_candidate.station_id,
                    "to_station_id": current_candidate.station_id,
                    "from_station_name": previous_candidate.name,
                    "to_station_name": current_candidate.name,
                    "render_method": pair_path.get("render_method"),
                    "search_mode": pair_path.get("search_mode"),
                    "graph_distance_km": pair_path.get("graph_distance_km"),
                    "connector_start_km": pair_path.get("connector_start_km"),
                    "connector_end_km": pair_path.get("connector_end_km"),
                    "bridge_gap_km": pair_path.get("bridge_gap_km"),
                    "total_score_km": pair_path.get("total_score_km"),
                    "geometry": {
                        "type": "LineString",
                        "coordinates": coords,
                    },
                    "segment_source": (
                        "component_bridge_gap"
                        if pair_path.get("render_method") == "topology_component_bridge"
                        else "graph_locked_station_path"
                    ),
                    "diagnostic": pair_path.get("transition_diag"),
                }
            )

            edge_index = 0

            if pair_path.get("render_method") == "topology_component_bridge":
                for edge_group in pair_path.get("edge_groups") or []:
                    if edge_group.get("kind") == "graph_path":
                        for edge in edge_group.get("edge_chain") or []:
                            edge_coords = edge.get("geometry_coords") or []
                            edge_geometry = rgm.build_simple_linestring(edge_coords)
                            if edge_geometry is None:
                                continue
                            edge_index += 1
                            network_segments.append(
                                {
                                    "segment_index": index,
                                    "edge_index": edge_index,
                                    "from_node_hash": edge.get("from_node_hash"),
                                    "to_node_hash": edge.get("to_node_hash"),
                                    "length_km": edge.get("length_km"),
                                    "segment_source": "graph_locked_station_path",
                                    "geometry": edge_geometry,
                                }
                            )
                    elif edge_group.get("kind") == "component_bridge":
                        edge_geometry = rgm.build_simple_linestring(edge_group.get("geometry_coords") or [])
                        if edge_geometry is None:
                            continue
                        edge_index += 1
                        network_segments.append(
                            {
                                "segment_index": index,
                                "edge_index": edge_index,
                                "from_node_hash": (pair_path.get("bridge") or {}).get("from_node_hash"),
                                "to_node_hash": (pair_path.get("bridge") or {}).get("to_node_hash"),
                                "length_km": edge_group.get("length_km"),
                                "segment_source": "component_bridge_gap",
                                "geometry": edge_geometry,
                            }
                        )
            else:
                for edge in (pair_path.get("path") or {}).get("edge_chain") or []:
                    edge_coords = edge.get("geometry_coords") or []
                    edge_geometry = rgm.build_simple_linestring(edge_coords)
                    if edge_geometry is None:
                        continue
                    edge_index += 1
                    network_segments.append(
                        {
                            "segment_index": index,
                            "edge_index": edge_index,
                            "from_node_hash": edge.get("from_node_hash"),
                            "to_node_hash": edge.get("to_node_hash"),
                            "length_km": edge.get("length_km"),
                            "segment_source": "graph_locked_station_path",
                            "geometry": edge_geometry,
                        }
                    )

            transition_log = {
                "segment_index": index,
                "from_stop_sequence": previous_stop.get("stop_sequence"),
                "to_stop_sequence": current_stop.get("stop_sequence"),
                "from_station_name_raw": previous_stop.get("station_name_raw"),
                "to_station_name_raw": current_stop.get("station_name_raw"),
                "from_selected_station_id": previous_candidate.station_id,
                "from_selected_station_name": previous_candidate.name,
                "to_selected_station_id": current_candidate.station_id,
                "to_selected_station_name": current_candidate.name,
                "segment_render_method": pair_path.get("render_method"),
                "path_found": True,
                "fallback_used": False,
                "repair_applied": repair_applied,
                "search_mode": pair_path.get("search_mode"),
                "from_entry_node_hash": (pair_path.get("start_link") or {}).get("node_hash"),
                "from_entry_source": (pair_path.get("start_link") or {}).get("source"),
                "from_entry_km": round(float(pair_path.get("connector_start_km") or 0.0), 4),
                "to_entry_node_hash": (pair_path.get("end_link") or {}).get("node_hash"),
                "to_entry_source": (pair_path.get("end_link") or {}).get("source"),
                "to_entry_km": round(float(pair_path.get("connector_end_km") or 0.0), 4),
                "graph_distance_km": round(float(pair_path.get("graph_distance_km") or 0.0), 3),
                "connector_start_km": round(float(pair_path.get("connector_start_km") or 0.0), 3),
                "connector_end_km": round(float(pair_path.get("connector_end_km") or 0.0), 3),
                "total_score_km": round(float(pair_path.get("total_score_km") or 0.0), 3),
                "graph_edge_count": pair_path.get("graph_edge_count"),
                "bridge_gap_km": round(pair_path.get("bridge_gap_km", 0.0), 4)
                if pair_path.get("bridge_gap_km") is not None else None,
                "bridge_from_component_id": (
                    pair_path.get("transition_diag", {}).get("bridge_from_component_id")
                ),
                "bridge_to_component_id": (
                    pair_path.get("transition_diag", {}).get("bridge_to_component_id")
                ),
                "cost_diag": pair_path.get("transition_diag"),
            }
            transition_logs.append(transition_log)

            rgm.log_event(
                "info",
                "locked_station_segment_rendered_on_topology_graph",
                **transition_log,
                **logger_context,
            )
            continue

        fallback_coords = [
            [previous_candidate.lon, previous_candidate.lat],
            [current_candidate.lon, current_candidate.lat],
        ]

        if not current_group:
            current_group = list(fallback_coords)
        else:
            if current_group[-1] == fallback_coords[0]:
                current_group.extend(fallback_coords[1:])
            else:
                segment_coordinate_groups.append(current_group)
                current_group = list(fallback_coords)

        segment_items.append(
            {
                "segment_index": index,
                "from_station_id": previous_candidate.station_id,
                "to_station_id": current_candidate.station_id,
                "from_station_name": previous_candidate.name,
                "to_station_name": current_candidate.name,
                "geometry": {
                    "type": "LineString",
                    "coordinates": fallback_coords,
                },
                "segment_source": "fallback_straight",
            }
        )

        transition_log = {
            "segment_index": index,
            "from_stop_sequence": previous_stop.get("stop_sequence"),
            "to_stop_sequence": current_stop.get("stop_sequence"),
            "from_station_name_raw": previous_stop.get("station_name_raw"),
            "to_station_name_raw": current_stop.get("station_name_raw"),
            "from_selected_station_id": previous_candidate.station_id,
            "from_selected_station_name": previous_candidate.name,
            "to_selected_station_id": current_candidate.station_id,
            "to_selected_station_name": current_candidate.name,
            "segment_render_method": "fallback_straight",
            "path_found": False,
            "fallback_used": True,
            "reason": "topology_graph_path_not_found_for_locked_stations",
        }
        transition_logs.append(transition_log)

        rgm.log_event(
            "warning",
            "locked_station_segment_rendered_with_fallback_straight",
            **transition_log,
            **logger_context,
        )

    if current_group:
        segment_coordinate_groups.append(current_group)

    geometry = rgm.build_linestring_or_multilinestring(segment_coordinate_groups)

    if diagnostics is not None:
        diagnostics["transition_diagnostics"] = transition_logs
        diagnostics["anchor_repairs"] = anchor_repairs
        diagnostics["locked_station_rendering"] = {
            "segments_count": len(transition_logs),
            "segments_with_graph_path": sum(1 for item in transition_logs if item.get("path_found")),
            "segments_with_fallback": sum(1 for item in transition_logs if item.get("fallback_used")),
            "anchor_repairs_count": len(anchor_repairs),
        }

    return geometry, segment_items, network_segments, transition_logs


def apply_live_anchor_patch() -> None:
    if getattr(rgm, "_LIVE_ANCHOR_PATCH_APPLIED", False):
        return

    rgm.lock_route_stop_candidates = _patched_lock_route_stop_candidates
    rgm.build_topology_path_between_candidates = _patched_build_topology_path_between_candidates
    rgm.build_route_geometry_between_locked_candidates = _patched_build_route_geometry_between_locked_candidates
    rgm._LIVE_ANCHOR_PATCH_APPLIED = True

    _anchor_console(
        "live anchor patch applied",
        {
            "module": rgm.__name__,
            "module_file": getattr(rgm, "__file__", None),
            "features": [
                "real_nearby_edge_attach_options",
                "console_logging",
                "middle_station_candidate_repair",
            ],
        },
    )


_ORIGINAL_LOCK_ROUTE_STOP_CANDIDATES = rgm.lock_route_stop_candidates


__all__ = ["apply_live_anchor_patch"]
