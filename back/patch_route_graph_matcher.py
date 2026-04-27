from pathlib import Path
import re

path = Path("app/route_graph_matcher.py")
text = path.read_text(encoding="utf-8")


def replace_function(src: str, function_name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"^def {re.escape(function_name)}\([\s\S]*?\n(?=def |\n@dataclass|\Z)",
        re.MULTILINE,
    )
    new_src, count = pattern.subn(replacement.rstrip() + "\n\n", src, count=1)
    if count != 1:
        raise RuntimeError(f"Не удалось заменить функцию {function_name}, count={count}")
    return new_src


# 1. constants
if "SYNTHETIC_GAP_MAX_KM" not in text:
    text = text.replace(
        "ANCHOR_NODE_REPAIR_FAIL_SCORE = 1_000_000.0\n",
        """ANCHOR_NODE_REPAIR_FAIL_SCORE = 1_000_000.0

SYNTHETIC_GAP_MAX_KM = 1.2
SYNTHETIC_GAP_PAIR_LIMIT = 16
SYNTHETIC_GAP_EXTRA_PENALTY = 1.5
""",
    )


# 2. _station_debug_label forward ref
text = re.sub(
    r'def _station_debug_label\(candidate: Candidate \| None, stop: dict\[str, Any\] \| None = None\) -> str:',
    'def _station_debug_label(candidate: "Candidate | None", stop: dict[str, Any] | None = None) -> str:',
    text,
)


# 3. _anchor_search_mode_penalty
text = replace_function(
    text,
    "_anchor_search_mode_penalty",
    '''
def _anchor_search_mode_penalty(search_mode: str | None) -> float:
    penalties = {
        "station_links_only": 0.0,
        "station_links_plus_nearby_edges_400m": 4.0,
        "station_links_plus_nearby_edges_600m": 7.0,
        "station_links_plus_local_rescue": 12.0,
        "synthetic_gap_bridge_last_resort": 16.0,
        "isolated_component_bridge_last_resort": 35.0,
        "rejected_absurd_detour_debug": 80.0,
    }
    return penalties.get(str(search_mode or ""), 18.0)
''',
)


# 4. _anchor_render_method_penalty
text = replace_function(
    text,
    "_anchor_render_method_penalty",
    '''
def _anchor_render_method_penalty(render_method: str | None) -> float:
    penalties = {
        "topology_graph_path": 0.0,
        "topology_synthetic_gap_bridge": 12.0,
        "topology_component_bridge": 20.0,
        "rejected_absurd_detour": 120.0,
        "fallback_straight": 300.0,
    }
    return penalties.get(str(render_method or ""), 120.0)
''',
)


# 5. helper before _evaluate_topology_link_pair_options
helper = '''
def _build_pair_path_coordinates(
    previous_candidate: "Candidate",
    current_candidate: "Candidate",
    start_link: dict[str, Any],
    end_link: dict[str, Any],
    graph_coords: list[list[float]] | None,
) -> list[list[float]]:
    sequences: list[list[list[float]]] = []

    connector_start = [
        [previous_candidate.lon, previous_candidate.lat],
        [float(start_link["node_lon"]), float(start_link["node_lat"])],
    ]
    if connector_start[0] != connector_start[1]:
        sequences.append(connector_start)

    if graph_coords:
        sequences.append(graph_coords)

    connector_end = [
        [float(end_link["node_lon"]), float(end_link["node_lat"])],
        [current_candidate.lon, current_candidate.lat],
    ]
    if connector_end[0] != connector_end[1]:
        sequences.append(connector_end)

    coordinates = merge_coordinate_sequences(sequences)
    if len(coordinates) < 2:
        coordinates = [
            [previous_candidate.lon, previous_candidate.lat],
            [current_candidate.lon, current_candidate.lat],
        ]

    return coordinates
'''

if "def _build_pair_path_coordinates(" not in text:
    text = text.replace(
        "\ndef _evaluate_topology_link_pair_options(",
        "\n" + helper.rstrip() + "\n\n\ndef _evaluate_topology_link_pair_options(",
    )


