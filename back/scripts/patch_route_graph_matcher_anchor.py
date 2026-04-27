from __future__ import annotations

import argparse
from pathlib import Path

ANCHOR_HELPERS = r'''

ANCHOR_REPAIR_MAX_CANDIDATES_PER_STOP = 6
ANCHOR_REPAIR_SCORE_FAILURE = 1_000_000.0
ANCHOR_REPAIR_ACCEPT_DELTA = 5.0


def _anchor_console_log(message: str, payload: dict[str, Any] | None = None) -> None:
    try:
        if payload is None:
            print(f"[anchor-repair] {message}")
        else:
            print(
                "[anchor-repair] "
                + message
                + ": "
                + json.dumps(payload, ensure_ascii=False, default=str)
            )
    except Exception:
        print(f"[anchor-repair] {message}")


def _station_debug_label(candidate: Candidate | None, stop: dict[str, Any] | None = None) -> str:
    if candidate is not None:
        return f"{candidate.name}#{candidate.station_id}"
    if stop is not None:
        return str(stop.get("station_name_raw") or stop.get("stop_sequence") or "?")
    return "?"


def _anchor_search_mode_penalty(search_mode: str | None) -> float:
    penalties = {
        "station_links_only": 0.0,
        "station_links_plus_nearby_edges_400m": 4.0,
        "station_links_plus_nearby_edges_600m": 7.0,
        "station_links_plus_local_rescue": 12.0,
        "isolated_component_bridge_last_resort": 35.0,
    }
    return penalties.get(str(search_mode or ""), 18.0)


def _anchor_render_method_penalty(render_method: str | None) -> float:
    penalties = {
        "topology_graph_path": 0.0,
        "topology_component_bridge": 20.0,
        "fallback_straight": 300.0,
    }
    return penalties.get(str(render_method or ""), 120.0)


def _segment_result_quality_score(pair_path: dict[str, Any] | None) -> float:
    if pair_path is None:
        return ANCHOR_REPAIR_SCORE_FAILURE

    score = float(pair_path.get("total_score_km") or 0.0)
    score += _anchor_search_mode_penalty(pair_path.get("search_mode"))
    score += _anchor_render_method_penalty(pair_path.get("render_method"))

    transition_diag = pair_path.get("transition_diag") or {}
    relative_error = safe_float(transition_diag.get("relative_error"))
    if relative_error is not None:
        score += relative_error * 25.0

    graph_distance_km = safe_float(pair_path.get("graph_distance_km"))
    if graph_distance_km is not None:
        score += graph_distance_km * 0.015

    bridge_gap_km = safe_float(pair_path.get("bridge_gap_km"))
    if bridge_gap_km is not None:
        score += bridge_gap_km * 15.0

    return score


def _candidate_override_list_for_stop(
    stop: dict[str, Any],
    candidates: list[Candidate],
    current_candidate: Candidate | None,
) -> list[Candidate]:
    result: list[Candidate] = []
    seen_station_ids: set[int] = set()

    if current_candidate is not None:
        result.append(current_candidate)
        seen_station_ids.add(int(current_candidate.station_id))

    for candidate in candidates:
        candidate_station_id = int(candidate.station_id)
        if candidate_station_id in seen_station_ids:
            continue

        name_similarity = compute_name_similarity(
            stop.get("station_name_raw"),
            candidate.name,
        )
        if name_similarity < 0.35 and not candidate.code_match and not candidate.anchor:
            continue

        result.append(candidate)
        seen_station_ids.add(candidate_station_id)

        if len(result) >= ANCHOR_REPAIR_MAX_CANDIDATES_PER_STOP:
            break

    return result


def _evaluate_anchor_override_for_stop(
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
    temporary_locked = list(locked_candidates)
    temporary_locked[stop_index] = override_candidate

    evaluated_segments: list[int] = []
    if stop_index >= 1:
        evaluated_segments.append(stop_index)
    if stop_index + 1 < len(stops):
        evaluated_segments.append(stop_index + 1)

    segment_results: list[dict[str, Any]] = []
    total_score = 0.0
    success_count = 0
    failure_count = 0

    for segment_index in evaluated_segments:
        previous_candidate = temporary_locked[segment_index - 1]
        current_candidate = temporary_locked[segment_index]

        pair_path = None
        if previous_candidate is not None and current_candidate is not None:
            pair_path = build_topology_path_between_candidates(
                previous_stop=stops[segment_index - 1],
                current_stop=stops[segment_index],
                previous_candidate=previous_candidate,
                current_candidate=current_candidate,
                network=network,
                path_cache=path_cache,
                fallback_node_cache=fallback_node_cache,
                rescue_node_cache=rescue_node_cache,
            )

        quality_score = _segment_result_quality_score(pair_path)
        total_score += quality_score

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
                "segment_score": round(quality_score, 4),
                "graph_distance_km": round(float(pair_path.get("graph_distance_km") or 0.0), 4)
                if pair_path is not None else None,
                "total_score_km": round(float(pair_path.get("total_score_km") or 0.0), 4)
                if pair_path is not None else None,
            }
        )

    name_similarity = compute_name_similarity(
        stops[stop_index].get("station_name_raw"),
        override_candidate.name,
    )
    total_score += max(0.0, 0.85 - name_similarity) * 20.0
    total_score += max(0.0, 1.0 - float(override_candidate.effective_score)) * 8.0

    if not override_candidate.code_match and not override_candidate.anchor:
        total_score += 0.75

    return {
        "candidate": override_candidate,
        "stop_index": stop_index,
        "evaluated_segments": evaluated_segments,
        "segment_results": segment_results,
        "success_count": success_count,
        "failure_count": failure_count,
        "name_similarity": round(name_similarity, 4),
        "total_score": float(total_score),
    }


def choose_anchor_repair_for_stop(
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
    logger_context = logger_context or {}

    if stop_index <= 0 or stop_index >= len(stops):
        return None
    if stop_index >= len(locked_candidates):
        return None
    if stop_index >= len(candidates_per_stop):
        return None

    current_locked = locked_candidates[stop_index]
    previous_locked = locked_candidates[stop_index - 1] if stop_index - 1 >= 0 else None

    if current_locked is None or previous_locked is None:
        return None

    stop = stops[stop_index]
    candidate_pool = _candidate_override_list_for_stop(
        stop,
        candidates_per_stop[stop_index],
        current_locked,
    )
    if len(candidate_pool) <= 1:
        _anchor_console_log(
            "repair probe skipped - no alternative candidates",
            {
                "segment_index": segment_index,
                "stop_index": stop_index,
                "station_name_raw": stop.get("station_name_raw"),
                "locked_station": _station_debug_label(current_locked, stop),
            },
        )
        return None

    baseline = _evaluate_anchor_override_for_stop(
        stop_index=stop_index,
        override_candidate=current_locked,
        locked_candidates=locked_candidates,
        stops=stops,
        network=network,
        path_cache=path_cache,
        fallback_node_cache=fallback_node_cache,
        rescue_node_cache=rescue_node_cache,
    )

    _anchor_console_log(
        "repair probe start",
        {
            "segment_index": segment_index,
            "stop_index": stop_index,
            "station_name_raw": stop.get("station_name_raw"),
            "locked_station": _station_debug_label(current_locked, stop),
            "baseline_score": round(float(baseline["total_score"]), 4),
            "baseline_failures": baseline["failure_count"],
            "candidate_pool_size": len(candidate_pool),
        },
    )

    best = baseline
    evaluation_logs: list[dict[str, Any]] = []

    for candidate in candidate_pool:
        evaluation = _evaluate_anchor_override_for_stop(
            stop_index=stop_index,
            override_candidate=candidate,
            locked_candidates=locked_candidates,
            stops=stops,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            rescue_node_cache=rescue_node_cache,
        )

        evaluation_log = {
            "candidate_station_id": int(candidate.station_id),
            "candidate_station_name": candidate.name,
            "candidate_score": round(float(candidate.effective_score), 4),
            "candidate_name_similarity": evaluation["name_similarity"],
            "total_score": round(float(evaluation["total_score"]), 4),
            "success_count": evaluation["success_count"],
            "failure_count": evaluation["failure_count"],
            "evaluated_segments": evaluation["evaluated_segments"],
            "segment_results": evaluation["segment_results"],
        }
        evaluation_logs.append(evaluation_log)
        _anchor_console_log("candidate checked", evaluation_log)

        if float(evaluation["total_score"]) < float(best["total_score"]):
            best = evaluation

    best_candidate: Candidate = best["candidate"]
    improvement = float(baseline["total_score"]) - float(best["total_score"])

    accepted = False
    if int(best_candidate.station_id) != int(current_locked.station_id):
        if best["failure_count"] < baseline["failure_count"]:
            accepted = True
        elif improvement >= ANCHOR_REPAIR_ACCEPT_DELTA:
            accepted = True

    result = {
        "accepted": accepted,
        "segment_index": segment_index,
        "stop_index": stop_index,
        "station_name_raw": stop.get("station_name_raw"),
        "candidate": best_candidate,
        "before_station_id": int(current_locked.station_id),
        "before_station_name": current_locked.name,
        "after_station_id": int(best_candidate.station_id),
        "after_station_name": best_candidate.name,
        "before_score": round(float(baseline["total_score"]), 4),
        "after_score": round(float(best["total_score"]), 4),
        "improvement": round(improvement, 4),
        "before_failures": int(baseline["failure_count"]),
        "after_failures": int(best["failure_count"]),
        "evaluated_segments": best["evaluated_segments"],
        "candidate_evaluations": evaluation_logs,
    }

    if diagnostics is not None:
        diagnostics.setdefault("anchor_repairs", [])
        diagnostics["anchor_repairs"].append(
            {
                key: value
                for key, value in result.items()
                if key != "candidate"
            }
        )

    if accepted:
        _anchor_console_log("repair accepted", {k: v for k, v in result.items() if k != "candidate"})
        log_event(
            "info",
            "anchor_repair_accepted",
            **{k: v for k, v in result.items() if k != "candidate"},
            **logger_context,
        )
    else:
        _anchor_console_log("repair rejected", {k: v for k, v in result.items() if k != "candidate"})

    return result
'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(f"Не найден фрагмент для замены: {label}")
    return text.replace(old, new, 1)


def apply_patch(source: str) -> str:
    if "ANCHOR_REPAIR_MAX_CANDIDATES_PER_STOP" in source:
        raise RuntimeError("Похоже, anchor patch уже применен")

    source = replace_once(
        source,
        '_GRAPH_CACHE: dict[str, Any] = {\n    "cache_by_region_key": {},\n}\n',
        '_GRAPH_CACHE: dict[str, Any] = {\n    "cache_by_region_key": {},\n}\n' + ANCHOR_HELPERS + '\n',
        "anchor helper injection",
    )

    source = replace_once(
        source,
        'def build_route_geometry_between_locked_candidates(\n    stops: list[dict[str, Any]],\n    locked_candidates: list[Candidate | None],\n    network: dict[str, Any],\n',
        'def build_route_geometry_between_locked_candidates(\n    stops: list[dict[str, Any]],\n    locked_candidates: list[Candidate | None],\n    candidates_per_stop: list[list[Candidate]],\n    network: dict[str, Any],\n',
        "build_route_geometry signature",
    )

    repair_block = '''        if pair_path is None:\n            repair_result = choose_anchor_repair_for_stop(\n                stop_index=index,\n                segment_index=index,\n                stops=stops,\n                locked_candidates=locked_candidates,\n                candidates_per_stop=candidates_per_stop,\n                network=network,\n                path_cache=path_cache,\n                fallback_node_cache=fallback_node_cache,\n                rescue_node_cache=rescue_node_cache,\n                diagnostics=diagnostics,\n                logger_context=logger_context,\n            )\n            if repair_result is not None and repair_result.get("accepted"):\n                repaired_stop_index = int(repair_result["stop_index"])\n                locked_candidates[repaired_stop_index] = repair_result["candidate"]\n                previous_candidate = locked_candidates[index - 1] if index - 1 < len(locked_candidates) else None\n                current_candidate = locked_candidates[index] if index < len(locked_candidates) else None\n\n                if previous_candidate is not None and current_candidate is not None:\n                    pair_path = build_topology_path_between_candidates(\n                        previous_stop=previous_stop,\n                        current_stop=current_stop,\n                        previous_candidate=previous_candidate,\n                        current_candidate=current_candidate,\n                        network=network,\n                        path_cache=path_cache,\n                        fallback_node_cache=fallback_node_cache,\n                        rescue_node_cache=rescue_node_cache,\n                    )\n                    if pair_path is not None:\n                        pair_path["anchor_repair_applied"] = True\n                        pair_path["anchor_repair_summary"] = {\n                            "stop_index": repair_result.get("stop_index"),\n                            "from_station_id": repair_result.get("before_station_id"),\n                            "to_station_id": repair_result.get("after_station_id"),\n                            "from_station_name": repair_result.get("before_station_name"),\n                            "to_station_name": repair_result.get("after_station_name"),\n                            "before_score": repair_result.get("before_score"),\n                            "after_score": repair_result.get("after_score"),\n                            "improvement": repair_result.get("improvement"),\n                            "evaluated_segments": repair_result.get("evaluated_segments"),\n                        }\n\n'''

    source = replace_once(
        source,
        '        if pair_path is not None:\n            coords = pair_path.get("coordinates") or []\n',
        repair_block + '        if pair_path is not None:\n            coords = pair_path.get("coordinates") or []\n',
        "anchor repair runtime hook",
    )

    source = replace_once(
        source,
        '                "cost_diag": pair_path.get("transition_diag"),\n            }\n',
        '                "cost_diag": pair_path.get("transition_diag"),\n                "anchor_repair_applied": bool(pair_path.get("anchor_repair_applied")),\n                "anchor_repair_summary": pair_path.get("anchor_repair_summary"),\n            }\n',
        "anchor repair transition log fields",
    )

    source = replace_once(
        source,
        '                    locked_candidates=locked_candidates,\n                    network=network,\n                    diagnostics=diagnostics,\n',
        '                    locked_candidates=locked_candidates,\n                    candidates_per_stop=candidates_per_stop,\n                    network=network,\n                    diagnostics=diagnostics,\n',
        "resolve_route_for_map callsite",
    )

    return source


def main() -> None:
    parser = argparse.ArgumentParser(description="Patch app/route_graph_matcher.py with anchor repair runtime logic")
    parser.add_argument(
        "--target",
        default="app/route_graph_matcher.py",
        help="Path to route_graph_matcher.py",
    )
    args = parser.parse_args()

    target = Path(args.target)
    if not target.exists():
        raise SystemExit(f"Файл не найден: {target}")

    source = target.read_text(encoding="utf-8")
    patched = apply_patch(source)

    backup = target.with_suffix(target.suffix + ".bak_anchor_patch")
    if not backup.exists():
        backup.write_text(source, encoding="utf-8")

    target.write_text(patched, encoding="utf-8")

    print(f"Patched: {target}")
    print(f"Backup:  {backup}")


if __name__ == "__main__":
    main()
