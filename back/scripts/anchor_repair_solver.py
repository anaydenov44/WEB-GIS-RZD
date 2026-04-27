#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anchor_repair_solver.py

Standalone solver for route anchor/component issues.

What this file does:
- loads a saved debug payload json (or a live payload dict if imported);
- detects a suspicious boundary station between two neighboring segments;
- extracts anchor seeds from incoming/outgoing segment diagnostics;
- searches for a repair plan:
    1) single-anchor repair
    2) dual-anchor boundary repair (different in/out anchors at the same station)
- emits a concrete override object that can be fed into the main matcher.

Important:
This file is written to be resilient to incomplete payloads. It supports two levels:
- diagnostic-only payloads: still localizes the problem;
- richer payloads with station candidates / anchor candidates / chosen pair fields:
  can produce a concrete repair override.

It does NOT depend on your current route matcher internals. You can run it against saved json.
Later you can import solve_anchor_problem(...) directly from main project and use the
returned override in the matcher.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


# ============================================================================
# Data models
# ============================================================================


@dataclass
class SeedCandidate:
    stop_sequence: Optional[int] = None
    station_id: Optional[int] = None
    station_name: Optional[str] = None
    station_name_raw: Optional[str] = None
    node_hash: Optional[str] = None
    component_id: Optional[int] = None
    component_size: Optional[int] = None
    entry_km: Optional[float] = None
    source: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    origin: Optional[str] = None
    mode_name: Optional[str] = None
    path_found: Optional[bool] = None
    chosen_search_mode: Optional[str] = None
    graph_distance_km: Optional[float] = None
    render_total_distance_km: Optional[float] = None
    delta_rzd_km: Optional[float] = None
    relative_error: Optional[float] = None
    rejected_reason: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> Tuple[Any, ...]:
        return (
            self.stop_sequence,
            self.station_id,
            self.node_hash,
            self.component_id,
            self.source,
            round(self.entry_km, 4) if self.entry_km is not None else None,
        )


@dataclass
class SegmentSummary:
    segment_index: int
    from_stop_sequence: Optional[int] = None
    to_stop_sequence: Optional[int] = None
    from_station_id: Optional[int] = None
    to_station_id: Optional[int] = None
    from_station_name: Optional[str] = None
    to_station_name: Optional[str] = None
    from_station_name_raw: Optional[str] = None
    to_station_name_raw: Optional[str] = None
    path_found: Optional[bool] = None
    chosen_search_mode: Optional[str] = None
    delta_rzd_km: Optional[float] = None
    ratio_graph_to_rzd: Optional[float] = None
    best_rejected_pair: Optional[Dict[str, Any]] = None
    chosen_pair: Optional[Dict[str, Any]] = None
    search_modes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateEvaluation:
    kind: str  # single_anchor | dual_anchor_boundary
    score: float
    verdict: str
    reason: str
    incoming_anchor: Optional[SeedCandidate] = None
    outgoing_anchor: Optional[SeedCandidate] = None
    station_context: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SolverResult:
    verdict: str
    suspected_station: Dict[str, Any]
    incoming_segment_index: Optional[int]
    outgoing_segment_index: Optional[int]
    incoming_candidates_count: int
    outgoing_candidates_count: int
    selected_candidate: Optional[CandidateEvaluation]
    alternatives: List[CandidateEvaluation]
    override: Optional[Dict[str, Any]]
    diagnostics: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Generic helpers
# ============================================================================


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _norm_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _round4(value: Any) -> Optional[float]:
    num = _safe_float(value)
    if num is None:
        return None
    return round(num, 4)


def _walk(obj: Any) -> Iterable[Any]:
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item)


def _iter_dicts(obj: Any) -> Iterable[Dict[str, Any]]:
    for item in _walk(obj):
        if isinstance(item, dict):
            yield item


def _find_dicts_with_keys(obj: Any, required_keys: Sequence[str]) -> List[Dict[str, Any]]:
    required = set(required_keys)
    results: List[Dict[str, Any]] = []
    for dct in _iter_dicts(obj):
        if required.issubset(set(dct.keys())):
            results.append(dct)
    return results


def _deep_get(dct: Any, path: Sequence[str], default: Any = None) -> Any:
    cur = dct
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _short_geometry(geometry: Any) -> Any:
    if not isinstance(geometry, dict):
        return geometry
    if geometry.get("type") != "LineString":
        return geometry
    coords = geometry.get("coordinates")
    if not isinstance(coords, list):
        return geometry
    head = coords[:3]
    tail = coords[-2:] if len(coords) > 5 else coords[3:]
    return {
        "type": "LineString",
        "coordinates": {
            "points_count": len(coords),
            "head": head,
            "tail": tail,
            "omitted": max(0, len(coords) - len(head) - len(tail)),
        },
    }


