from __future__ import annotations

import ast
import re
from pathlib import Path


# Запускать из папки back
TARGET = Path("app/route_graph_matcher.py")


FUNCTIONS_TO_REMOVE = {
    "_anchor_console_log",
    "_station_debug_label",
    "_anchor_search_mode_penalty",
    "_anchor_render_method_penalty",
    "_segment_result_quality_score",
    "_candidate_override_list_for_stop",
    "_evaluate_anchor_override_for_stop",
    "choose_anchor_repair_for_stop",
    "choose_bidirectional_station_repair_for_segment",
    "get_nearby_edge_link_options_for_candidate",
    "get_local_rescue_node_options_for_candidate",
    "load_nearby_edge_attach_options",
    "get_nearby_edge_attach_options_for_candidate",
    "merge_link_options",
    "build_station_anchor_node_options",
    "_evaluate_node_anchor_override_for_stop",
    "choose_node_anchor_repair_for_stop",
    "choose_bidirectional_node_anchor_repair_for_segment",
    "try_synthetic_gap_bridge_rescue",
}


CONSTANTS_TO_REMOVE = {
    "ANCHOR_REPAIR_MAX_CANDIDATES_PER_STOP",
    "ANCHOR_REPAIR_SCORE_FAILURE",
    "ANCHOR_REPAIR_ACCEPT_DELTA",
    "ANCHOR_NODE_REPAIR_MAX_OPTIONS",
    "ANCHOR_NODE_REPAIR_ACCEPT_DELTA",
    "ANCHOR_NODE_REPAIR_FAIL_SCORE",
    "SYNTHETIC_GAP_MAX_KM",
    "SYNTHETIC_GAP_PAIR_LIMIT",
    "SYNTHETIC_GAP_EXTRA_PENALTY",
    "NEARBY_EDGE_ATTACH_RADII_METERS",
    "NEARBY_EDGE_ATTACH_LIMIT_PER_RADIUS",
    "NEARBY_EDGE_ATTACH_MAX_ENTRY_KM",
    "LOCAL_RESCUE_NODE_RADIUS_KM",
    "LOCAL_RESCUE_NODE_LIMIT",
    "LOCAL_RESCUE_EXTRA_PENALTY",
}


NEW_LINK_SOURCE_RANK = '''def _link_source_rank(source: str | None) -> int:
    source = str(source or "")
    ranks = {
        "station_link": 0,
        "fallback_nearest_node": 1,
    }
    return ranks.get(source, 9)
'''


NEW_COMPUTE_TRANSITION_COST = '''def compute_transition_cost(
    previous_stop: dict[str, Any],
    next_stop: dict[str, Any],
    render_total_distance_km: float | None,
    hop_count: int | None,
) -> tuple[float | None, dict[str, Any]]:
    delta_rzd = None
    current_distance = safe_float(previous_stop.get("distance_km"))
    next_distance = safe_float(next_stop.get("distance_km"))

    if current_distance is not None and next_distance is not None:
        delta_rzd = max(0.0, next_distance - current_distance)

    if render_total_distance_km is None:
        return None, {
            "delta_rzd_km": delta_rzd,
            "graph_distance_km": None,
            "distance_error_km": None,
            "relative_error": None,
            "hop_count": hop_count,
            "rejected_reason": "no_graph_path",
        }

    graph_distance = float(render_total_distance_km)
    hop_count = int(hop_count or 0)

    if delta_rzd is None:
        cost = graph_distance * 0.03 + hop_count * 0.02
        return cost, {
            "delta_rzd_km": None,
            "graph_distance_km": graph_distance,
            "distance_error_km": None,
            "relative_error": None,
            "hop_count": hop_count,
        }

    if delta_rzd <= 1.0:
        if graph_distance <= 2.0:
            cost = graph_distance * 0.05 + hop_count * 0.05
        else:
            return None, {
                "delta_rzd_km": delta_rzd,
                "graph_distance_km": graph_distance,
                "distance_error_km": abs(graph_distance - delta_rzd),
                "relative_error": 0.0 if delta_rzd == 0 else abs(graph_distance - delta_rzd) / max(delta_rzd, 1.0),
                "hop_count": hop_count,
                "rejected_reason": "tiny_delta_but_long_graph_path",
            }

        return cost, {
            "delta_rzd_km": delta_rzd,
            "graph_distance_km": graph_distance,
            "distance_error_km": abs(graph_distance - delta_rzd),
            "relative_error": 0.0 if delta_rzd == 0 else abs(graph_distance - delta_rzd) / max(delta_rzd, 1.0),
            "hop_count": hop_count,
        }

    if delta_rzd >= 50.0 and graph_distance < delta_rzd * 0.20:
        return None, {
            "delta_rzd_km": delta_rzd,
            "graph_distance_km": graph_distance,
            "distance_error_km": abs(graph_distance - delta_rzd),
            "relative_error": abs(graph_distance - delta_rzd) / max(delta_rzd, 1.0),
            "hop_count": hop_count,
            "rejected_reason": "graph_path_too_short",
        }

    distance_error = abs(graph_distance - delta_rzd)
    relative_error = distance_error / max(delta_rzd, 10.0)
    cost = distance_error * 0.09 + relative_error * 12.0 + max(0, hop_count - 1) * 0.12

    return cost, {
        "delta_rzd_km": delta_rzd,
        "graph_distance_km": graph_distance,
        "distance_error_km": distance_error,
        "relative_error": relative_error,
        "hop_count": hop_count,
    }
'''


