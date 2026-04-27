#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone/full version of a route root-cause debugger with anchor-repair probe.

Important:
- This is a full, self-contained file you can copy into scripts/debug_route_root_cause.py
  or use as a reference replacement.
- It works best with --from-json on an already-saved debug payload.
- It also contains the repair-probe logic that searches for a better mid-station anchor
  using seed nodes extracted from debug output.
- To make repair work inside the main project (without json fallback), wire the adapter
  in ProjectGraphRuntime.find_path_between_debug_seeds(...) to your real matcher/runtime.

Expected high-level payload shape (flexible, partial fields are tolerated):
{
  "route": {...},
  "network": {...},
  "inferred_regions": {...},
  "selected_segment_debug": {...},
  "previous_segment_debug": {...},
  "next_segment_debug": {...},
  "segment_station_candidates": {...},
  ...
}

The script is intentionally defensive because real debug payloads often drift.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# =====================================================================================
# Printing helpers
# =====================================================================================


def print_section(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def dump_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


# =====================================================================================
# Small generic helpers
# =====================================================================================


def _as_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _first_nonempty(*values: Any) -> Any:
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def _round_if_num(v: Any, ndigits: int = 4) -> Any:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(v, ndigits)
    return v


def _safe_rel_err(actual_km: Optional[float], expected_km: Optional[float]) -> Optional[float]:
    if actual_km is None or expected_km is None or expected_km <= 0:
        return None
    return abs(actual_km - expected_km) / expected_km


def _find_first_key_recursively(obj: Any, target_key: str) -> Any:
    if isinstance(obj, dict):
        if target_key in obj:
            return obj[target_key]
        for v in obj.values():
            found = _find_first_key_recursively(v, target_key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_first_key_recursively(item, target_key)
            if found is not None:
                return found
    return None


# =====================================================================================
# Geometry compacting
# =====================================================================================


def compact_geometry(geometry: Any) -> Any:
    geometry = _as_dict(geometry)
    if not geometry:
        return None

    coords = geometry.get("coordinates")
    if geometry.get("type") != "LineString" or not isinstance(coords, list):
        return geometry

    points_count = len(coords)
    if points_count <= 6:
        return geometry

    return {
        "type": "LineString",
        "coordinates": {
            "points_count": points_count,
            "head": coords[:3],
            "tail": coords[-2:],
            "omitted": max(0, points_count - 5),
        },
    }


# =====================================================================================
# Seed extraction from debug pairs
# =====================================================================================


def _pair_to_seed(
    pair: Dict[str, Any],
    side: str,
    *,
    seed_origin: str,
    station_id: Any = None,
    station_name: Any = None,
) -> Optional[Dict[str, Any]]:
    node_hash = pair.get(f"{side}_node_hash")
    if not node_hash:
        return None

    geom = _as_dict(pair.get("geometry"))
    lon = None
    lat = None
    coords = geom.get("coordinates")

    if isinstance(coords, list) and coords:
        pt = coords[0] if side == "from" else coords[-1]
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            lon, lat = pt[0], pt[1]

    return {
        "node_hash": node_hash,
        "source": pair.get(f"{side}_source"),
        "entry_km": pair.get(f"{side}_entry_km"),
        "component_id": pair.get(f"{side}_component_id"),
        "component_size": pair.get(f"{side}_component_size"),
        "station_id": station_id,
        "station_name": station_name,
        "lon": lon,
        "lat": lat,
        "seed_origin": seed_origin,
    }


def _dedupe_seed_dicts(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        if not item:
            continue
        key = (
            item.get("node_hash"),
            item.get("component_id"),
            item.get("source"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _collect_mode_seed_nodes(
    mode_block: Dict[str, Any],
    *,
    from_station: Optional[Dict[str, Any]] = None,
    to_station: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    from_station = _as_dict(from_station)
    to_station = _as_dict(to_station)

    from_station_id = from_station.get("station_id")
    from_station_name = from_station.get("station_name")
    to_station_id = to_station.get("station_id")
    to_station_name = to_station.get("station_name")

    seeds_from: List[Dict[str, Any]] = []
    seeds_to: List[Dict[str, Any]] = []

    best_rejected = _as_dict(mode_block.get("best_rejected"))
    if best_rejected:
        one = _pair_to_seed(
            best_rejected,
            "from",
            seed_origin="best_rejected",
            station_id=from_station_id,
            station_name=from_station_name,
        )
        if one:
            seeds_from.append(one)
        one = _pair_to_seed(
            best_rejected,
            "to",
            seed_origin="best_rejected",
            station_id=to_station_id,
            station_name=to_station_name,
        )
        if one:
            seeds_to.append(one)

    for pair in _as_list(mode_block.get("successful_pairs"))[:50]:
        pair = _as_dict(pair)
        one = _pair_to_seed(
            pair,
            "from",
            seed_origin="successful_pair",
            station_id=from_station_id,
            station_name=from_station_name,
        )
        if one:
            seeds_from.append(one)
        one = _pair_to_seed(
            pair,
            "to",
            seed_origin="successful_pair",
            station_id=to_station_id,
            station_name=to_station_name,
        )
        if one:
            seeds_to.append(one)

    for pair in _as_list(mode_block.get("rejected_pairs"))[:50]:
        pair = _as_dict(pair)
        one = _pair_to_seed(
            pair,
            "from",
            seed_origin="rejected_pair",
            station_id=from_station_id,
            station_name=from_station_name,
        )
        if one:
            seeds_from.append(one)
        one = _pair_to_seed(
            pair,
            "to",
            seed_origin="rejected_pair",
            station_id=to_station_id,
            station_name=to_station_name,
        )
        if one:
            seeds_to.append(one)

    return {
        "from": _dedupe_seed_dicts(seeds_from),
        "to": _dedupe_seed_dicts(seeds_to),
    }


# =====================================================================================
# Payload normalization / enrichment
# =====================================================================================


def _build_repair_station_refs(segment_debug: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    from_stop = _as_dict(segment_debug.get("from_stop"))
    to_stop = _as_dict(segment_debug.get("to_stop"))

    return {
        "from": {
            "stop_sequence": _first_nonempty(from_stop.get("stop_sequence"), segment_debug.get("from_stop_sequence")),
            "station_id": _first_nonempty(
                from_stop.get("locked_station_id"),
                from_stop.get("station_id"),
                segment_debug.get("from_station_id"),
            ),
            "station_name": _first_nonempty(
                from_stop.get("locked_station_name"),
                from_stop.get("station_name"),
                from_stop.get("station_name_raw"),
                segment_debug.get("from_station_name"),
            ),
            "station_name_raw": _first_nonempty(from_stop.get("station_name_raw"), segment_debug.get("from_station_name")),
        },
        "to": {
            "stop_sequence": _first_nonempty(to_stop.get("stop_sequence"), segment_debug.get("to_stop_sequence")),
            "station_id": _first_nonempty(
                to_stop.get("locked_station_id"),
                to_stop.get("station_id"),
                segment_debug.get("to_station_id"),
            ),
            "station_name": _first_nonempty(
                to_stop.get("locked_station_name"),
                to_stop.get("station_name"),
                to_stop.get("station_name_raw"),
                segment_debug.get("to_station_name"),
            ),
            "station_name_raw": _first_nonempty(to_stop.get("station_name_raw"), segment_debug.get("to_station_name")),
        },
    }


SEARCH_MODE_KEYS = (
    "station_links_only",
    "station_links_plus_nearby_edges_400m",
    "station_links_plus_nearby_edges_600m",
    "station_links_plus_local_rescue",
    "isolated_component_bridge_last_resort",
)


def _extract_search_modes(segment_debug: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    raw = _as_dict(segment_debug.get("search_modes"))
    if raw:
        return raw

    extracted = {}
    for key in SEARCH_MODE_KEYS:
        block = _as_dict(segment_debug.get(key))
        if block:
            extracted[key] = block
    return extracted


def _enrich_segment_for_repair(segment_debug: Any) -> Dict[str, Any]:
    segment_debug = _as_dict(segment_debug)
    if not segment_debug:
        return {}

    out = dict(segment_debug)
    out["repair_station_refs"] = _build_repair_station_refs(segment_debug)

    mode_blocks = _extract_search_modes(segment_debug)
    out["search_modes"] = mode_blocks

    repair_seed_nodes = {}
    from_station = out["repair_station_refs"]["from"]
    to_station = out["repair_station_refs"]["to"]

    for mode_name, mode_block in mode_blocks.items():
        repair_seed_nodes[mode_name] = _collect_mode_seed_nodes(
            _as_dict(mode_block),
            from_station=from_station,
            to_station=to_station,
        )

    out["repair_seed_nodes"] = repair_seed_nodes
    return out


def _build_middle_station_ref(selected_segment: Dict[str, Any], next_segment: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    selected_to = _as_dict(_as_dict(selected_segment.get("repair_station_refs")).get("to"))
    next_from = _as_dict(_as_dict(next_segment.get("repair_station_refs")).get("from"))

    if selected_to.get("station_id") and selected_to.get("station_id") == next_from.get("station_id"):
        return selected_to
    if selected_to.get("station_name") and selected_to.get("station_name") == next_from.get("station_name"):
        return selected_to or next_from
    return selected_to or next_from or None


# =====================================================================================
# Segment summaries / diagnostics
# =====================================================================================


def _segment_brief(segment_index: Optional[int], seg: Dict[str, Any]) -> str:
    refs = _as_dict(seg.get("repair_station_refs"))
    from_name = _as_dict(refs.get("from")).get("station_name") or "?"
    to_name = _as_dict(refs.get("to")).get("station_name") or "?"
    return (
        f"[segment {segment_index}] {from_name} -> {to_name} | "
        f"path_found={seg.get('path_found')} | "
        f"chosen_search_mode={seg.get('chosen_search_mode')} | "
        f"delta_rzd_km={seg.get('delta_rzd_km')} | "
        f"ratio_graph_to_rzd={seg.get('ratio_graph_to_rzd')}"
    )


def _infer_station_link_complexity(selected_segment: Dict[str, Any], previous_segment: Dict[str, Any], next_segment: Dict[str, Any]) -> Dict[str, Any]:
    def pairs_checked(seg: Dict[str, Any], mode_name: str = "station_links_only") -> Optional[int]:
        mode = _as_dict(_as_dict(seg.get("search_modes")).get(mode_name))
        return mode.get("pairs_checked")

    selected_pairs = pairs_checked(selected_segment)
    prev_pairs = pairs_checked(previous_segment)
    next_pairs = pairs_checked(next_segment)

    notes = []
    from_est = None
    to_est = None

    if isinstance(next_pairs, int) and next_pairs > 0:
        to_est = 1
        if isinstance(selected_pairs, int) and selected_pairs > 0:
            from_est = selected_pairs
        notes.append("Следующий сегмент имеет ровно 1 pair, значит текущая конечная станция, скорее всего, имеет 1 station_link.")
        if from_est is not None:
            notes.append(f"Тогда текущая начальная станция, вероятно, имеет {from_est} station_link-ов.")

    return {
        "selected_station_links_only_pairs_checked": selected_pairs,
        "previous_station_links_only_pairs_checked": prev_pairs,
        "next_station_links_only_pairs_checked": next_pairs,
        "from_station_link_count_estimate": from_est,
        "to_station_link_count_estimate": to_est,
        "notes": notes,
    }


def _compact_pair_for_output(pair: Dict[str, Any]) -> Dict[str, Any]:
    pair = dict(pair)
    if "geometry" in pair:
        pair["geometry"] = compact_geometry(pair.get("geometry"))

    # round a few common floats for readability
    for k in [
        "from_entry_km",
        "to_entry_km",
        "graph_distance_km",
        "render_total_distance_km",
        "connector_penalty",
        "source_penalty",
    ]:
        if k in pair:
            pair[k] = _round_if_num(pair[k])

    diag = _as_dict(pair.get("transition_diag"))
    if diag:
        pair["transition_diag"] = {kk: _round_if_num(vv) for kk, vv in diag.items()}

    return pair


def _build_root_cause_hypotheses(
    selected_segment: Dict[str, Any],
    previous_segment: Dict[str, Any],
    next_segment: Dict[str, Any],
    complexity: Dict[str, Any],
) -> List[Dict[str, Any]]:
    hypotheses = []

    # best rejected on selected segment
    selected_station_links_only = _as_dict(_as_dict(selected_segment.get("search_modes")).get("station_links_only"))
    best_rejected_selected = _as_dict(selected_station_links_only.get("best_rejected"))
    ratio = _first_nonempty(
        selected_segment.get("ratio_graph_to_rzd"),
        _as_dict(best_rejected_selected.get("transition_diag")).get("relative_error"),
    )

    if isinstance(ratio, (int, float)) and ratio >= 1.0:
        hypotheses.append({
            "severity": "high",
            "code": "same_component_huge_detour",
            "message": None,
            "details": {
                "ratio_graph_to_rzd": _round_if_num(ratio),
                "from_component_id": best_rejected_selected.get("from_component_id"),
                "to_component_id": best_rejected_selected.get("to_component_id"),
                "best_rejected_pair": _compact_pair_for_output(best_rejected_selected),
            },
        })

    # next segment component break
    next_station_links_only = _as_dict(_as_dict(next_segment.get("search_modes")).get("station_links_only"))
    best_rejected_next = _as_dict(next_station_links_only.get("best_rejected"))
    if best_rejected_next.get("rejected_reason") == "no_graph_path":
        hypotheses.append({
            "severity": "critical",
            "code": "next_segment_component_break",
            "message": None,
            "details": {
                "next_segment_index": next_segment.get("segment_index"),
                "next_segment_from_station": _as_dict(_as_dict(next_segment.get("repair_station_refs")).get("from")).get("station_name"),
                "next_segment_to_station": _as_dict(_as_dict(next_segment.get("repair_station_refs")).get("to")).get("station_name"),
                "from_component_id": best_rejected_next.get("from_component_id"),
                "to_component_id": best_rejected_next.get("to_component_id"),
                "best_rejected_pair": _compact_pair_for_output(best_rejected_next),
                "next_chosen_search_mode": next_segment.get("chosen_search_mode"),
            },
        })

    hypotheses.append({
        "severity": "critical",
        "code": "middle_station_anchor_problem",
        "message": None,
        "details": {
            "suspected_station": _build_middle_station_ref(selected_segment, next_segment),
            "incoming_segment_index": selected_segment.get("segment_index"),
            "outgoing_segment_index": next_segment.get("segment_index"),
        },
    })

    hypotheses.append({
        "severity": "medium",
        "code": "large_station_many_station_links",
        "message": None,
        "details": complexity,
    })

    # spread by search mode
    mode_values = []
    for mode_name in SEARCH_MODE_KEYS:
        mode = _as_dict(_as_dict(selected_segment.get("search_modes")).get(mode_name))
        best_rejected = _as_dict(mode.get("best_rejected"))
        value = _first_nonempty(best_rejected.get("render_total_distance_km"), best_rejected.get("graph_distance_km"))
        if isinstance(value, (int, float)):
            mode_values.append(round(float(value), 4))

    if mode_values:
        hypotheses.append({
            "severity": "high",
            "code": "not_a_local_search_radius_issue",
            "message": None,
            "details": {
                "mode_render_total_distance_km_values": mode_values,
                "spread_km": round(max(mode_values) - min(mode_values), 4),
            },
        })

    hypotheses.append({
        "severity": "high",
        "code": "failure_is_localized_not_global",
        "message": None,
        "details": {
            "previous_segment_index": previous_segment.get("segment_index"),
            "previous_from_station": _as_dict(_as_dict(previous_segment.get("repair_station_refs")).get("from")).get("station_name"),
            "previous_to_station": _as_dict(_as_dict(previous_segment.get("repair_station_refs")).get("to")).get("station_name"),
        },
    })

    return hypotheses


def _build_anchor_probe_summary(
    selected_segment: Dict[str, Any],
    next_segment: Dict[str, Any],
    complexity: Dict[str, Any],
) -> Dict[str, Any]:
    middle_station = _build_middle_station_ref(selected_segment, next_segment)

    ratio = selected_segment.get("ratio_graph_to_rzd")
    if ratio is None:
        best_rejected = _as_dict(_as_dict(_as_dict(selected_segment.get("search_modes")).get("station_links_only")).get("best_rejected"))
        ratio = _as_dict(best_rejected.get("transition_diag")).get("relative_error")

    next_station_links_only = _as_dict(_as_dict(next_segment.get("search_modes")).get("station_links_only"))
    next_path_found = next_segment.get("path_found")
    if next_path_found is None:
        next_path_found = next_station_links_only.get("successful_pairs_count", 0) > 0

    is_primary_vokzal_problem = bool((complexity.get("from_station_link_count_estimate") or 0) >= 6)
    verdict = "primary_anchor_or_component_problem"
    if middle_station is None:
        verdict = "unclear"

    return {
        "verdict": verdict,
        "suspected_station": middle_station,
        "is_primary_vokzal_problem": is_primary_vokzal_problem,
        "selected_segment_ratio_graph_to_rzd": _round_if_num(ratio),
        "selected_from_station_link_estimate": complexity.get("from_station_link_count_estimate"),
        "selected_to_station_link_estimate": complexity.get("to_station_link_count_estimate"),
        "next_segment_path_found": bool(next_path_found),
        "next_segment_chosen_search_mode": next_segment.get("chosen_search_mode"),
        "recommended_next_actions": [
            "Проверить topology attachment у промежуточной станции.",
            "Попробовать альтернативные anchor/component для конечной станции выбранного сегмента.",
            "Сравнивать входящий и исходящий сегменты совместно, а не по одному.",
            "Не рисовать absurd_detour путь как fallback.",
        ],
    }


# =====================================================================================
# Graph runtime adapter
# =====================================================================================


class ProjectGraphRuntime:
    """
    Adapter for real project integration.

    Replace find_path_between_debug_seeds(...) with a call into your real routing/
    matcher runtime. The contract is intentionally small.
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload

    def is_available(self) -> bool:
        # Return True only after you wire the real runtime.
        return False

    def find_path_between_debug_seeds(
        self,
        *,
        from_node_hash: str,
        to_node_hash: str,
        expected_km: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Expected result shape:
        {
          "path_found": bool,
          "graph_distance_km": float | None,
          "render_total_distance_km": float | None,
          "hop_count": int | None,
          "connector_penalty": float | None,
          "source_penalty": float | None,
          "rejected_reason": str | None,
          "from_component_id": int | None,
          "to_component_id": int | None,
          "geometry": {...} | None,
        }

        TODO: wire to your real topology graph matcher.
        """
        return None


# =====================================================================================
# Repair probe
# =====================================================================================


def _collect_middle_station_seed_pool(selected_segment: Dict[str, Any], next_segment: Dict[str, Any]) -> List[Dict[str, Any]]:
    seeds: List[Dict[str, Any]] = []

    for _mode_name, block in _as_dict(selected_segment.get("repair_seed_nodes")).items():
        seeds.extend(_as_list(_as_dict(block).get("to")))

    for _mode_name, block in _as_dict(next_segment.get("repair_seed_nodes")).items():
        seeds.extend(_as_list(_as_dict(block).get("from")))

    return _dedupe_seed_dicts(seeds)


def _score_path_candidate(path_result: Optional[Dict[str, Any]], expected_km: Optional[float]) -> float:
    if not path_result:
        return 1e9
    if not path_result.get("path_found"):
        return 1e9

    actual = _first_nonempty(path_result.get("render_total_distance_km"), path_result.get("graph_distance_km"))
    rel_err = _safe_rel_err(actual, expected_km)

    score = 0.0
    if rel_err is None:
        score += 5000.0
    else:
        score += rel_err * 1000.0

    if path_result.get("component_break"):
        score += 2000.0
    if path_result.get("rejected_reason") == "graph_path_absurd_detour":
        score += 3000.0

    hops = path_result.get("hop_count")
    if isinstance(hops, int):
        score += min(hops, 300) * 0.5

    connector_penalty = path_result.get("connector_penalty") or 0.0
    source_penalty = path_result.get("source_penalty") or 0.0
    score += float(connector_penalty) * 10.0 + float(source_penalty) * 20.0

    return score


def _probe_path_between_seed_pools(
    *,
    graph_runtime: ProjectGraphRuntime,
    from_seed_pool: List[Dict[str, Any]],
    to_seed_pool: List[Dict[str, Any]],
    expected_km: Optional[float],
) -> Optional[Dict[str, Any]]:
    best = None
    best_score = 1e18

    for src in from_seed_pool:
        for dst in to_seed_pool:
            from_hash = src.get("node_hash")
            to_hash = dst.get("node_hash")
            if not from_hash or not to_hash:
                continue

            path_result = graph_runtime.find_path_between_debug_seeds(
                from_node_hash=from_hash,
                to_node_hash=to_hash,
                expected_km=expected_km,
            )
            if not path_result:
                continue

            score = _score_path_candidate(path_result, expected_km)
            if score < best_score:
                best_score = score
                best = {
                    "score": score,
                    "from_seed": src,
                    "to_seed": dst,
                    "path_result": path_result,
                }

    return best


def run_mid_station_anchor_repair_probe(
    *,
    selected_segment: Dict[str, Any],
    next_segment: Dict[str, Any],
    previous_segment: Optional[Dict[str, Any]],
    graph_runtime: ProjectGraphRuntime,
) -> Dict[str, Any]:
    middle_station = _build_middle_station_ref(selected_segment, next_segment)

    candidate_seeds = _collect_middle_station_seed_pool(selected_segment, next_segment)
    if not candidate_seeds:
        return {
            "probe_type": "repair_search",
            "status": "no_candidate_seeds",
            "suspected_station": middle_station or None,
            "message": "Не удалось собрать seed-кандидаты из debug output.",
        }

    incoming_from_pool: List[Dict[str, Any]] = []
    outgoing_to_pool: List[Dict[str, Any]] = []

    for _mode_name, block in _as_dict(selected_segment.get("repair_seed_nodes")).items():
        incoming_from_pool.extend(_as_list(_as_dict(block).get("from")))

    for _mode_name, block in _as_dict(next_segment.get("repair_seed_nodes")).items():
        outgoing_to_pool.extend(_as_list(_as_dict(block).get("to")))

    incoming_from_pool = _dedupe_seed_dicts(incoming_from_pool)
    outgoing_to_pool = _dedupe_seed_dicts(outgoing_to_pool)

    if not incoming_from_pool or not outgoing_to_pool:
        return {
            "probe_type": "repair_search",
            "status": "insufficient_neighbor_seed_pools",
            "suspected_station": middle_station or None,
            "incoming_pool_size": len(incoming_from_pool),
            "outgoing_pool_size": len(outgoing_to_pool),
        }

    if not graph_runtime.is_available():
        return {
            "probe_type": "repair_search",
            "status": "runtime_not_wired",
            "suspected_station": middle_station or None,
            "candidate_count": len(candidate_seeds),
            "message": "Repair probe готов, но не подключен к реальному topology runtime. Нужно реализовать ProjectGraphRuntime.find_path_between_debug_seeds().",
        }

    selected_expected_km = selected_segment.get("delta_rzd_km")
    next_expected_km = next_segment.get("delta_rzd_km")

    scored_candidates = []
    for seed in candidate_seeds:
        incoming_best = _probe_path_between_seed_pools(
            graph_runtime=graph_runtime,
            from_seed_pool=incoming_from_pool,
            to_seed_pool=[seed],
            expected_km=selected_expected_km,
        )
        outgoing_best = _probe_path_between_seed_pools(
            graph_runtime=graph_runtime,
            from_seed_pool=[seed],
            to_seed_pool=outgoing_to_pool,
            expected_km=next_expected_km,
        )

        incoming_score = _score_path_candidate(
            _as_dict(_as_dict(incoming_best).get("path_result")) or None,
            selected_expected_km,
        )
        outgoing_score = _score_path_candidate(
            _as_dict(_as_dict(outgoing_best).get("path_result")) or None,
            next_expected_km,
        )

        total_score = incoming_score + outgoing_score
        component_consistency_bonus = 0.0

        seed_component = seed.get("component_id")
        incoming_path = _as_dict(_as_dict(incoming_best).get("path_result"))
        outgoing_path = _as_dict(_as_dict(outgoing_best).get("path_result"))
        in_comp = incoming_path.get("to_component_id")
        out_comp = outgoing_path.get("from_component_id")
        if seed_component is not None and in_comp == seed_component and out_comp == seed_component:
            component_consistency_bonus = -500.0
            total_score += component_consistency_bonus

        scored_candidates.append({
            "seed": seed,
            "incoming_best": incoming_best,
            "outgoing_best": outgoing_best,
            "incoming_score": round(incoming_score, 4),
            "outgoing_score": round(outgoing_score, 4),
            "component_consistency_bonus": round(component_consistency_bonus, 4),
            "total_score": round(total_score, 4),
        })

    if not scored_candidates:
        return {
            "probe_type": "repair_search",
            "status": "no_runtime_results",
            "suspected_station": middle_station or None,
            "candidate_count": len(candidate_seeds),
        }

    scored_candidates.sort(key=lambda x: x["total_score"])
    best = scored_candidates[0]

    incoming_path = _as_dict(_as_dict(best.get("incoming_best")).get("path_result"))
    outgoing_path = _as_dict(_as_dict(best.get("outgoing_best")).get("path_result"))

    incoming_actual = _first_nonempty(incoming_path.get("render_total_distance_km"), incoming_path.get("graph_distance_km"))
    outgoing_actual = _first_nonempty(outgoing_path.get("render_total_distance_km"), outgoing_path.get("graph_distance_km"))
    incoming_rel_err = _safe_rel_err(incoming_actual, selected_expected_km)
    outgoing_rel_err = _safe_rel_err(outgoing_actual, next_expected_km)

    repair_found = (
        best["total_score"] < 1e8
        and incoming_path.get("path_found")
        and outgoing_path.get("path_found")
        and (incoming_rel_err is None or incoming_rel_err < 0.35)
        and (outgoing_rel_err is None or outgoing_rel_err < 0.35)
    )

    return {
        "probe_type": "repair_search",
        "status": "repair_found" if repair_found else "repair_not_good_enough",
        "suspected_station": middle_station or None,
        "candidate_count": len(scored_candidates),
        "best_candidate": best,
        "top_candidates": scored_candidates[:5],
        "recommended_anchor_seed": best["seed"] if repair_found else None,
    }


# =====================================================================================
# Payload I/O / route selection
# =====================================================================================


def _pick_latest_json(debug_output_dir: Path) -> Optional[Path]:
    files = sorted(debug_output_dir.glob("route_root_cause_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def load_payload(args: argparse.Namespace) -> Tuple[Dict[str, Any], str]:
    if args.from_json:
        path = Path(args.from_json)
        if not path.exists():
            raise FileNotFoundError(f"JSON not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            return json.load(f), f"json:{path}"

    debug_dir = Path(args.debug_output_dir)
    latest = _pick_latest_json(debug_dir)
    if latest is None:
        raise FileNotFoundError(
            f"Не найден ни один debug json в {debug_dir}. Запусти с --from-json или сохрани debug payload."
        )
    with latest.open("r", encoding="utf-8") as f:
        return json.load(f), f"latest_saved_json:{latest}"


# =====================================================================================
# Fuzzy payload readers
# =====================================================================================


def _get_route(payload: Dict[str, Any]) -> Dict[str, Any]:
    route = _as_dict(payload.get("route"))
    if route:
        return route
    return _as_dict(_find_first_key_recursively(payload, "route"))


def _get_inferred_regions(payload: Dict[str, Any]) -> Dict[str, Any]:
    for key in ("inferred_regions", "inferred_region_codes"):
        if key in payload:
            val = payload[key]
            if isinstance(val, dict):
                return val
            if isinstance(val, list):
                return {"inferred_region_codes": val}
    found = _find_first_key_recursively(payload, "inferred_region_codes")
    if isinstance(found, list):
        return {"inferred_region_codes": found}
    return {}


def _get_network(payload: Dict[str, Any]) -> Dict[str, Any]:
    network = _as_dict(payload.get("network"))
    if network:
        return network
    return _as_dict(_find_first_key_recursively(payload, "network"))


def _get_segment_debugs(payload: Dict[str, Any], segment_index: int) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    selected = _enrich_segment_for_repair(payload.get("selected_segment_debug"))
    prev_seg = _enrich_segment_for_repair(payload.get("previous_segment_debug"))
    next_seg = _enrich_segment_for_repair(payload.get("next_segment_debug"))

    if selected:
        selected.setdefault("segment_index", segment_index)
    if prev_seg:
        prev_seg.setdefault("segment_index", segment_index - 1)
    if next_seg:
        next_seg.setdefault("segment_index", segment_index + 1)

    # fallback: if payload has neighbor_segments as a list
    if not selected:
        neighbors = _as_list(payload.get("neighbor_segments"))
        for seg in neighbors:
            segd = _enrich_segment_for_repair(seg)
            idx = segd.get("segment_index")
            if idx == segment_index:
                selected = segd
            elif idx == segment_index - 1:
                prev_seg = segd
            elif idx == segment_index + 1:
                next_seg = segd

    return selected, prev_seg, next_seg


# =====================================================================================
# Saving
# =====================================================================================


def save_output(data: Dict[str, Any], output_dir: Path, segment_index: int, route_id: Optional[int], from_json: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if from_json:
        name = f"route_root_cause_from_json_segment_{segment_index}_{ts}.json"
    else:
        rid = route_id if route_id is not None else "unknown"
        name = f"route_root_cause_{rid}_segment_{segment_index}_{ts}.json"
    path = output_dir / name
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    return path


# =====================================================================================
# Main pipeline
# =====================================================================================


def run(args: argparse.Namespace) -> int:
    payload, source_label = load_payload(args)

    route = _get_route(payload)
    route_id = args.route_id if args.route_id is not None else route.get("id")
    segment_index = args.segment

    selected_segment, previous_segment, next_segment = _get_segment_debugs(payload, segment_index)
    middle_station_ref = _build_middle_station_ref(selected_segment, next_segment)

    complexity = _infer_station_link_complexity(selected_segment, previous_segment, next_segment)
    hypotheses = _build_root_cause_hypotheses(selected_segment, previous_segment, next_segment, complexity)
    anchor_probe_summary = _build_anchor_probe_summary(selected_segment, next_segment, complexity)
    graph_runtime = ProjectGraphRuntime(payload)
    repair_probe = run_mid_station_anchor_repair_probe(
        selected_segment=selected_segment,
        next_segment=next_segment,
        previous_segment=previous_segment,
        graph_runtime=graph_runtime,
    )

    result = {
        "source": source_label,
        "route": route,
        "inferred_regions": _get_inferred_regions(payload),
        "network": _get_network(payload),
        "segment_index": segment_index,
        "middle_station_ref": middle_station_ref,
        "selected_segment_debug": selected_segment,
        "previous_segment_debug": previous_segment,
        "next_segment_debug": next_segment,
        "segment_station_candidates": _as_dict(payload.get("segment_station_candidates")),
        "station_link_complexity_inference": complexity,
        "root_cause_hypotheses": hypotheses,
        "anchor_probe_summary": anchor_probe_summary,
        "mid_station_anchor_repair_probe": repair_probe,
    }

    print_section("START")
    print(f"source = {source_label}")
    print(f"route_id = {route_id}")
    print(f"segment = {segment_index}")

    print_section("ROUTE")
    dump_json(route)

    print_section("INFERRED_REGIONS")
    dump_json(_get_inferred_regions(payload))

    print_section("NETWORK")
    dump_json(_get_network(payload))

    print_section("SELECTED SEGMENT DEBUG")
    print(_segment_brief(segment_index, selected_segment))

    print_section("NEIGHBOR SEGMENTS")
    print(_segment_brief(segment_index - 1, previous_segment))
    print(_segment_brief(segment_index, selected_segment))
    print(_segment_brief(segment_index + 1, next_segment))

    print_section("STATION LINK COMPLEXITY INFERENCE")
    dump_json(complexity)

    print_section("ROOT CAUSE HYPOTHESES")
    for idx, hyp in enumerate(hypotheses, start=1):
        print(f"{idx}. [{hyp.get('severity')}] {hyp.get('code')}")
        print(f"   {hyp.get('message')}")
        print("   details=" + json.dumps(hyp.get("details"), ensure_ascii=False, indent=2, default=str))

    print_section("ANCHOR PROBE SUMMARY")
    dump_json(anchor_probe_summary)

    print_section("MID-STATION ANCHOR REPAIR PROBE")
    dump_json(repair_probe)

    out_path = save_output(
        result,
        output_dir=Path(args.debug_output_dir),
        segment_index=segment_index,
        route_id=route_id,
        from_json=bool(args.from_json),
    )

    print_section("FILE SAVED")
    print(str(out_path))
    return 0


# =====================================================================================
# CLI
# =====================================================================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Route root-cause debugger with anchor repair probe")
    p.add_argument("route_id", nargs="?", type=int, help="Route id (optional when --from-json is used)")
    p.add_argument("--segment", type=int, required=True, help="Segment index to inspect")
    p.add_argument("--from-json", dest="from_json", help="Load already-saved debug payload json")
    p.add_argument(
        "--debug-output-dir",
        default="debug_output",
        help="Directory for reading latest saved json and writing results",
    )
    return p


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_arg_parser().parse_args()))
    except KeyboardInterrupt:
        raise SystemExit(130)