# 6. replace _evaluate_topology_link_pair_options
text = replace_function(
    text,
    "_evaluate_topology_link_pair_options",
    '''
def _evaluate_topology_link_pair_options(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    start_links: list[dict[str, Any]],
    end_links: list[dict[str, Any]],
    adjacency: dict[str, list[dict[str, Any]]],
    node_coords: dict[str, dict[str, float]],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    search_mode: str,
    allow_rejected_detour_fallback: bool = False,
) -> dict[str, Any] | None:
    best_option: dict[str, Any] | None = None
    best_score = math.inf

    best_rejected_option: dict[str, Any] | None = None
    best_rejected_score = math.inf

    seen_pairs: set[tuple[str, str]] = set()

    for start_link in start_links:
        for end_link in end_links:
            pair_key = (str(start_link["node_hash"]), str(end_link["node_hash"]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            graph_path = dijkstra_topology_path(
                adjacency=adjacency,
                node_coords=node_coords,
                start_node_hash=str(start_link["node_hash"]),
                end_node_hash=str(end_link["node_hash"]),
                path_cache=path_cache,
            )
            if graph_path is None:
                continue

            render_total_distance_km = (
                float(graph_path["distance_km"])
                + float(start_link["link_distance_km"])
                + float(end_link["link_distance_km"])
            )

            transition_cost, transition_diag = compute_transition_cost(
                previous_stop=previous_stop,
                next_stop=current_stop,
                render_total_distance_km=render_total_distance_km,
                hop_count=int(graph_path.get("hop_count") or 0),
            )

            connector_penalty = (
                float(start_link["link_distance_km"])
                + float(end_link["link_distance_km"])
            ) * 4.0

            source_penalty = 0.0
            if start_link.get("source") == "local_rescue_node":
                source_penalty += LOCAL_RESCUE_EXTRA_PENALTY
            elif str(start_link.get("source") or "").startswith("edge_"):
                source_penalty += 0.45
            elif start_link.get("source") != "station_link":
                source_penalty += 0.8
            elif not start_link.get("is_primary"):
                source_penalty += 0.15

            if end_link.get("source") == "local_rescue_node":
                source_penalty += LOCAL_RESCUE_EXTRA_PENALTY
            elif str(end_link.get("source") or "").startswith("edge_"):
                source_penalty += 0.45
            elif end_link.get("source") != "station_link":
                source_penalty += 0.8
            elif not end_link.get("is_primary"):
                source_penalty += 0.15

            coordinates = _build_pair_path_coordinates(
                previous_candidate=previous_candidate,
                current_candidate=current_candidate,
                start_link=start_link,
                end_link=end_link,
                graph_coords=graph_path.get("coordinates") or [],
            )

            if transition_cost is None:
                rejected_reason = (transition_diag or {}).get("rejected_reason")

                if allow_rejected_detour_fallback and rejected_reason == "graph_path_absurd_detour":
                    rejected_score = (
                        render_total_distance_km
                        + connector_penalty
                        + source_penalty
                    )

                    if rejected_score < best_rejected_score:
                        best_rejected_score = rejected_score
                        best_rejected_option = {
                            "render_method": "rejected_absurd_detour",
                            "search_mode": "rejected_absurd_detour_debug",
                            "start_link": start_link,
                            "end_link": end_link,
                            "path": graph_path,
                            "coordinates": coordinates,
                            "graph_distance_km": float(graph_path["distance_km"]),
                            "connector_start_km": float(start_link["link_distance_km"]),
                            "connector_end_km": float(end_link["link_distance_km"]),
                            "total_score_km": render_total_distance_km,
                            "graph_edge_count": len(graph_path.get("edge_chain") or []),
                            "transition_cost": None,
                            "transition_diag": {
                                **(transition_diag or {}),
                                "connector_start_km": float(start_link["link_distance_km"]),
                                "connector_end_km": float(end_link["link_distance_km"]),
                                "render_total_distance_km": render_total_distance_km,
                                "forced_debug_render": True,
                            },
                            "final_score": rejected_score,
                        }
                continue

            final_score = float(transition_cost) + connector_penalty + source_penalty

            if final_score < best_score:
                best_score = final_score
                best_option = {
                    "render_method": "topology_graph_path",
                    "search_mode": search_mode,
                    "start_link": start_link,
                    "end_link": end_link,
                    "path": graph_path,
                    "coordinates": coordinates,
                    "graph_distance_km": float(graph_path["distance_km"]),
                    "connector_start_km": float(start_link["link_distance_km"]),
                    "connector_end_km": float(end_link["link_distance_km"]),
                    "total_score_km": render_total_distance_km,
                    "graph_edge_count": len(graph_path.get("edge_chain") or []),
                    "transition_cost": float(transition_cost),
                    "transition_diag": {
                        **transition_diag,
                        "connector_start_km": float(start_link["link_distance_km"]),
                        "connector_end_km": float(end_link["link_distance_km"]),
                        "render_total_distance_km": render_total_distance_km,
                    },
                    "final_score": final_score,
                }

    if best_option is not None:
        return best_option

    if allow_rejected_detour_fallback and best_rejected_option is not None:
        return best_rejected_option

    return None
''',
)