NEW_EVALUATE_TOPOLOGY_LINK_PAIR_OPTIONS = '''def _evaluate_topology_link_pair_options(
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
) -> dict[str, Any] | None:
    best_option: dict[str, Any] | None = None
    best_score = math.inf

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
            if transition_cost is None:
                continue

            connector_penalty = (
                float(start_link["link_distance_km"])
                + float(end_link["link_distance_km"])
            ) * 4.0

            source_penalty = 0.0

            if start_link.get("source") != "station_link":
                source_penalty += 0.8
            elif not start_link.get("is_primary"):
                source_penalty += 0.15

            if end_link.get("source") != "station_link":
                source_penalty += 0.8
            elif not end_link.get("is_primary"):
                source_penalty += 0.15

            final_score = float(transition_cost) + connector_penalty + source_penalty

            coordinates = _build_pair_path_coordinates(
                previous_candidate=previous_candidate,
                current_candidate=current_candidate,
                start_link=start_link,
                end_link=end_link,
                graph_coords=graph_path.get("coordinates") or [],
            )

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

    return best_option
'''


NEW_TOPOLOGY_RESULT_SOURCE_RANK = '''def _topology_result_source_rank(source: str | None) -> int:
    source = source or ""

    if source == "station_link":
        return 0
    if source == "fallback_nearest_node":
        return 1

    return 9
'''


NEW_BUILD_TOPOLOGY_PATH_BETWEEN_CANDIDATES = '''def build_topology_path_between_candidates(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    adjacency = network["adjacency"]
    node_coords = network["node_coords"]

    start_links = get_station_link_options_for_candidate(
        previous_candidate,
        network,
        fallback_node_cache,
    )
    end_links = get_station_link_options_for_candidate(
        current_candidate,
        network,
        fallback_node_cache,
    )

    direct_result = _evaluate_topology_link_pair_options(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        start_links=start_links,
        end_links=end_links,
        adjacency=adjacency,
        node_coords=node_coords,
        path_cache=path_cache,
        search_mode="station_links_only",
    )
    if direct_result is not None:
        return direct_result

    bridge_result = try_isolated_component_bridge_rescue(
        previous_stop=previous_stop,
        current_stop=current_stop,
        previous_candidate=previous_candidate,
        current_candidate=current_candidate,
        network=network,
        path_cache=path_cache,
        all_start_links=start_links,
        all_end_links=end_links,
    )
    if bridge_result is not None:
        return bridge_result

    return None
'''


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0

    for line in text.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)

    return offsets


def replace_top_level_function(text: str, name: str, replacement: str) -> str:
    tree = ast.parse(text)
    offsets = _line_offsets(text)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = offsets[node.lineno - 1]
            end = offsets[node.end_lineno]
            return text[:start] + replacement.rstrip() + "\n\n" + text[end:]

    raise RuntimeError(f"Function not found: {name}")


def remove_top_level_functions(text: str, names: set[str]) -> str:
    tree = ast.parse(text)
    offsets = _line_offsets(text)

    ranges: list[tuple[int, int, str]] = []

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            ranges.append((offsets[node.lineno - 1], offsets[node.end_lineno], node.name))

    found = {name for _, _, name in ranges}
    missing = names - found

    if missing:
        print("WARN: functions not found, maybe already removed:")
        for name in sorted(missing):
            print(f"  - {name}")

    for start, end, _name in sorted(ranges, reverse=True):
        text = text[:start] + text[end:]

    return text


def remove_constants(text: str, names: set[str]) -> str:
    for name in sorted(names, key=len, reverse=True):
        text = re.sub(
            rf"^{re.escape(name)}\\s*=.*\\n",
            "",
            text,
            flags=re.MULTILINE,
        )

    return text


