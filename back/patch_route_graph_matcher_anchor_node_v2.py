from __future__ import annotations

import argparse
from pathlib import Path


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    if old not in text:
        raise SystemExit(f"[patch] anchor not found for {label}")
    return text.replace(old, new, 1)


def insert_after(text: str, anchor: str, addition: str, *, label: str) -> str:
    idx = text.find(anchor)
    if idx == -1:
        raise SystemExit(f"[patch] insert anchor not found for {label}")
    pos = idx + len(anchor)
    return text[:pos] + addition + text[pos:]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", help="Path to app/route_graph_matcher.py")
    args = parser.parse_args()

    target = Path(args.target)
    text = target.read_text(encoding="utf-8")

    text = replace_once(
        text,
        'def _station_debug_label(candidate: Candidate | None, stop: dict[str, Any] | None = None) -> str:',
        'def _station_debug_label(candidate: "Candidate | None", stop: dict[str, Any] | None = None) -> str:',
        label="station debug label forward ref",
    )

    text = replace_once(
        text,
        "ANCHOR_REPAIR_ACCEPT_DELTA = 5.0\n",
        (
            "ANCHOR_REPAIR_ACCEPT_DELTA = 5.0\n"
            "ANCHOR_NODE_REPAIR_MAX_OPTIONS = 24\n"
            "ANCHOR_NODE_REPAIR_ACCEPT_DELTA = 3.0\n"
            "ANCHOR_NODE_REPAIR_FAIL_SCORE = 1_000_000.0\n"
        ),
        label="anchor node repair constants",
    )

    anchor_render_block = '''def _anchor_render_method_penalty(render_method: str | None) -> float:
    penalties = {
        "topology_graph_path": 0.0,
        "topology_component_bridge": 20.0,
        "fallback_straight": 300.0,
    }
    return penalties.get(str(render_method or ""), 120.0)
'''
    addition = '''

def _link_source_rank(source: str | None) -> int:
    source = str(source or "")
    ranks = {
        "station_link": 0,
        "edge_400m": 1,
        "edge_600m": 2,
        "nearby_edge_400m": 3,
        "nearby_edge_600m": 4,
        "local_rescue_node": 5,
        "fallback_nearest_node": 6,
    }
    return ranks.get(source, 9)


def _link_priority(item: dict[str, Any]) -> tuple[int, int, float, str]:
    return (
        _link_source_rank(item.get("source")),
        0 if item.get("is_primary") else 1,
        float(item.get("link_distance_km") or 999999.0),
        str(item.get("node_hash") or ""),
    )
'''
    if "def _link_source_rank(" not in text:
        text = insert_after(text, anchor_render_block, addition, label="link rank helpers")

    text = replace_once(
        text,
        '''def _anchor_search_mode_penalty(search_mode: str | None) -> float:
    penalties = {
        "station_links_only": 0.0,
        "station_links_plus_nearby_edges_400m": 4.0,
        "station_links_plus_nearby_edges_600m": 7.0,
        "station_links_plus_local_rescue": 12.0,
        "isolated_component_bridge_last_resort": 35.0,
    }
    return penalties.get(str(search_mode or ""), 18.0)
''',
        '''def _anchor_search_mode_penalty(search_mode: str | None) -> float:
    penalties = {
        "station_links_only": 0.0,
        "station_links_plus_edge_attach_400m": 2.0,
        "station_links_plus_edge_attach_600m": 4.0,
        "station_links_plus_nearby_edges_400m": 5.0,
        "station_links_plus_nearby_edges_600m": 8.0,
        "station_links_plus_local_rescue": 14.0,
        "isolated_component_bridge_last_resort": 35.0,
    }
    return penalties.get(str(search_mode or ""), 18.0)
''',
        label="anchor search mode penalty",
    )

    text = replace_once(
        text,
        '''def _topology_result_source_rank(source: str | None) -> int:
    source = source or ""

    if source == "station_link":
        return 0
    if source == "nearby_edge_400m":
        return 1
    if source == "nearby_edge_600m":
        return 2
    if source == "local_rescue_node":
        return 3

    return 9
''',
        '''def _topology_result_source_rank(source: str | None) -> int:
    source = source or ""

    if source == "station_link":
        return 0
    if source == "edge_400m":
        return 1
    if source == "edge_600m":
        return 2
    if source == "nearby_edge_400m":
        return 3
    if source == "nearby_edge_600m":
        return 4
    if source == "local_rescue_node":
        return 5
    if source == "fallback_nearest_node":
        return 6

    return 9
''',
        label="topology result source rank",
    )

    text = replace_once(
        text,
        '''    source_rank = {
        "station_link": 0,
        "edge_400m": 1,
        "edge_600m": 2,
        "local_rescue_node": 3,
        "fallback_nearest_node": 4,
    }

    normalized.sort(
        key=lambda x: (
            source_rank.get(str(x.get("source") or ""), 9),
            0 if x["is_primary"] else 1,
            x["link_distance_km"],
            x["node_hash"],
        )
    )
''',
        '''    normalized.sort(
        key=lambda x: (
            _link_source_rank(x.get("source")),
            0 if x["is_primary"] else 1,
            x["link_distance_km"],
            x["node_hash"],
        )
    )
''',
        label="_normalize_link_options rank",
    )

    text = replace_once(
        text,
        '''    source_rank = {
        "station_link": 0,
        "edge_400m": 1,
        "edge_600m": 2,
        "local_rescue_node": 3,
        "fallback_nearest_node": 4,
    }

    def priority(item: dict[str, Any]) -> tuple[int, int, float, str]:
        source = str(item.get("source") or "")
        primary_rank = 0 if item.get("is_primary") else 1
        return (
            source_rank.get(source, 9),
            primary_rank,
            float(item["link_distance_km"]),
            str(item["node_hash"]),
        )
''',
        '''    def priority(item: dict[str, Any]) -> tuple[int, int, float, str]:
        primary_rank = 0 if item.get("is_primary") else 1
        return (
            _link_source_rank(item.get("source")),
            primary_rank,
            float(item["link_distance_km"]),
            str(item["node_hash"]),
        )
''',
        label="merge_link_options rank",
    )

    if "def build_station_anchor_node_options(" not in text:
        merge_anchor = '''def merge_link_options(
    base_options: list[dict[str, Any]],
    extra_options: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    best_by_node: dict[str, dict[str, Any]] = {}

    def priority(item: dict[str, Any]) -> tuple[int, int, float, str]:
        primary_rank = 0 if item.get("is_primary") else 1
        return (
            _link_source_rank(item.get("source")),
            primary_rank,
            float(item["link_distance_km"]),
            str(item["node_hash"]),
        )

    for item in list(base_options) + list(extra_options):
        node_hash = str(item["node_hash"])
        existing = best_by_node.get(node_hash)
        if existing is None or priority(item) < priority(existing):
            best_by_node[node_hash] = item

    merged = list(best_by_node.values())
    merged.sort(key=priority)
    return merged[: max(MAX_TOPOLOGY_LINK_OPTIONS_PER_STATION, LOCAL_RESCUE_NODE_LIMIT)]
'''
        merge_addition = '''

def build_station_anchor_node_options(
    candidate: Candidate,
    network: dict[str, Any],
    *,
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []

    options.extend(
        get_station_link_options_for_candidate(
            candidate,
            network,
            fallback_node_cache,
        )
    )

    for radius_m in NEARBY_EDGE_ATTACH_RADII_METERS:
        options.extend(
            get_nearby_edge_attach_options_for_candidate(
                candidate,
                network,
                nearby_edge_cache,
                radius_m,
            )
        )

    for radius_m in (400, 600):
        options.extend(
            get_nearby_edge_link_options_for_candidate(
                candidate,
                network,
                radius_m=radius_m,
                fallback_node_cache=fallback_node_cache,
            )
        )

    options.extend(
        get_local_rescue_node_options_for_candidate(
            candidate,
            network,
            rescue_node_cache,
        )
    )

    dedup: dict[str, dict[str, Any]] = {}
    for item in options:
        node_hash = str(item["node_hash"])
        existing = dedup.get(node_hash)
        if existing is None or _link_priority(item) < _link_priority(existing):
            dedup[node_hash] = {
                "node_hash": node_hash,
                "link_distance_km": float(item["link_distance_km"]),
                "is_primary": bool(item.get("is_primary")),
                "node_lon": float(item["node_lon"]),
                "node_lat": float(item["node_lat"]),
                "source": item.get("source") or "station_link",
            }

    result = list(dedup.values())
    result.sort(key=_link_priority)
    return result[:ANCHOR_NODE_REPAIR_MAX_OPTIONS]
'''
        text = insert_after(text, merge_anchor, merge_addition, label="build_station_anchor_node_options")

    if "def _evaluate_node_anchor_override_for_stop(" not in text:
        compute_anchor = "def compute_transition_cost(\n"
        idx = text.find(compute_anchor)
        if idx == -1:
            raise SystemExit("[patch] anchor not found for node repair helpers")
        node_helpers = '''def _evaluate_node_anchor_override_for_stop(
    *,
    stop_index: int,
    override_link: dict[str, Any],
    stops: list[dict[str, Any]],
    locked_candidates: list[Candidate | None],
    node_anchor_overrides: dict[int, dict[str, Any]],
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]],
) -> dict[str, Any]:
    temp_overrides = dict(node_anchor_overrides)
    temp_overrides[stop_index] = override_link

    evaluated_segments: list[int] = []
    if stop_index >= 1:
        evaluated_segments.append(stop_index)
    if stop_index + 1 < len(stops):
        evaluated_segments.append(stop_index + 1)

    total_score = 0.0
    success_count = 0
    failure_count = 0
    segment_results: list[dict[str, Any]] = []

    for segment_index in evaluated_segments:
        previous_candidate = locked_candidates[segment_index - 1]
        current_candidate = locked_candidates[segment_index]

        pair_path = None
        if previous_candidate is not None and current_candidate is not None:
            start_override = temp_overrides.get(segment_index - 1)
            end_override = temp_overrides.get(segment_index)

            pair_path = build_topology_path_between_candidates(
                previous_stop=stops[segment_index - 1],
                current_stop=stops[segment_index],
                previous_candidate=previous_candidate,
                current_candidate=current_candidate,
                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
                nearby_edge_cache=nearby_edge_cache,
                start_link_options_override=[start_override] if start_override is not None else None,
                end_link_options_override=[end_override] if end_override is not None else None,
            )

        score = _segment_result_quality_score(pair_path)
        total_score += score

        if pair_path is None:
            failure_count += 1
        else:
            success_count += 1

        segment_results.append(
            {
                "segment_index": segment_index,
                "path_found": pair_path is not None,
                "render_method": pair_path.get("render_method") if pair_path else None,
                "search_mode": pair_path.get("search_mode") if pair_path else None,
                "segment_score": round(score, 4),
                "graph_distance_km": round(float(pair_path.get("graph_distance_km") or 0.0), 4)
                if pair_path is not None else None,
                "total_score_km": round(float(pair_path.get("total_score_km") or 0.0), 4)
                if pair_path is not None else None,
            }
        )

    total_score += max(0.0, float(override_link.get("link_distance_km") or 0.0) - 0.75) * 12.0
    total_score += _link_source_rank(override_link.get("source")) * 1.5

    return {
        "stop_index": stop_index,
        "override_link": override_link,
        "evaluated_segments": evaluated_segments,
        "segment_results": segment_results,
        "success_count": success_count,
        "failure_count": failure_count,
        "total_score": float(total_score),
    }


def choose_node_anchor_repair_for_stop(
    *,
    stop_index: int,
    segment_index: int,
    stops: list[dict[str, Any]],
    locked_candidates: list[Candidate | None],
    node_anchor_overrides: dict[int, dict[str, Any]],
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]],
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    logger_context = logger_context or {}

    if stop_index <= 0 or stop_index >= len(stops):
        return None
    if stop_index >= len(locked_candidates):
        return None

    current_candidate = locked_candidates[stop_index]
    previous_candidate = locked_candidates[stop_index - 1]
    if current_candidate is None or previous_candidate is None:
        return None

    node_options = build_station_anchor_node_options(
        current_candidate,
        network,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
        nearby_edge_cache=nearby_edge_cache,
    )
    if not node_options:
        return None

    baseline_override = node_anchor_overrides.get(stop_index)
    if baseline_override is None:
        baseline_override = node_options[0]

    baseline = _evaluate_node_anchor_override_for_stop(
        stop_index=stop_index,
        override_link=baseline_override,
        stops=stops,
        locked_candidates=locked_candidates,
        node_anchor_overrides=node_anchor_overrides,
        network=network,
        path_cache=path_cache,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
        nearby_edge_cache=nearby_edge_cache,
    )

    _anchor_console_log(
        "node repair start",
        {
            "segment_index": segment_index,
            "stop_index": stop_index,
            "station_name_raw": stops[stop_index].get("station_name_raw"),
            "station": _station_debug_label(current_candidate, stops[stop_index]),
            "candidate_nodes": len(node_options),
            "baseline_score": round(float(baseline["total_score"]), 4),
            "baseline_failures": baseline["failure_count"],
        },
    )

    best = baseline
    evaluations: list[dict[str, Any]] = []

    for option in node_options:
        evaluation = _evaluate_node_anchor_override_for_stop(
            stop_index=stop_index,
            override_link=option,
            stops=stops,
            locked_candidates=locked_candidates,
            node_anchor_overrides=node_anchor_overrides,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
            nearby_edge_cache=nearby_edge_cache,
        )

        item = {
            "node_hash": option["node_hash"],
            "source": option.get("source"),
            "link_distance_km": round(float(option.get("link_distance_km") or 0.0), 4),
            "total_score": round(float(evaluation["total_score"]), 4),
            "success_count": evaluation["success_count"],
            "failure_count": evaluation["failure_count"],
            "segment_results": evaluation["segment_results"],
        }
        evaluations.append(item)
        _anchor_console_log("node candidate checked", item)

        if float(evaluation["total_score"]) < float(best["total_score"]):
            best = evaluation

    improvement = float(baseline["total_score"]) - float(best["total_score"])
    accepted = (
        best["failure_count"] < baseline["failure_count"]
        or improvement >= ANCHOR_NODE_REPAIR_ACCEPT_DELTA
    )

    result = {
        "accepted": accepted,
        "segment_index": segment_index,
        "stop_index": stop_index,
        "station_name_raw": stops[stop_index].get("station_name_raw"),
        "station_id": int(current_candidate.station_id),
        "station_name": current_candidate.name,
        "before_node_hash": baseline_override.get("node_hash"),
        "after_node_hash": best["override_link"].get("node_hash"),
        "before_source": baseline_override.get("source"),
        "after_source": best["override_link"].get("source"),
        "before_score": round(float(baseline["total_score"]), 4),
        "after_score": round(float(best["total_score"]), 4),
        "improvement": round(improvement, 4),
        "before_failures": int(baseline["failure_count"]),
        "after_failures": int(best["failure_count"]),
        "evaluated_segments": best["evaluated_segments"],
        "node_evaluations": evaluations,
        "override_link": best["override_link"],
    }

    if diagnostics is not None:
        diagnostics.setdefault("anchor_node_repairs", [])
        diagnostics["anchor_node_repairs"].append(
            {k: v for k, v in result.items() if k != "override_link"}
        )

    if accepted:
        _anchor_console_log("node repair accepted", {k: v for k, v in result.items() if k != "override_link"})
        log_event(
            "info",
            "anchor_node_repair_accepted",
            **{k: v for k, v in result.items() if k != "override_link"},
            **logger_context,
        )
    else:
        _anchor_console_log("node repair rejected", {k: v for k, v in result.items() if k != "override_link"})

    return result


'''
        text = text[:idx] + node_helpers + text[idx:]

    text = replace_once(
        text,
        '''def _evaluate_anchor_override_for_stop(
    *,
    stop_index: int,
    override_candidate: Candidate,
    locked_candidates: list[Candidate | None],
    stops: list[dict[str, Any]],
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
''',
        '''def _evaluate_anchor_override_for_stop(
    *,
    stop_index: int,
    override_candidate: Candidate,
    locked_candidates: list[Candidate | None],
    stops: list[dict[str, Any]],
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]],
) -> dict[str, Any]:
''',
        label="_evaluate_anchor_override_for_stop signature",
    )

    text = text.replace(
        '''                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
            )''',
        '''                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
                nearby_edge_cache=nearby_edge_cache,
            )''',
        3,
    )

    text = replace_once(
        text,
        '''def choose_anchor_repair_for_stop(
    *,
    stop_index: int,
    segment_index: int,
    stops: list[dict[str, Any]],
    locked_candidates: list[Candidate | None],
    candidates_per_stop: list[list[Candidate]],
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
''',
        '''def choose_anchor_repair_for_stop(
    *,
    stop_index: int,
    segment_index: int,
    stops: list[dict[str, Any]],
    locked_candidates: list[Candidate | None],
    candidates_per_stop: list[list[Candidate]],
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]],
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
''',
        label="choose_anchor_repair_for_stop signature",
    )

    text = text.replace(
        '''        network=network,
        path_cache=path_cache,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
    )''',
        '''        network=network,
        path_cache=path_cache,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
        nearby_edge_cache=nearby_edge_cache,
    )''',
        2,
    )

    text = replace_once(
        text,
        '''def build_topology_path_between_candidates(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
) -> dict[str, Any] | None:
''',
        '''def build_topology_path_between_candidates(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    rescue_node_cache: dict[int, list[dict[str, Any]]],
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]],
    start_link_options_override: list[dict[str, Any]] | None = None,
    end_link_options_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
''',
        label="build_topology_path_between_candidates signature",
    )

    text = replace_once(
        text,
        '''    base_start_links = get_station_link_options_for_candidate(
        previous_candidate,
        network,
        fallback_node_cache,
    )
    base_end_links = get_station_link_options_for_candidate(
        current_candidate,
        network,
        fallback_node_cache,
    )
''',
        '''    base_start_links = (
        _normalize_link_options(start_link_options_override)
        if start_link_options_override is not None
        else get_station_link_options_for_candidate(
            previous_candidate,
            network,
            fallback_node_cache,
        )
    )
    base_end_links = (
        _normalize_link_options(end_link_options_override)
        if end_link_options_override is not None
        else get_station_link_options_for_candidate(
            current_candidate,
            network,
            fallback_node_cache,
        )
    )
''',
        label="build_topology_path_between_candidates base links",
    )

    text = replace_once(
        text,
        '''    if direct_result is not None:
        return direct_result

    nearby_400_start = merge_link_options(
        base_start_links,
''',
        '''    if direct_result is not None:
        return direct_result

    edge_attach_400_start = merge_link_options(
        base_start_links,
        get_nearby_edge_attach_options_for_candidate(
            previous_candidate,
            network,
            nearby_edge_cache,
            400,
        ),
    )
    edge_attach_400_end = merge_link_options(
        base_end_links,
        get_nearby_edge_attach_options_for_candidate(
            current_candidate,
            network,
            nearby_edge_cache,
            400,
        ),
    )

    edge_attach_400_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=edge_attach_400_start,
        end_links=edge_attach_400_end,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="station_links_plus_edge_attach_400m",
    )
    if edge_attach_400_result is not None:
        return edge_attach_400_result

    edge_attach_600_start = merge_link_options(
        edge_attach_400_start,
        get_nearby_edge_attach_options_for_candidate(
            previous_candidate,
            network,
            nearby_edge_cache,
            600,
        ),
    )
    edge_attach_600_end = merge_link_options(
        edge_attach_400_end,
        get_nearby_edge_attach_options_for_candidate(
            current_candidate,
            network,
            nearby_edge_cache,
            600,
        ),
    )

    edge_attach_600_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=edge_attach_600_start,
        end_links=edge_attach_600_end,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="station_links_plus_edge_attach_600m",
    )
    if edge_attach_600_result is not None:
        return edge_attach_600_result

    nearby_400_start = merge_link_options(
        edge_attach_600_start,
''',
        label="edge attach phases insertion",
    )

    text = replace_once(
        text,
        '''    nearby_400_end = merge_link_options(
        base_end_links,
''',
        '''    nearby_400_end = merge_link_options(
        edge_attach_600_end,
''',
        label="nearby_400_end base",
    )

    text = replace_once(
        text,
        '''    path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    fallback_node_cache: dict[int, list[dict[str, Any]]] = {}
    rescue_node_cache: dict[int, list[dict[str, Any]]] = {}
''',
        '''    path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    fallback_node_cache: dict[int, list[dict[str, Any]]] = {}
    rescue_node_cache: dict[int, list[dict[str, Any]]] = {}
    nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}
    node_anchor_overrides: dict[int, dict[str, Any]] = {}
''',
        label="geometry caches",
    )

    text = replace_once(
        text,
        '''        pair_path = build_topology_path_between_candidates(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
        )
''',
        '''        pair_path = build_topology_path_between_candidates(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
            nearby_edge_cache=nearby_edge_cache,
            start_link_options_override=[node_anchor_overrides[index - 1]]
            if (index - 1) in node_anchor_overrides else None,
            end_link_options_override=[node_anchor_overrides[index]]
            if index in node_anchor_overrides else None,
        )
''',
        label="geometry initial pair_path call",
    )

    text = replace_once(
        text,
        '''        if pair_path is None:
            repair_result = choose_anchor_repair_for_stop(
''',
        '''        if pair_path is None:
            node_repair_result = choose_node_anchor_repair_for_stop(
                stop_index=index,
                segment_index=index,
                stops=stops,
                locked_candidates=locked_candidates,
                node_anchor_overrides=node_anchor_overrides,
                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
                nearby_edge_cache=nearby_edge_cache,
                diagnostics=diagnostics,
                logger_context=logger_context,
            )
            if node_repair_result is not None and node_repair_result.get("accepted"):
                node_anchor_overrides[int(node_repair_result["stop_index"])] = node_repair_result["override_link"]

                pair_path = build_topology_path_between_candidates(
                    previous_stop=previous_stop,
                    current_stop=current_stop,
                    previous_candidate=previous_candidate,
                    current_candidate=current_candidate,
                    network=network,
                    path_cache=path_cache,
                    fallback_node_cache=fallback_node_cache,
                    rescue_node_cache=rescue_node_cache,
                    nearby_edge_cache=nearby_edge_cache,
                    start_link_options_override=[node_anchor_overrides[index - 1]]
                    if (index - 1) in node_anchor_overrides else None,
                    end_link_options_override=[node_anchor_overrides[index]]
                    if index in node_anchor_overrides else None,
                )

                if pair_path is not None:
                    pair_path["anchor_repair_applied"] = True
                    pair_path["anchor_repair_summary"] = {
                        "repair_type": "node_anchor_override",
                        "stop_index": node_repair_result.get("stop_index"),
                        "station_id": node_repair_result.get("station_id"),
                        "station_name": node_repair_result.get("station_name"),
                        "before_node_hash": node_repair_result.get("before_node_hash"),
                        "after_node_hash": node_repair_result.get("after_node_hash"),
                        "before_source": node_repair_result.get("before_source"),
                        "after_source": node_repair_result.get("after_source"),
                        "before_score": node_repair_result.get("before_score"),
                        "after_score": node_repair_result.get("after_score"),
                        "improvement": node_repair_result.get("improvement"),
                    }

            repair_result = choose_anchor_repair_for_stop(
''',
        label="node repair before station repair",
    )

    text = replace_once(
        text,
        '''                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
                diagnostics=diagnostics,
                logger_context=logger_context,
            )
''',
        '''                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
                nearby_edge_cache=nearby_edge_cache,
                diagnostics=diagnostics,
                logger_context=logger_context,
            )
''',
        label="station repair call includes nearby_edge_cache",
    )

    text = replace_once(
        text,
        '''                    pair_path = build_topology_path_between_candidates(
                        previous_stop=previous_stop,
                        current_stop=current_stop,
                        previous_candidate=previous_candidate,
                        current_candidate=current_candidate,
                        network=network,
                        path_cache=path_cache,
                        fallback_node_cache=fallback_node_cache,
                        rescue_node_cache=rescue_node_cache,
                    )
''',
        '''                    pair_path = build_topology_path_between_candidates(
                        previous_stop=previous_stop,
                        current_stop=current_stop,
                        previous_candidate=previous_candidate,
                        current_candidate=current_candidate,
                        network=network,
                        path_cache=path_cache,
                        fallback_node_cache=fallback_node_cache,
                        rescue_node_cache=rescue_node_cache,
                        nearby_edge_cache=nearby_edge_cache,
                        start_link_options_override=[node_anchor_overrides[index - 1]]
                        if (index - 1) in node_anchor_overrides else None,
                        end_link_options_override=[node_anchor_overrides[index]]
                        if index in node_anchor_overrides else None,
                    )
''',
        label="station repair rebuild pair_path",
    )

    target.write_text(text, encoding="utf-8")
    print(f"[patch] updated: {target}")


if __name__ == "__main__":
    main()