def _best_ratio_from_pair(pair: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(pair, dict):
        return None
    rel = _safe_float(_deep_get(pair, ["transition_diag", "relative_error"]))
    if rel is not None:
        return rel + 1.0
    graph_distance = _safe_float(pair.get("render_total_distance_km") or pair.get("graph_distance_km"))
    delta_rzd = _safe_float(_deep_get(pair, ["transition_diag", "delta_rzd_km"]))
    if graph_distance is not None and delta_rzd and delta_rzd > 0:
        return graph_distance / delta_rzd
    return None


# ============================================================================
# Payload normalization
# ============================================================================


SEGMENT_MODE_KEYS = (
    "station_links_only",
    "station_links_plus_nearby_edges_400m",
    "station_links_plus_nearby_edges_600m",
    "station_links_plus_local_rescue",
    "isolated_component_bridge_last_resort",
)


PAIR_CONTAINER_KEYS = (
    "best_rejected",
    "best_pair",
    "selected_pair",
    "chosen_pair",
    "best_successful_pair",
    "best_success_pair",
)


SUCCESS_PAIR_KEYS = (
    "from_node_hash",
    "to_node_hash",
)


def _normalize_pair(pair: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(pair, dict):
        return None
    normalized = copy.deepcopy(pair)
    if "geometry" in normalized:
        normalized["geometry"] = _short_geometry(normalized["geometry"])
    return normalized


def _extract_pair_from_mode_payload(mode_payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(mode_payload, dict):
        return None
    for key in PAIR_CONTAINER_KEYS:
        pair = mode_payload.get(key)
        if isinstance(pair, dict):
            return _normalize_pair(pair)
    # fallback: mode payload itself looks like a pair
    if set(SUCCESS_PAIR_KEYS).issubset(mode_payload.keys()):
        return _normalize_pair(mode_payload)
    return None


def _extract_segment_search_modes(seg_raw: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    modes: Dict[str, Dict[str, Any]] = {}
    for mode_name in SEGMENT_MODE_KEYS:
        mode_payload = seg_raw.get(mode_name)
        if isinstance(mode_payload, dict):
            modes[mode_name] = copy.deepcopy(mode_payload)

    # sometimes search modes sit under nested dicts
    for key in ("search_modes", "mode_results", "diagnostics", "segment_debug"):
        nested = seg_raw.get(key)
        if isinstance(nested, dict):
            for mode_name in SEGMENT_MODE_KEYS:
                mode_payload = nested.get(mode_name)
                if isinstance(mode_payload, dict) and mode_name not in modes:
                    modes[mode_name] = copy.deepcopy(mode_payload)
    return modes


def _find_best_rejected_from_modes(modes: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best_pair: Optional[Dict[str, Any]] = None
    best_ratio: Optional[float] = None
    for mode_name, payload in modes.items():
        pair = _extract_pair_from_mode_payload(payload)
        if not isinstance(pair, dict):
            continue
        pair.setdefault("_mode_name", mode_name)
        ratio = _best_ratio_from_pair(pair)
        if best_pair is None:
            best_pair = pair
            best_ratio = ratio
            continue
        if ratio is None:
            continue
        if best_ratio is None or ratio < best_ratio:
            best_pair = pair
            best_ratio = ratio
    return best_pair


def _extract_chosen_pair(seg_raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("chosen_pair", "selected_pair", "best_pair", "path_pair", "selected_path_pair"):
        pair = seg_raw.get(key)
        if isinstance(pair, dict):
            return _normalize_pair(pair)

    for key in ("selected_path", "path_result", "chosen_result"):
        nested = seg_raw.get(key)
        if isinstance(nested, dict):
            for subkey in ("pair", "selected_pair", "chosen_pair"):
                pair = nested.get(subkey)
                if isinstance(pair, dict):
                    return _normalize_pair(pair)
            if set(SUCCESS_PAIR_KEYS).issubset(nested.keys()):
                return _normalize_pair(nested)
    return None


def _segment_index_from_dict(seg_raw: Dict[str, Any], fallback: Optional[int]) -> Optional[int]:
    return _safe_int(
        _first_not_none(
            seg_raw.get("segment_index"),
            seg_raw.get("index"),
            seg_raw.get("segment"),
            fallback,
        )
    )


def _make_segment_summary(seg_raw: Dict[str, Any], fallback_index: Optional[int] = None) -> Optional[SegmentSummary]:
    if not isinstance(seg_raw, dict):
        return None
    seg_index = _segment_index_from_dict(seg_raw, fallback_index)
    if seg_index is None:
        return None

    modes = _extract_segment_search_modes(seg_raw)
    best_rejected = _normalize_pair(seg_raw.get("best_rejected_pair")) or _find_best_rejected_from_modes(modes)
    chosen_pair = _extract_chosen_pair(seg_raw)

    from_stop = _safe_int(
        _first_not_none(
            seg_raw.get("from_stop_sequence"),
            _deep_get(seg_raw, ["from_stop", "stop_sequence"]),
        )
    )
    to_stop = _safe_int(
        _first_not_none(
            seg_raw.get("to_stop_sequence"),
            _deep_get(seg_raw, ["to_stop", "stop_sequence"]),
        )
    )

    summary = SegmentSummary(
        segment_index=seg_index,
        from_stop_sequence=from_stop,
        to_stop_sequence=to_stop,
        from_station_id=_safe_int(_first_not_none(seg_raw.get("from_station_id"), _deep_get(seg_raw, ["from_stop", "station_id"]), _deep_get(seg_raw, ["from_stop", "locked_station_id"]))),
        to_station_id=_safe_int(_first_not_none(seg_raw.get("to_station_id"), _deep_get(seg_raw, ["to_stop", "station_id"]), _deep_get(seg_raw, ["to_stop", "locked_station_id"]))),
        from_station_name=_norm_text(_first_not_none(seg_raw.get("from_station_name"), _deep_get(seg_raw, ["from_stop", "station_name"]), _deep_get(seg_raw, ["from_stop", "locked_station_name"]))),
        to_station_name=_norm_text(_first_not_none(seg_raw.get("to_station_name"), _deep_get(seg_raw, ["to_stop", "station_name"]), _deep_get(seg_raw, ["to_stop", "locked_station_name"]))),
        from_station_name_raw=_norm_text(_first_not_none(seg_raw.get("from_station_name_raw"), _deep_get(seg_raw, ["from_stop", "station_name_raw"]))),
        to_station_name_raw=_norm_text(_first_not_none(seg_raw.get("to_station_name_raw"), _deep_get(seg_raw, ["to_stop", "station_name_raw"]))),
        path_found=seg_raw.get("path_found"),
        chosen_search_mode=_norm_text(seg_raw.get("chosen_search_mode")),
        delta_rzd_km=_safe_float(seg_raw.get("delta_rzd_km")),
        ratio_graph_to_rzd=_safe_float(seg_raw.get("ratio_graph_to_rzd")),
        best_rejected_pair=best_rejected,
        chosen_pair=chosen_pair,
        search_modes=modes,
        raw=copy.deepcopy(seg_raw),
    )
    return summary


def _collect_segments(payload: Dict[str, Any]) -> Dict[int, SegmentSummary]:
    segments: Dict[int, SegmentSummary] = {}

    # explicit arrays/maps
    for key in ("segments", "segment_debug", "segments_debug", "route_segments", "neighbor_segments"):
        container = payload.get(key)
        if isinstance(container, list):
            for idx, item in enumerate(container):
                seg = _make_segment_summary(item, fallback_index=idx)
                if seg is not None:
                    segments[seg.segment_index] = seg
        elif isinstance(container, dict):
            for raw_key, item in container.items():
                fallback = _safe_int(raw_key)
                seg = _make_segment_summary(item, fallback_index=fallback)
                if seg is not None:
                    segments[seg.segment_index] = seg

    # selected + neighbors often stored separately
    for key, fallback in (("selected_segment_debug", None), ("selected_segment", None)):
        seg = _make_segment_summary(payload.get(key, {}), fallback_index=fallback)
        if seg is not None:
            segments[seg.segment_index] = seg

    # broad search across dicts with segment-like keys
    for dct in _iter_dicts(payload):
        if not isinstance(dct, dict):
            continue
        if not any(k in dct for k in ("segment_index", "chosen_search_mode", "path_found")):
            continue
        if not any(k in dct for k in SEGMENT_MODE_KEYS) and "best_rejected_pair" not in dct and "chosen_pair" not in dct:
            continue
        seg = _make_segment_summary(dct)
        if seg is not None:
            segments.setdefault(seg.segment_index, seg)

    return dict(sorted(segments.items(), key=lambda item: item[0]))


def _extract_route_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    return {
        "id": _first_not_none(route.get("id"), payload.get("route_id")),
        "train_number": _first_not_none(route.get("train_number"), payload.get("train_number")),
        "route_name": _first_not_none(route.get("route_name"), payload.get("route_name")),
        "snapshot_date": _first_not_none(route.get("snapshot_date"), payload.get("snapshot_date")),
    }


def _extract_network_meta(payload: Dict[str, Any]) -> Dict[str, Any]:
    network = payload.get("network") if isinstance(payload.get("network"), dict) else {}
    return {
        "network_mode": _first_not_none(network.get("network_mode"), payload.get("network_mode")),
        "region_codes": _first_not_none(network.get("region_codes"), payload.get("region_codes")),
        "scope_key": _first_not_none(network.get("scope_key"), payload.get("scope_key")),
        "visible_stations_count": _first_not_none(network.get("visible_stations_count"), payload.get("visible_stations_count")),
        "adjacency_node_count": _first_not_none(network.get("adjacency_node_count"), payload.get("adjacency_node_count")),
        "directed_edge_count": _first_not_none(network.get("directed_edge_count"), payload.get("directed_edge_count")),
        "topology_station_links_count": _first_not_none(network.get("topology_station_links_count"), payload.get("topology_station_links_count")),
    }


# ============================================================================
# Seed extraction
# ============================================================================


def _seed_from_pair_endpoint(
    pair: Dict[str, Any],
    endpoint: str,
    stop_sequence: Optional[int],
    station_id: Optional[int],
    station_name: Optional[str],
    station_name_raw: Optional[str],
    origin: str,
    mode_name: Optional[str],
    path_found: Optional[bool],
    chosen_search_mode: Optional[str],
    delta_rzd_km: Optional[float],
) -> Optional[SeedCandidate]:
    prefix = "from" if endpoint == "from" else "to"
    node_hash = _norm_text(pair.get(f"{prefix}_node_hash"))
    if node_hash is None:
        return None

    coord = None
    geometry = pair.get("geometry")
    if isinstance(geometry, dict) and geometry.get("type") == "LineString":
        coords = geometry.get("coordinates")
        if isinstance(coords, list) and coords:
            coord = coords[0] if endpoint == "from" else coords[-1]
        elif isinstance(coords, dict):
            head = coords.get("head") or []
            tail = coords.get("tail") or []
            coord = head[0] if endpoint == "from" and head else (tail[-1] if endpoint == "to" and tail else None)

    lat, lon = None, None
    if isinstance(coord, list) and len(coord) >= 2:
        lon = _safe_float(coord[0])
        lat = _safe_float(coord[1])

    return SeedCandidate(
        stop_sequence=stop_sequence,
        station_id=station_id,
        station_name=station_name,
        station_name_raw=station_name_raw,
        node_hash=node_hash,
        component_id=_safe_int(pair.get(f"{prefix}_component_id")),
        component_size=_safe_int(pair.get(f"{prefix}_component_size")),
        entry_km=_safe_float(pair.get(f"{prefix}_entry_km")),
        source=_norm_text(pair.get(f"{prefix}_source")),
        lat=lat,
        lon=lon,
        origin=origin,
        mode_name=mode_name,
        path_found=path_found,
        chosen_search_mode=chosen_search_mode,
        graph_distance_km=_safe_float(pair.get("graph_distance_km")),
        render_total_distance_km=_safe_float(pair.get("render_total_distance_km")),
        delta_rzd_km=delta_rzd_km,
        relative_error=_safe_float(_deep_get(pair, ["transition_diag", "relative_error"])),
        rejected_reason=_norm_text(_first_not_none(pair.get("rejected_reason"), _deep_get(pair, ["transition_diag", "rejected_reason"]))),
        raw=copy.deepcopy(pair),
    )


def _extract_explicit_anchor_candidates(payload: Dict[str, Any], suspected_stop_sequence: Optional[int]) -> List[SeedCandidate]:
    seeds: List[SeedCandidate] = []
    candidate_containers: List[Dict[str, Any]] = []

    for dct in _iter_dicts(payload):
        if not isinstance(dct, dict):
            continue
        if "anchor_candidates" in dct and isinstance(dct.get("anchor_candidates"), list):
            candidate_containers.append(dct)
        elif "seed_candidates" in dct and isinstance(dct.get("seed_candidates"), list):
            candidate_containers.append(dct)
        elif "topology_attachments" in dct and isinstance(dct.get("topology_attachments"), list):
            candidate_containers.append(dct)
        elif "station_links" in dct and isinstance(dct.get("station_links"), list):
            candidate_containers.append(dct)

    for container in candidate_containers:
        stop_seq = _safe_int(_first_not_none(container.get("stop_sequence"), _deep_get(container, ["station", "stop_sequence"])))
        if suspected_stop_sequence is not None and stop_seq is not None and stop_seq != suspected_stop_sequence:
            continue

        station_id = _safe_int(_first_not_none(container.get("station_id"), _deep_get(container, ["station", "station_id"]), _deep_get(container, ["station", "locked_station_id"])))
        station_name = _norm_text(_first_not_none(container.get("station_name"), _deep_get(container, ["station", "station_name"]), _deep_get(container, ["station", "locked_station_name"])))
        station_name_raw = _norm_text(_first_not_none(container.get("station_name_raw"), _deep_get(container, ["station", "station_name_raw"])))

        for list_key in ("anchor_candidates", "seed_candidates", "topology_attachments", "station_links"):
            raw_list = container.get(list_key)
            if not isinstance(raw_list, list):
                continue
            for item in raw_list:
                if not isinstance(item, dict):
                    continue
                node_hash = _norm_text(_first_not_none(item.get("node_hash"), item.get("hash")))
                if node_hash is None:
                    continue
                coord = item.get("coord") or item.get("coordinates")
                lat, lon = None, None
                if isinstance(coord, list) and len(coord) >= 2:
                    lon = _safe_float(coord[0])
                    lat = _safe_float(coord[1])
                seeds.append(
                    SeedCandidate(
                        stop_sequence=stop_seq,
                        station_id=station_id,
                        station_name=station_name,
                        station_name_raw=station_name_raw,
                        node_hash=node_hash,
                        component_id=_safe_int(item.get("component_id")),
                        component_size=_safe_int(item.get("component_size")),
                        entry_km=_safe_float(item.get("entry_km")),
                        source=_norm_text(_first_not_none(item.get("source"), list_key[:-1] if list_key.endswith("s") else list_key)),
                        lat=lat,
                        lon=lon,
                        origin=f"explicit:{list_key}",
                        raw=copy.deepcopy(item),
                    )
                )
    return seeds


def _extract_station_candidates(payload: Dict[str, Any], suspected_stop_sequence: Optional[int]) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    for dct in _iter_dicts(payload):
        if not isinstance(dct, dict):
            continue
        if "candidates" not in dct or not isinstance(dct.get("candidates"), list):
            continue
        stop_seq = _safe_int(_first_not_none(dct.get("stop_sequence"), _deep_get(dct, ["station", "stop_sequence"])))
        if suspected_stop_sequence is not None and stop_seq is not None and stop_seq != suspected_stop_sequence:
            continue
        if any("station_id" in item or "station_name" in item for item in dct["candidates"] if isinstance(item, dict)):
            results.append(copy.deepcopy(dct))
    return results


def _dedupe_seeds(seeds: List[SeedCandidate]) -> List[SeedCandidate]:
    seen: Set[Tuple[Any, ...]] = set()
    results: List[SeedCandidate] = []
    for seed in seeds:
        key = seed.key()
        if key in seen:
            continue
        seen.add(key)
        results.append(seed)
    return results


def _extract_boundary_seeds(
    incoming: Optional[SegmentSummary],
    outgoing: Optional[SegmentSummary],
    suspected_stop_sequence: Optional[int],
    suspected_station_id: Optional[int],
    suspected_station_name: Optional[str],
    suspected_station_name_raw: Optional[str],
    payload: Dict[str, Any],
) -> Tuple[List[SeedCandidate], List[SeedCandidate], Dict[str, Any]]:
    incoming_seeds: List[SeedCandidate] = []
    outgoing_seeds: List[SeedCandidate] = []
    diagnostics: Dict[str, Any] = {
        "incoming_sources": [],
        "outgoing_sources": [],
    }

    # explicit anchor candidates belong to both in/out pools until classified later
    explicit = _extract_explicit_anchor_candidates(payload, suspected_stop_sequence)
    if explicit:
        diagnostics["explicit_anchor_candidates_count"] = len(explicit)

    # incoming segment: suspected station is normally the TO endpoint
    if incoming is not None:
        if incoming.best_rejected_pair:
            pair = incoming.best_rejected_pair
            seed = _seed_from_pair_endpoint(
                pair=pair,
                endpoint="to",
                stop_sequence=suspected_stop_sequence or incoming.to_stop_sequence,
                station_id=suspected_station_id or incoming.to_station_id,
                station_name=suspected_station_name or incoming.to_station_name,
                station_name_raw=suspected_station_name_raw or incoming.to_station_name_raw,
                origin="incoming:best_rejected:to",
                mode_name=_norm_text(pair.get("_mode_name")),
                path_found=incoming.path_found,
                chosen_search_mode=incoming.chosen_search_mode,
                delta_rzd_km=incoming.delta_rzd_km,
            )
            if seed:
                incoming_seeds.append(seed)
                diagnostics["incoming_sources"].append(seed.origin)

        if incoming.chosen_pair:
            seed = _seed_from_pair_endpoint(
                pair=incoming.chosen_pair,
                endpoint="to",
                stop_sequence=suspected_stop_sequence or incoming.to_stop_sequence,
                station_id=suspected_station_id or incoming.to_station_id,
                station_name=suspected_station_name or incoming.to_station_name,
                station_name_raw=suspected_station_name_raw or incoming.to_station_name_raw,
                origin="incoming:chosen_pair:to",
                mode_name=incoming.chosen_search_mode,
                path_found=incoming.path_found,
                chosen_search_mode=incoming.chosen_search_mode,
                delta_rzd_km=incoming.delta_rzd_km,
            )
            if seed:
                incoming_seeds.append(seed)
                diagnostics["incoming_sources"].append(seed.origin)

        for mode_name, mode_payload in incoming.search_modes.items():
            pair = _extract_pair_from_mode_payload(mode_payload)
            if pair:
                seed = _seed_from_pair_endpoint(
                    pair=pair,
                    endpoint="to",
                    stop_sequence=suspected_stop_sequence or incoming.to_stop_sequence,
                    station_id=suspected_station_id or incoming.to_station_id,
                    station_name=suspected_station_name or incoming.to_station_name,
                    station_name_raw=suspected_station_name_raw or incoming.to_station_name_raw,
                    origin=f"incoming:{mode_name}:to",
                    mode_name=mode_name,
                    path_found=mode_payload.get("path_found", incoming.path_found),
                    chosen_search_mode=incoming.chosen_search_mode,
                    delta_rzd_km=incoming.delta_rzd_km,
                )
                if seed:
                    incoming_seeds.append(seed)

    # outgoing segment: suspected station is normally the FROM endpoint
    if outgoing is not None:
        if outgoing.best_rejected_pair:
            pair = outgoing.best_rejected_pair
            seed = _seed_from_pair_endpoint(
                pair=pair,
                endpoint="from",
                stop_sequence=suspected_stop_sequence or outgoing.from_stop_sequence,
                station_id=suspected_station_id or outgoing.from_station_id,
                station_name=suspected_station_name or outgoing.from_station_name,
                station_name_raw=suspected_station_name_raw or outgoing.from_station_name_raw,
                origin="outgoing:best_rejected:from",
                mode_name=_norm_text(pair.get("_mode_name")),
                path_found=outgoing.path_found,
                chosen_search_mode=outgoing.chosen_search_mode,
                delta_rzd_km=outgoing.delta_rzd_km,
            )
            if seed:
                outgoing_seeds.append(seed)
                diagnostics["outgoing_sources"].append(seed.origin)

        if outgoing.chosen_pair:
            seed = _seed_from_pair_endpoint(
                pair=outgoing.chosen_pair,
                endpoint="from",
                stop_sequence=suspected_stop_sequence or outgoing.from_stop_sequence,
                station_id=suspected_station_id or outgoing.from_station_id,
                station_name=suspected_station_name or outgoing.from_station_name,
                station_name_raw=suspected_station_name_raw or outgoing.from_station_name_raw,
                origin="outgoing:chosen_pair:from",
                mode_name=outgoing.chosen_search_mode,
                path_found=outgoing.path_found,
                chosen_search_mode=outgoing.chosen_search_mode,
                delta_rzd_km=outgoing.delta_rzd_km,
            )
            if seed:
                outgoing_seeds.append(seed)
                diagnostics["outgoing_sources"].append(seed.origin)

        for mode_name, mode_payload in outgoing.search_modes.items():
            pair = _extract_pair_from_mode_payload(mode_payload)
            if pair:
                seed = _seed_from_pair_endpoint(
                    pair=pair,
                    endpoint="from",
                    stop_sequence=suspected_stop_sequence or outgoing.from_stop_sequence,
                    station_id=suspected_station_id or outgoing.from_station_id,
                    station_name=suspected_station_name or outgoing.from_station_name,
                    station_name_raw=suspected_station_name_raw or outgoing.from_station_name_raw,
                    origin=f"outgoing:{mode_name}:from",
                    mode_name=mode_name,
                    path_found=mode_payload.get("path_found", outgoing.path_found),
                    chosen_search_mode=outgoing.chosen_search_mode,
                    delta_rzd_km=outgoing.delta_rzd_km,
                )
                if seed:
                    outgoing_seeds.append(seed)

    # add explicit seeds to both pools – evaluator will decide how to use them
    if explicit:
        incoming_seeds.extend(copy.deepcopy(explicit))
        outgoing_seeds.extend(copy.deepcopy(explicit))

    incoming_seeds = _dedupe_seeds(incoming_seeds)
    outgoing_seeds = _dedupe_seeds(outgoing_seeds)
    diagnostics["incoming_candidates_count"] = len(incoming_seeds)
    diagnostics["outgoing_candidates_count"] = len(outgoing_seeds)
    return incoming_seeds, outgoing_seeds, diagnostics


# ============================================================================
# Suspicion + repair scoring
# ============================================================================


def _infer_suspected_station(
    incoming: Optional[SegmentSummary],
    outgoing: Optional[SegmentSummary],
    selected_index: int,
) -> Dict[str, Any]:
    # boundary station between selected segment and the next segment
    stop_sequence = _first_not_none(
        incoming.to_stop_sequence if incoming else None,
        outgoing.from_stop_sequence if outgoing else None,
        selected_index + 1,
    )
    station_id = _first_not_none(
        incoming.to_station_id if incoming else None,
        outgoing.from_station_id if outgoing else None,
    )
    station_name = _first_not_none(
        incoming.to_station_name if incoming else None,
        outgoing.from_station_name if outgoing else None,
    )
    station_name_raw = _first_not_none(
        incoming.to_station_name_raw if incoming else None,
        outgoing.from_station_name_raw if outgoing else None,
        station_name,
    )
    return {
        "stop_sequence": stop_sequence,
        "station_id": station_id,
        "station_name": station_name,
        "station_name_raw": station_name_raw,
    }


def _score_seed_quality(seed: SeedCandidate) -> float:
    score = 0.0

    if seed.component_id is not None:
        score += 3.0
    if seed.component_size is not None:
        score += min(3.0, math.log10(max(seed.component_size, 1) + 1.0))
    if seed.source == "station_link":
        score += 2.0
    elif seed.source and seed.source.startswith("edge_"):
        score += 1.2
    elif seed.source:
        score += 0.5

    if seed.entry_km is not None:
        score += max(0.0, 2.5 - min(seed.entry_km, 2.5))

    if seed.path_found is True:
        score += 3.0
    elif seed.path_found is False:
        score -= 0.5

    if seed.rejected_reason == "no_graph_path":
        score -= 4.0
    elif seed.rejected_reason == "graph_path_absurd_detour":
        score -= 1.5

    if seed.relative_error is not None:
        score += max(-3.0, 2.0 - seed.relative_error * 2.0)

    if seed.chosen_search_mode == "isolated_component_bridge_last_resort":
        score -= 1.0

    return score


def _evaluate_single_anchor(
    seed: SeedCandidate,
    suspected_station: Dict[str, Any],
    incoming: Optional[SegmentSummary],
    outgoing: Optional[SegmentSummary],
) -> CandidateEvaluation:
    score = 0.0
    reasons: List[str] = []
    diagnostics: Dict[str, Any] = {}

    score += _score_seed_quality(seed)
    diagnostics["seed_quality"] = round(_score_seed_quality(seed), 4)

    incoming_component = None
    if incoming and incoming.best_rejected_pair:
        incoming_component = _safe_int(incoming.best_rejected_pair.get("to_component_id"))
    outgoing_component = None
    if outgoing and outgoing.best_rejected_pair:
        outgoing_component = _safe_int(outgoing.best_rejected_pair.get("from_component_id"))

    diagnostics["incoming_component_hint"] = incoming_component
    diagnostics["outgoing_component_hint"] = outgoing_component

    if seed.component_id is not None and incoming_component is not None and seed.component_id == incoming_component:
        score += 3.0
        reasons.append("matches incoming boundary component")
    if seed.component_id is not None and outgoing_component is not None and seed.component_id == outgoing_component:
        score += 3.0
        reasons.append("matches outgoing boundary component")

    if incoming_component is not None and outgoing_component is not None and incoming_component != outgoing_component:
        if seed.component_id in {incoming_component, outgoing_component}:
            score += 1.0
            reasons.append("helps one side of component split")
        else:
            score -= 2.0
            reasons.append("matches neither side of component split")

    # prefer named / locked station candidate if present
    if suspected_station.get("station_id") is not None and seed.station_id == suspected_station.get("station_id"):
        score += 1.0
    if suspected_station.get("station_name") and seed.station_name == suspected_station.get("station_name"):
        score += 0.5

    verdict = "candidate"
    if score >= 9.0:
        verdict = "strong_candidate"
    elif score >= 6.0:
        verdict = "good_candidate"
    elif score < 2.0:
        verdict = "weak_candidate"

    return CandidateEvaluation(
        kind="single_anchor",
        score=round(score, 4),
        verdict=verdict,
        reason=", ".join(reasons) if reasons else "heuristic single-anchor candidate",
        incoming_anchor=seed,
        outgoing_anchor=seed,
        station_context=copy.deepcopy(suspected_station),
        diagnostics=diagnostics,
    )


def _evaluate_dual_anchor(
    incoming_seed: SeedCandidate,
    outgoing_seed: SeedCandidate,
    suspected_station: Dict[str, Any],
) -> CandidateEvaluation:
    score = 0.0
    reasons: List[str] = []
    diagnostics: Dict[str, Any] = {}

    in_score = _score_seed_quality(incoming_seed)
    out_score = _score_seed_quality(outgoing_seed)
    score += in_score + out_score
    diagnostics["incoming_seed_quality"] = round(in_score, 4)
    diagnostics["outgoing_seed_quality"] = round(out_score, 4)

    same_node = incoming_seed.node_hash and outgoing_seed.node_hash and incoming_seed.node_hash == outgoing_seed.node_hash
    same_component = (
        incoming_seed.component_id is not None
        and outgoing_seed.component_id is not None
        and incoming_seed.component_id == outgoing_seed.component_id
    )
    diagnostics["same_node"] = same_node
    diagnostics["same_component"] = same_component

    if same_node:
        score += 3.0
        reasons.append("same node works for both sides")
    elif same_component:
        score += 1.5
        reasons.append("same component on both sides")
    else:
        # allow dual-anchor repair across different components at same station
        score -= 0.5
        reasons.append("dual-anchor boundary transfer")

    # station-link on at least one side is good
    if incoming_seed.source == "station_link" or outgoing_seed.source == "station_link":
        score += 0.8

    # very different coordinates -> likely bad unless components differ and station is large
    if all(v is not None for v in (incoming_seed.lat, incoming_seed.lon, outgoing_seed.lat, outgoing_seed.lon)):
        dx = incoming_seed.lon - outgoing_seed.lon
        dy = incoming_seed.lat - outgoing_seed.lat
        dist = math.sqrt(dx * dx + dy * dy)
        diagnostics["approx_degree_distance"] = round(dist, 6)
        if dist < 0.005:
            score += 0.8
            reasons.append("anchors are spatially close")
        elif dist > 0.05:
            score -= 2.0
            reasons.append("anchors are too far apart for one station")

    verdict = "candidate"
    if score >= 12.0:
        verdict = "strong_candidate"
    elif score >= 8.0:
        verdict = "good_candidate"
    elif score < 3.0:
        verdict = "weak_candidate"

    return CandidateEvaluation(
        kind="dual_anchor_boundary",
        score=round(score, 4),
        verdict=verdict,
        reason=", ".join(reasons) if reasons else "heuristic dual-anchor boundary repair",
        incoming_anchor=incoming_seed,
        outgoing_anchor=outgoing_seed,
        station_context=copy.deepcopy(suspected_station),
        diagnostics=diagnostics,
    )


def _sort_evaluations(evaluations: List[CandidateEvaluation]) -> List[CandidateEvaluation]:
    return sorted(
        evaluations,
        key=lambda item: (
            item.score,
            1 if item.kind == "single_anchor" else 0,
            1 if item.verdict == "strong_candidate" else 0,
        ),
        reverse=True,
    )


def _build_override(candidate: CandidateEvaluation) -> Optional[Dict[str, Any]]:
    if candidate.incoming_anchor is None and candidate.outgoing_anchor is None:
        return None

    station = candidate.station_context
    override: Dict[str, Any] = {
        "repair_kind": candidate.kind,
        "stop_sequence": station.get("stop_sequence"),
        "station_id": station.get("station_id"),
        "station_name": station.get("station_name"),
        "station_name_raw": station.get("station_name_raw"),
        "reason": candidate.reason,
        "score": candidate.score,
    }

    if candidate.incoming_anchor is not None:
        override["incoming_anchor"] = {
            "node_hash": candidate.incoming_anchor.node_hash,
            "component_id": candidate.incoming_anchor.component_id,
            "component_size": candidate.incoming_anchor.component_size,
            "entry_km": candidate.incoming_anchor.entry_km,
            "source": candidate.incoming_anchor.source,
            "origin": candidate.incoming_anchor.origin,
        }
    if candidate.outgoing_anchor is not None:
        override["outgoing_anchor"] = {
            "node_hash": candidate.outgoing_anchor.node_hash,
            "component_id": candidate.outgoing_anchor.component_id,
            "component_size": candidate.outgoing_anchor.component_size,
            "entry_km": candidate.outgoing_anchor.entry_km,
            "source": candidate.outgoing_anchor.source,
            "origin": candidate.outgoing_anchor.origin,
        }
    return override


def solve_anchor_problem(payload: Dict[str, Any], segment_index: int) -> SolverResult:
    segments = _collect_segments(payload)
    incoming = segments.get(segment_index)
    outgoing = segments.get(segment_index + 1)
    previous_seg = segments.get(segment_index - 1)

    if incoming is None:
        raise ValueError(f"Не найден сегмент {segment_index} в payload")

    suspected_station = _infer_suspected_station(incoming, outgoing, segment_index)

    in_seeds, out_seeds, seed_diag = _extract_boundary_seeds(
        incoming=incoming,
        outgoing=outgoing,
        suspected_stop_sequence=_safe_int(suspected_station.get("stop_sequence")),
        suspected_station_id=_safe_int(suspected_station.get("station_id")),
        suspected_station_name=_norm_text(suspected_station.get("station_name")),
        suspected_station_name_raw=_norm_text(suspected_station.get("station_name_raw")),
        payload=payload,
    )

    station_candidates = _extract_station_candidates(payload, _safe_int(suspected_station.get("stop_sequence")))

    evaluations: List[CandidateEvaluation] = []
    for seed in in_seeds:
        evaluations.append(_evaluate_single_anchor(seed, suspected_station, incoming, outgoing))
    for seed in out_seeds:
        evaluations.append(_evaluate_single_anchor(seed, suspected_station, incoming, outgoing))

    max_dual = 36
    dual_pairs_checked = 0
    for in_seed in in_seeds:
        for out_seed in out_seeds:
            dual_pairs_checked += 1
            if dual_pairs_checked > max_dual:
                break
            evaluations.append(_evaluate_dual_anchor(in_seed, out_seed, suspected_station))
        if dual_pairs_checked > max_dual:
            break

    evaluations = _sort_evaluations(evaluations)
    selected = evaluations[0] if evaluations else None
    override = _build_override(selected) if selected else None

    if selected is None:
        verdict = "no_repair_candidates"
    elif selected.kind == "dual_anchor_boundary":
        verdict = "repairable_with_dual_anchor_boundary"
    elif selected.kind == "single_anchor":
        verdict = "repairable_with_single_anchor"
    else:
        verdict = "repairable"

    diagnostics: Dict[str, Any] = {
        "route": _extract_route_meta(payload),
        "network": _extract_network_meta(payload),
        "previous_segment_index": previous_seg.segment_index if previous_seg else None,
        "incoming_segment_index": incoming.segment_index if incoming else None,
        "outgoing_segment_index": outgoing.segment_index if outgoing else None,
        "incoming_path_found": incoming.path_found if incoming else None,
        "outgoing_path_found": outgoing.path_found if outgoing else None,
        "incoming_chosen_search_mode": incoming.chosen_search_mode if incoming else None,
        "outgoing_chosen_search_mode": outgoing.chosen_search_mode if outgoing else None,
        "incoming_ratio_graph_to_rzd": incoming.ratio_graph_to_rzd if incoming else None,
        "incoming_best_rejected_pair": _normalize_pair(incoming.best_rejected_pair) if incoming and incoming.best_rejected_pair else None,
        "outgoing_best_rejected_pair": _normalize_pair(outgoing.best_rejected_pair) if outgoing and outgoing.best_rejected_pair else None,
        "seed_diagnostics": seed_diag,
        "station_candidates_count": len(station_candidates),
        "station_candidates_preview": station_candidates[:2],
        "evaluations_checked": len(evaluations),
        "dual_pairs_checked": min(dual_pairs_checked, max_dual),
    }

    return SolverResult(
        verdict=verdict,
        suspected_station=suspected_station,
        incoming_segment_index=incoming.segment_index if incoming else None,
        outgoing_segment_index=outgoing.segment_index if outgoing else None,
        incoming_candidates_count=len(in_seeds),
        outgoing_candidates_count=len(out_seeds),
        selected_candidate=selected,
        alternatives=evaluations[1:6] if len(evaluations) > 1 else [],
        override=override,
        diagnostics=diagnostics,
    )


# ============================================================================
# Output formatting
# ============================================================================


def _seed_to_json(seed: Optional[SeedCandidate]) -> Optional[Dict[str, Any]]:
    if seed is None:
        return None
    raw = asdict(seed)
    # raw payload too noisy
    raw.pop("raw", None)
    return raw


def _candidate_to_json(candidate: Optional[CandidateEvaluation]) -> Optional[Dict[str, Any]]:
    if candidate is None:
        return None
    return {
        "kind": candidate.kind,
        "score": candidate.score,
        "verdict": candidate.verdict,
        "reason": candidate.reason,
        "station_context": candidate.station_context,
        "incoming_anchor": _seed_to_json(candidate.incoming_anchor),
        "outgoing_anchor": _seed_to_json(candidate.outgoing_anchor),
        "diagnostics": candidate.diagnostics,
    }


def result_to_jsonable(result: SolverResult) -> Dict[str, Any]:
    return {
        "verdict": result.verdict,
        "suspected_station": result.suspected_station,
        "incoming_segment_index": result.incoming_segment_index,
        "outgoing_segment_index": result.outgoing_segment_index,
        "incoming_candidates_count": result.incoming_candidates_count,
        "outgoing_candidates_count": result.outgoing_candidates_count,
        "selected_candidate": _candidate_to_json(result.selected_candidate),
        "alternatives": [_candidate_to_json(item) for item in result.alternatives],
        "override": result.override,
        "diagnostics": result.diagnostics,
    }


def _print_block(title: str, value: Any) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)
    if isinstance(value, str):
        print(value)
    else:
        print(_json_dump(value))


# ============================================================================
# CLI
# ============================================================================


def _load_json_file(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _save_result_json(result: Dict[str, Any], output_dir: str, stem: str) -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = Path(output_dir) / f"{stem}_{ts}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    return str(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Solve anchor/component boundary issues from debug payload json.",
    )
    parser.add_argument("--from-json", dest="from_json", help="Path to saved debug payload json")
    parser.add_argument("--segment", dest="segment", type=int, required=True, help="Incoming segment index to inspect")
    parser.add_argument("--output-dir", dest="output_dir", default="debug_output", help="Directory for saved solver result json")
    parser.add_argument("--print-json-only", action="store_true", help="Print only final json")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.from_json:
        parser.error("Пока этот standalone-файл работает через --from-json")

    payload = _load_json_file(args.from_json)
    result = solve_anchor_problem(payload, args.segment)
    result_json = result_to_jsonable(result)

    if args.print_json_only:
        print(_json_dump(result_json))
    else:
        _print_block("START", {"source": f"json:{args.from_json}", "segment": args.segment})
        _print_block("ROUTE", result.diagnostics.get("route"))
        _print_block("NETWORK", result.diagnostics.get("network"))
        _print_block(
            "SUSPECTED STATION",
            result.suspected_station,
        )
        _print_block(
            "SEED EXTRACTION",
            {
                "incoming_candidates_count": result.incoming_candidates_count,
                "outgoing_candidates_count": result.outgoing_candidates_count,
                **result.diagnostics.get("seed_diagnostics", {}),
            },
        )
        _print_block("SOLVER RESULT", {
            "verdict": result.verdict,
            "selected_candidate": _candidate_to_json(result.selected_candidate),
            "override": result.override,
        })
        if result.alternatives:
            _print_block("ALTERNATIVES", [_candidate_to_json(item) for item in result.alternatives])

    stem = f"anchor_repair_solver_segment_{args.segment}"
    saved_path = _save_result_json(result_json, args.output_dir, stem)
    if not args.print_json_only:
        _print_block("FILE SAVED", saved_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