# 7. add try_synthetic_gap_bridge_rescue after try_isolated_component_bridge_rescue
synthetic_func = '''
def try_synthetic_gap_bridge_rescue(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    all_start_links: list[dict[str, Any]],
    all_end_links: list[dict[str, Any]],
) -> dict[str, Any] | None:
    best_result: dict[str, Any] | None = None
    best_score = math.inf

    candidate_pairs: list[tuple[float, dict[str, Any], dict[str, Any]]] = []

    for start_link in all_start_links:
        for end_link in all_end_links:
            start_node_hash = str(start_link["node_hash"])
            end_node_hash = str(end_link["node_hash"])

            if start_node_hash == end_node_hash:
                continue

            gap_km = haversine_km(
                float(start_link["node_lon"]),
                float(start_link["node_lat"]),
                float(end_link["node_lon"]),
                float(end_link["node_lat"]),
            )

            if gap_km > SYNTHETIC_GAP_MAX_KM:
                continue

            candidate_pairs.append((gap_km, start_link, end_link))

    candidate_pairs.sort(
        key=lambda item: (
            item[0],
            float(item[1].get("link_distance_km") or 999999.0),
            float(item[2].get("link_distance_km") or 999999.0),
            str(item[1].get("node_hash") or ""),
            str(item[2].get("node_hash") or ""),
        )
    )

    for gap_km, start_link, end_link in candidate_pairs[:SYNTHETIC_GAP_PAIR_LIMIT]:
        render_total_distance_km = (
            float(start_link["link_distance_km"])
            + gap_km
            + float(end_link["link_distance_km"])
        )

        transition_cost, transition_diag = compute_transition_cost(
            previous_stop=previous_stop,
            next_stop=current_stop,
            render_total_distance_km=render_total_distance_km,
            hop_count=1,
        )
        if transition_cost is None:
            continue

        connector_penalty = (
            float(start_link["link_distance_km"])
            + float(end_link["link_distance_km"])
        ) * 4.0

        source_penalty = 0.0
        if start_link.get("source") != "station_link":
            source_penalty += 0.8
        if end_link.get("source") != "station_link":
            source_penalty += 0.8

        final_score = (
            float(transition_cost)
            + connector_penalty
            + source_penalty
            + SYNTHETIC_GAP_EXTRA_PENALTY
            + gap_km * 6.0
        )

        coordinates = merge_coordinate_sequences(
            [
                [
                    [previous_candidate.lon, previous_candidate.lat],
                    [float(start_link["node_lon"]), float(start_link["node_lat"])],
                ],
                [
                    [float(start_link["node_lon"]), float(start_link["node_lat"])],
                    [float(end_link["node_lon"]), float(end_link["node_lat"])],
                ],
                [
                    [float(end_link["node_lon"]), float(end_link["node_lat"])],
                    [current_candidate.lon, current_candidate.lat],
                ],
            ]
        )

        if len(coordinates) < 2:
            continue

        if final_score < best_score:
            best_score = final_score
            best_result = {
                "render_method": "topology_synthetic_gap_bridge",
                "search_mode": "synthetic_gap_bridge_last_resort",
                "start_link": start_link,
                "end_link": end_link,
                "path": {
                    "edge_chain": [],
                    "coordinates": [],
                    "distance_km": 0.0,
                    "hop_count": 0,
                },
                "coordinates": coordinates,
                "graph_distance_km": gap_km,
                "connector_start_km": float(start_link["link_distance_km"]),
                "connector_end_km": float(end_link["link_distance_km"]),
                "bridge_gap_km": gap_km,
                "total_score_km": render_total_distance_km,
                "graph_edge_count": 0,
                "transition_cost": float(transition_cost),
                "transition_diag": {
                    **transition_diag,
                    "connector_start_km": float(start_link["link_distance_km"]),
                    "connector_end_km": float(end_link["link_distance_km"]),
                    "bridge_gap_km": gap_km,
                    "render_total_distance_km": render_total_distance_km,
                    "synthetic_bridge": True,
                },
                "final_score": final_score,
            }

    return best_result
'''

if "def try_synthetic_gap_bridge_rescue(" not in text:
    marker = "\ndef _normalize_link_options("
    text = text.replace(marker, "\n" + synthetic_func.rstrip() + "\n\n" + marker.lstrip(), 1)