def patch_build_route_geometry_between_locked_candidates(text: str) -> str:
    name = "build_route_geometry_between_locked_candidates"
    tree = ast.parse(text)
    offsets = _line_offsets(text)

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = offsets[node.lineno - 1]
            end = offsets[node.end_lineno]
            func = text[start:end]
            break
    else:
        raise RuntimeError(f"Function not found: {name}")

    lines = func.splitlines(keepends=True)

    # 1. Удаляем локальные cache/override-переменные repair-логики.
    cleaned_lines: list[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped == "rescue_node_cache: dict[int, list[dict[str, Any]]] = {}":
            continue
        if stripped == "nearby_edge_cache: dict[tuple[int, int], list[dict[str, Any]]] = {}":
            continue
        if stripped == "node_anchor_overrides: dict[int, dict[str, Any]] = {}":
            continue

        cleaned_lines.append(line)

    lines = cleaned_lines

    # 2. Удаляем большой блок:
    #    if pair_path is None:
    #        node_repair_result = ...
    #        ...
    #        station repair ...
    #    до следующего "if pair_path is not None:"
    result_lines = []
    i = 0
    removed_repair_block = False

    while i < len(lines):
        line = lines[i]

        if line.startswith("        if pair_path is None:"):
            j = i + 1

            while j < len(lines) and not lines[j].strip():
                j += 1

            if (
                j < len(lines)
                and lines[j].startswith(
                    "            node_repair_result = choose_bidirectional_node_anchor_repair_for_segment("
                )
            ):
                k = j + 1

                while k < len(lines):
                    if lines[k].startswith("        if pair_path is not None:"):
                        break
                    k += 1

                if k >= len(lines):
                    raise RuntimeError(
                        "Could not find end of node/station repair block before `if pair_path is not None:`"
                    )

                result_lines.append(lines[k])
                i = k + 1
                removed_repair_block = True
                continue

        result_lines.append(line)
        i += 1

    if not removed_repair_block:
        print("WARN: repair block was not found, maybe already removed")

    lines = result_lines

    # 3. Чистим аргументы старой расширенной сигнатуры build_topology_path_between_candidates(...).
    result_lines = []
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()

        if stripped in {
            "rescue_node_cache=rescue_node_cache,",
            "nearby_edge_cache=nearby_edge_cache,",
        }:
            i += 1
            continue

        if stripped.startswith("start_link_options_override=[node_anchor_overrides[index - 1]]"):
            i += 1
            while i < len(lines) and "else None," not in lines[i]:
                i += 1
            if i < len(lines):
                i += 1
            continue

        if stripped.startswith("end_link_options_override=[node_anchor_overrides[index]]"):
            i += 1
            while i < len(lines) and "else None," not in lines[i]:
                i += 1
            if i < len(lines):
                i += 1
            continue

        result_lines.append(lines[i])
        i += 1

    func = "".join(result_lines)

    return text[:start] + func + text[end:]


def main() -> None:
    if not TARGET.exists():
        raise SystemExit(f"File not found: {TARGET}")

    original = TARGET.read_text(encoding="utf-8")

    backup = TARGET.with_suffix(TARGET.suffix + ".bak_before_repair_rollback")
    backup.write_text(original, encoding="utf-8")

    text = original

    text = remove_constants(text, CONSTANTS_TO_REMOVE)
    text = remove_top_level_functions(text, FUNCTIONS_TO_REMOVE)

    text = replace_top_level_function(text, "_link_source_rank", NEW_LINK_SOURCE_RANK)
    text = replace_top_level_function(text, "compute_transition_cost", NEW_COMPUTE_TRANSITION_COST)
    text = replace_top_level_function(
        text,
        "_evaluate_topology_link_pair_options",
        NEW_EVALUATE_TOPOLOGY_LINK_PAIR_OPTIONS,
    )
    text = replace_top_level_function(
        text,
        "_topology_result_source_rank",
        NEW_TOPOLOGY_RESULT_SOURCE_RANK,
    )
    text = replace_top_level_function(
        text,
        "build_topology_path_between_candidates",
        NEW_BUILD_TOPOLOGY_PATH_BETWEEN_CANDIDATES,
    )

    text = patch_build_route_geometry_between_locked_candidates(text)

    forbidden_markers = [
        "rejected absurd detour selected for rendering",
        "rejected_absurd_detour",
        "graph_path_absurd_detour",
        "forced_debug_render",
        "node_anchor_overrides",
        "nearby_edge_cache",
        "build_station_anchor_node_options",
        "_evaluate_node_anchor_override_for_stop",
        "choose_node_anchor_repair_for_stop",
        "choose_bidirectional_node_anchor_repair_for_segment",
        "try_synthetic_gap_bridge_rescue",
        "start_link_options_override",
        "end_link_options_override",
    ]

    found = [marker for marker in forbidden_markers if marker in text]

    if found:
        print("ERROR: forbidden markers are still present after patch:")
        for marker in found:
            print(f"  - {marker}")
        print()
        print("File was NOT written.")
        print(f"Backup is available at: {backup}")
        raise SystemExit(1)

    # Проверка синтаксиса до записи.
    ast.parse(text)

    TARGET.write_text(text, encoding="utf-8")

    print(f"OK: patched {TARGET}")
    print(f"Backup: {backup}")


if __name__ == "__main__":
    main()