# 8. replace build_topology_path_between_candidates
text = replace_function(
    text,
    "build_topology_path_between_candidates",
    '''
def build_topology_path_between_candidates(
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
    adjacency = network["adjacency"]
    node_coords = network["node_coords"]

    base_start_links = (
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

    direct_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=base_start_links,
        end_links=base_end_links,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="station_links_only",
    )
    if direct_result is not None:
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
        get_nearby_edge_link_options_for_candidate(
            previous_candidate,
            network,
            radius_m=400,
            fallback_node_cache=fallback_node_cache,
        ),
    )
    nearby_400_end = merge_link_options(
        edge_attach_600_end,
        get_nearby_edge_link_options_for_candidate(
            current_candidate,
            network,
            radius_m=400,
            fallback_node_cache=fallback_node_cache,
        ),
    )

    nearby_400_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=nearby_400_start,
        end_links=nearby_400_end,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="station_links_plus_nearby_edges_400m",
    )
    if nearby_400_result is not None:
        return nearby_400_result

    nearby_600_start = merge_link_options(
        nearby_400_start,
        get_nearby_edge_link_options_for_candidate(
            previous_candidate,
            network,
            radius_m=600,
            fallback_node_cache=fallback_node_cache,
        ),
    )
    nearby_600_end = merge_link_options(
        nearby_400_end,
        get_nearby_edge_link_options_for_candidate(
            current_candidate,
            network,
            radius_m=600,
            fallback_node_cache=fallback_node_cache,
        ),
    )

    nearby_600_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=nearby_600_start,
        end_links=nearby_600_end,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="station_links_plus_nearby_edges_600m",
    )
    if nearby_600_result is not None:
        return nearby_600_result

    rescue_start_links = merge_link_options(
        nearby_600_start,
        get_local_rescue_node_options_for_candidate(
            previous_candidate,
            network,
            rescue_node_cache,
        ),
    )
    rescue_end_links = merge_link_options(
        nearby_600_end,
        get_local_rescue_node_options_for_candidate(
            current_candidate,
            network,
            rescue_node_cache,
        ),
    )

    rescue_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=rescue_start_links,
        end_links=rescue_end_links,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="station_links_plus_local_rescue",
    )
    if rescue_result is not None:
        return rescue_result

    bridge_result = try_isolated_component_bridge_rescue(
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
        return bridge_result

    synthetic_result = try_synthetic_gap_bridge_rescue(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        all_start_links=rescue_start_links,
        all_end_links=rescue_end_links,
    )
    if synthetic_result is not None:
        _anchor_console_log(
            "synthetic bridge selected",
            {
                "from_station": _station_debug_label(previous_candidate, previous_stop),
                "to_station": _station_debug_label(current_candidate, current_stop),
                "start_node_hash": synthetic_result.get("start_link", {}).get("node_hash"),
                "end_node_hash": synthetic_result.get("end_link", {}).get("node_hash"),
                "bridge_gap_km": round(float(synthetic_result.get("bridge_gap_km") or 0.0), 4),
                "total_score_km": round(float(synthetic_result.get("total_score_km") or 0.0), 4),
            },
        )
        return synthetic_result

    rejected_absurd_detour_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=rescue_start_links,
        end_links=rescue_end_links,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="rejected_absurd_detour_debug",
        allow_rejected_detour_fallback=True,
    )
    if rejected_absurd_detour_result is not None:
        _anchor_console_log(
            "rejected absurd detour selected for rendering",
            {
                "from_station": _station_debug_label(previous_candidate, previous_stop),
                "to_station": _station_debug_label(current_candidate, current_stop),
                "graph_distance_km": round(float(rejected_absurd_detour_result.get("graph_distance_km") or 0.0), 4),
                "total_score_km": round(float(rejected_absurd_detour_result.get("total_score_km") or 0.0), 4),
                "start_node_hash": rejected_absurd_detour_result.get("start_link", {}).get("node_hash"),
                "end_node_hash": rejected_absurd_detour_result.get("end_link", {}).get("node_hash"),
            },
        )
        return rejected_absurd_detour_result

    return None
''',
)


# 9. _topology_result_source_rank
text = replace_function(
    text,
    "_topology_result_source_rank",
    '''
def _topology_result_source_rank(source: str | None) -> int:
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
)


path.write_text(text, encoding="utf-8")
print("OK: app/route_graph_matcher.py patched")