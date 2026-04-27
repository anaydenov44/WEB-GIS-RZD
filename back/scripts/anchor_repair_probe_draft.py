#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Черновик probe-скрипта для проверки логики repair middle-anchor.

Что он умеет уже сейчас:
1. Читать json от твоего текущего дебагера.
2. Распознавать сигнатуру anchor problem:
   - входящий сегмент дает absurd detour
   - исходящий сегмент не строится обычным graph path
3. Вытаскивать station candidates для промежуточной станции.
4. Формировать список candidate anchors для тестирования.
5. Печатать понятный план, что именно надо прогнать на реальном matcher.
6. Работать в двух режимах:
   - heuristic-only: только по json, без доступа к matcher
   - executable-probe: если ты потом подключишь реальные callbacks

Что он НЕ умеет без интеграции:
- реально прогонять путь по graph для каждого альтернативного anchor
- автоматически чинить маршрут в проде

Это отдельный тестовый файл, чтобы сначала обкатать логику repair-а,
а потом уже встраивать в основной matcher.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# =============================================================================
# dataclasses
# =============================================================================

@dataclass
class AnchorSeed:
    station_id: Optional[int]
    station_name: Optional[str]
    source: str
    node_hash: Optional[str] = None
    component_id: Optional[int] = None
    entry_km: Optional[float] = None
    region_code: Optional[str] = None
    score_hint: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SegmentEval:
    path_found: Optional[bool]
    search_mode: Optional[str]
    ratio_graph_to_rzd: Optional[float]
    rejected_reason: Optional[str]
    graph_distance_km: Optional[float] = None
    render_total_distance_km: Optional[float] = None
    component_id_from: Optional[int] = None
    component_id_to: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateProbeResult:
    candidate_anchor: AnchorSeed
    incoming_eval: Optional[SegmentEval]
    outgoing_eval: Optional[SegmentEval]
    score: float
    accepted: bool
    notes: List[str] = field(default_factory=list)


# =============================================================================
# utils
# =============================================================================

def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def compact_geometry(obj: Any, max_head: int = 3, max_tail: int = 2) -> Any:
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == "coordinates" and isinstance(v, list) and len(v) > (max_head + max_tail + 3):
                result[k] = {
                    "points_count": len(v),
                    "head": v[:max_head],
                    "tail": v[-max_tail:],
                    "omitted": len(v) - max_head - max_tail,
                }
            else:
                result[k] = compact_geometry(v, max_head=max_head, max_tail=max_tail)
        return result
    if isinstance(obj, list):
        return [compact_geometry(x, max_head=max_head, max_tail=max_tail) for x in obj]
    return obj


def print_section(title: str) -> None:
    print()
    print("=" * 80)
    print(title)
    print("=" * 80)


def print_json(obj: Any) -> None:
    print(json.dumps(compact_geometry(obj), ensure_ascii=False, indent=2))


def get_latest_matching_json(debug_dir: Path, route_id: Optional[int], segment: Optional[int]) -> Optional[Path]:
    if not debug_dir.exists():
        return None

    candidates = [p for p in debug_dir.glob("*.json") if p.name.startswith("route_root_cause_")]
    if not candidates:
        return None

    if route_id is not None and segment is not None:
        prefix = f"route_root_cause_{route_id}_segment_{segment}_"
        filtered = [p for p in candidates if p.name.startswith(prefix)]
        if filtered:
            return max(filtered, key=lambda p: p.stat().st_mtime)

    return max(candidates, key=lambda p: p.stat().st_mtime)


# =============================================================================
# json extraction helpers
# =============================================================================

def get_selected_segment_index(payload: Dict[str, Any], cli_segment: Optional[int]) -> Optional[int]:
    if cli_segment is not None:
        return cli_segment
    value = payload.get("selected_segment_index")
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def get_route_brief(payload: Dict[str, Any]) -> Dict[str, Any]:
    route = payload.get("route") or {}
    return {
        "id": route.get("id"),
        "train_number": route.get("train_number"),
        "route_name": route.get("route_name"),
        "snapshot_date": route.get("snapshot_date"),
    }


def get_station_candidates_block(payload: Dict[str, Any]) -> Dict[str, Any]:
    return payload.get("segment_station_candidates") or {}


def get_segment_debug_by_index(payload: Dict[str, Any], idx: int) -> Optional[Dict[str, Any]]:
    selected = payload.get("selected_segment_debug") or {}
    if selected.get("segment_index") == idx:
        return selected

    neighbors = payload.get("neighbor_segments") or {}
    for key in ("previous", "selected", "next"):
        seg = neighbors.get(key) or {}
        if seg.get("segment_index") == idx:
            return seg

    all_segments = payload.get("all_segment_debugs") or {}
    maybe = all_segments.get(str(idx))
    if isinstance(maybe, dict):
        return maybe

    return None


def get_from_station_name(seg: Dict[str, Any]) -> Optional[str]:
    if not isinstance(seg, dict):
        return None
    return (
        seg.get("from_station_name")
        or (seg.get("from_stop") or {}).get("station_name_raw")
        or (seg.get("from_stop") or {}).get("station_name")
    )


def get_to_station_name(seg: Dict[str, Any]) -> Optional[str]:
    if not isinstance(seg, dict):
        return None
    return (
        seg.get("to_station_name")
        or (seg.get("to_stop") or {}).get("station_name_raw")
        or (seg.get("to_stop") or {}).get("station_name")
    )


def get_mode_block(seg: Dict[str, Any], mode_name: str) -> Dict[str, Any]:
    modes = seg.get("search_modes") or {}
    mode = modes.get(mode_name)
    return mode if isinstance(mode, dict) else {}


def get_best_rejected(seg: Dict[str, Any], preferred_mode: str = "station_links_only") -> Optional[Dict[str, Any]]:
    mode = get_mode_block(seg, preferred_mode)
    best = mode.get("best_rejected")
    if isinstance(best, dict):
        return best

    modes = seg.get("search_modes") or {}
    for mode_name in (
        "station_links_only",
        "station_links_plus_nearby_edges_400m",
        "station_links_plus_nearby_edges_600m",
        "station_links_plus_local_rescue",
    ):
        item = modes.get(mode_name) or {}
        best = item.get("best_rejected")
        if isinstance(best, dict):
            return best

    return None


def extract_transition_rejected_reason(best_rejected: Optional[Dict[str, Any]]) -> Optional[str]:
    if not isinstance(best_rejected, dict):
        return None
    diag = best_rejected.get("transition_diag") or {}
    return diag.get("rejected_reason") or best_rejected.get("rejected_reason")


# =============================================================================
# diagnosis
# =============================================================================

def detect_anchor_problem_signature(payload: Dict[str, Any], selected_idx: int) -> Dict[str, Any]:
    selected = get_segment_debug_by_index(payload, selected_idx) or {}
    next_seg = get_segment_debug_by_index(payload, selected_idx + 1) or {}
    prev_seg = get_segment_debug_by_index(payload, selected_idx - 1) or {}

    ratio = safe_float(selected.get("ratio_graph_to_rzd"))
    chosen_search_mode = selected.get("chosen_search_mode")
    selected_best_rejected = get_best_rejected(selected)
    selected_rejected_reason = extract_transition_rejected_reason(selected_best_rejected)

    next_best_rejected = get_best_rejected(next_seg)
    next_rejected_reason = extract_transition_rejected_reason(next_best_rejected)

    prev_path_found = prev_seg.get("path_found")
    next_path_found = next_seg.get("path_found")
    next_mode = next_seg.get("chosen_search_mode")

    signature = {
        "selected_ratio_graph_to_rzd": ratio,
        "selected_rejected_reason": selected_rejected_reason,
        "selected_path_found": selected.get("path_found"),
        "selected_chosen_search_mode": chosen_search_mode,
        "next_path_found": next_path_found,
        "next_rejected_reason": next_rejected_reason,
        "next_chosen_search_mode": next_mode,
        "prev_path_found": prev_path_found,
        "looks_like_anchor_problem": False,
        "reason_flags": [],
    }

    if ratio is not None and ratio > 1.8:
        signature["reason_flags"].append("selected_ratio_too_high")
    if selected_rejected_reason == "graph_path_absurd_detour":
        signature["reason_flags"].append("selected_absurd_detour")
    if next_rejected_reason == "no_graph_path":
        signature["reason_flags"].append("next_no_graph_path")
    if next_mode == "isolated_component_bridge_last_resort":
        signature["reason_flags"].append("next_bridge_last_resort")
    if prev_path_found is True:
        signature["reason_flags"].append("prev_ok")

    if (
        ("selected_absurd_detour" in signature["reason_flags"] or "selected_ratio_too_high" in signature["reason_flags"])
        and ("next_no_graph_path" in signature["reason_flags"] or "next_bridge_last_resort" in signature["reason_flags"])
        and ("prev_ok" in signature["reason_flags"])
    ):
        signature["looks_like_anchor_problem"] = True

    return signature


def infer_middle_station(payload: Dict[str, Any], selected_idx: int) -> Dict[str, Any]:
    selected = get_segment_debug_by_index(payload, selected_idx) or {}
    next_seg = get_segment_debug_by_index(payload, selected_idx + 1) or {}

    station_candidates = get_station_candidates_block(payload)
    to_stop = station_candidates.get("to_stop") or {}
    next_from_name = get_from_station_name(next_seg)

    station_name = (
        to_stop.get("locked_station_name")
        or to_stop.get("station_name_raw")
        or get_to_station_name(selected)
        or next_from_name
    )
    station_id = to_stop.get("locked_station_id") or to_stop.get("station_id")

    return {
        "station_id": station_id,
        "station_name": station_name,
        "from_station_selected_segment": get_from_station_name(selected),
        "to_station_selected_segment": get_to_station_name(selected),
        "from_station_next_segment": next_from_name,
        "to_station_next_segment": get_to_station_name(next_seg),
    }


# =============================================================================
# candidate collection
# =============================================================================

def collect_middle_station_candidate_seeds(payload: Dict[str, Any]) -> List[AnchorSeed]:
    """
    Пока что seeds берем из segment_station_candidates.to_stop.candidates.
    Это не реальные anchors graph-а, а station-level кандидаты.
    Для текстового теста этого достаточно.

    Следующий шаг интеграции:
    вместо этого брать реальные topology anchors / station links / nearby edges.
    """
    station_candidates = get_station_candidates_block(payload)
    to_stop = station_candidates.get("to_stop") or {}
    raw_candidates = to_stop.get("candidates") or []

    seeds: List[AnchorSeed] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue

        seeds.append(
            AnchorSeed(
                station_id=item.get("station_id"),
                station_name=item.get("station_name"),
                source=item.get("match_method") or "station_candidate",
                node_hash=None,
                component_id=None,
                entry_km=None,
                region_code=item.get("region_code"),
                score_hint=safe_float(item.get("effective_score")),
                raw=item,
            )
        )

    locked_id = to_stop.get("locked_station_id")
    locked_name = to_stop.get("locked_station_name")
    if locked_id is not None and not any(s.station_id == locked_id for s in seeds):
        seeds.insert(
            0,
            AnchorSeed(
                station_id=locked_id,
                station_name=locked_name,
                source="locked_station",
                score_hint=1.0,
                raw=to_stop,
            ),
        )

    return seeds


# =============================================================================
# executable callbacks hook
# =============================================================================

class ProbeExecutor:
    def available(self) -> bool:
        return False

    def evaluate_candidate(
        self,
        payload: Dict[str, Any],
        selected_segment_index: int,
        candidate: AnchorSeed,
    ) -> CandidateProbeResult:
        raise NotImplementedError


class HeuristicOnlyExecutor(ProbeExecutor):
    """
    Текстовый / эвристический режим.
    Мы не гоняем граф, а строим план действий и примерный priority score.
    """

    def available(self) -> bool:
        return True

    def evaluate_candidate(
        self,
        payload: Dict[str, Any],
        selected_segment_index: int,
        candidate: AnchorSeed,
    ) -> CandidateProbeResult:
        selected = get_segment_debug_by_index(payload, selected_segment_index) or {}
        next_seg = get_segment_debug_by_index(payload, selected_segment_index + 1) or {}

        incoming_eval = SegmentEval(
            path_found=selected.get("path_found"),
            search_mode=selected.get("chosen_search_mode"),
            ratio_graph_to_rzd=safe_float(selected.get("ratio_graph_to_rzd")),
            rejected_reason=extract_transition_rejected_reason(get_best_rejected(selected)),
            raw=selected,
        )
        outgoing_eval = SegmentEval(
            path_found=next_seg.get("path_found"),
            search_mode=next_seg.get("chosen_search_mode"),
            ratio_graph_to_rzd=safe_float(next_seg.get("ratio_graph_to_rzd")),
            rejected_reason=extract_transition_rejected_reason(get_best_rejected(next_seg)),
            raw=next_seg,
        )

        score = 0.0
        notes: List[str] = []

        if candidate.source in ("existing_visible_station_id", "locked_station"):
            score += 100.0
            notes.append("baseline_locked_station_candidate")
        else:
            score += 30.0
            notes.append("alternative_station_level_candidate")

        if candidate.score_hint is not None:
            score += (1.0 - max(0.0, min(1.0, candidate.score_hint))) * 50.0

        if incoming_eval.rejected_reason == "graph_path_absurd_detour":
            score += 400.0
        if outgoing_eval.rejected_reason == "no_graph_path":
            score += 700.0
        if outgoing_eval.search_mode == "isolated_component_bridge_last_resort":
            score += 600.0

        if candidate.score_hint is not None and candidate.score_hint >= 0.8:
            score -= 20.0
            notes.append("high_station_match_score")

        accepted = False
        if (
            candidate.source not in ("existing_visible_station_id", "locked_station")
            and candidate.score_hint is not None
            and candidate.score_hint >= 0.9
        ):
            notes.append("candidate_is_good_enough_for_manual_real_probe")

        return CandidateProbeResult(
            candidate_anchor=candidate,
            incoming_eval=incoming_eval,
            outgoing_eval=outgoing_eval,
            score=round(score, 4),
            accepted=accepted,
            notes=notes,
        )


# =============================================================================
# result building
# =============================================================================

def build_probe_report(
    payload: Dict[str, Any],
    selected_segment_index: int,
    signature: Dict[str, Any],
    middle_station: Dict[str, Any],
    results: List[CandidateProbeResult],
    mode: str,
) -> Dict[str, Any]:
    sorted_results = sorted(results, key=lambda x: x.score)
    best = sorted_results[0] if sorted_results else None

    report = {
        "probe_type": "middle_station_anchor_repair",
        "mode": mode,
        "selected_segment_index": selected_segment_index,
        "signature": signature,
        "middle_station": middle_station,
        "tested_candidates_count": len(results),
        "accepted_candidate_found": any(r.accepted for r in results),
        "best_candidate": (
            {
                "station_id": best.candidate_anchor.station_id,
                "station_name": best.candidate_anchor.station_name,
                "source": best.candidate_anchor.source,
                "score_hint": best.candidate_anchor.score_hint,
                "score": best.score,
                "accepted": best.accepted,
                "notes": best.notes,
            }
            if best
            else None
        ),
        "recommended_action": None,
        "top_candidates": [
            {
                "rank": idx + 1,
                "candidate_anchor": asdict(item.candidate_anchor),
                "score": item.score,
                "accepted": item.accepted,
                "notes": item.notes,
            }
            for idx, item in enumerate(sorted_results[:10])
        ],
    }

    if not signature.get("looks_like_anchor_problem"):
        report["recommended_action"] = "not_anchor_signature__do_not_run_auto_repair"
    elif mode == "heuristic-only":
        report["recommended_action"] = "signature_detected__need_real_graph_probe_with_middle_station_alternative_anchors"
    elif any(r.accepted for r in results):
        report["recommended_action"] = "override_middle_station_anchor"
    else:
        report["recommended_action"] = "anchor_probe_failed__escalate_to_graph_repair"

    return report


# =============================================================================
# CLI printing
# =============================================================================

def print_candidate_table(results: List[CandidateProbeResult]) -> None:
    if not results:
        print("Кандидатов нет.")
        return

    for idx, item in enumerate(sorted(results, key=lambda x: x.score), start=1):
        print(
            f"{idx:>2}. station_id={item.candidate_anchor.station_id} | "
            f"station_name={item.candidate_anchor.station_name} | "
            f"source={item.candidate_anchor.source} | "
            f"score_hint={item.candidate_anchor.score_hint} | "
            f"score={item.score} | "
            f"accepted={item.accepted}"
        )
        if item.notes:
            print(f"    notes={item.notes}")


def print_execution_plan(middle_station: Dict[str, Any], results: List[CandidateProbeResult]) -> None:
    print("Что надо прогонять на реальном matcher:")
    print(
        f"1. Взять промежуточную станцию: "
        f"{middle_station.get('station_name')} (station_id={middle_station.get('station_id')})"
    )
    print("2. Собрать реальные anchors этой станции:")
    print("   - station_link anchors")
    print("   - nearby edge anchors 200-400 м")
    print("   - nearby edge anchors 400-600 м")
    print("   - local rescue anchors")
    print("3. Для каждого anchor прогнать 2 проверки:")
    print("   - incoming: prev -> middle_candidate")
    print("   - outgoing: middle_candidate -> next")
    print("4. Посчитать score и выбрать лучший.")
    print("5. Принимать override только если оба сегмента чинятся без absurd_detour и без bridge fallback.")
    print()
    print("Приоритет station-level кандидатов для ручной проверки:")
    print_candidate_table(results)


# =============================================================================
# main
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Тестовый probe-скрипт для логики repair middle-anchor."
    )
    parser.add_argument(
        "--from-json",
        type=str,
        default=None,
        help="Путь к json текущего дебагера.",
    )
    parser.add_argument(
        "--route-id",
        type=int,
        default=None,
        help="Route id для автопоиска последнего json.",
    )
    parser.add_argument(
        "--segment",
        type=int,
        default=None,
        help="Segment index.",
    )
    parser.add_argument(
        "--debug-dir",
        type=str,
        default="debug_output",
        help="Папка с json дебагера.",
    )
    parser.add_argument(
        "--save-report",
        action="store_true",
        help="Сохранить отдельный json-отчет probe-а.",
    )
    return parser.parse_args()


def resolve_input_json(args: argparse.Namespace) -> Path:
    if args.from_json:
        path = Path(args.from_json)
        if not path.exists():
            raise FileNotFoundError(f"Файл не найден: {path}")
        return path

    latest = get_latest_matching_json(Path(args.debug_dir), args.route_id, args.segment)
    if latest is None:
        raise FileNotFoundError(
            "Не нашел json дебагера. Передай --from-json или положи файл в debug_output."
        )
    return latest


def main() -> None:
    args = parse_args()
    input_json = resolve_input_json(args)
    payload = load_json(input_json)

    selected_segment_index = get_selected_segment_index(payload, args.segment)
    if selected_segment_index is None:
        raise RuntimeError("Не удалось определить selected segment index. Передай --segment явно.")

    route_brief = get_route_brief(payload)
    signature = detect_anchor_problem_signature(payload, selected_segment_index)
    middle_station = infer_middle_station(payload, selected_segment_index)
    seeds = collect_middle_station_candidate_seeds(payload)

    executor: ProbeExecutor = HeuristicOnlyExecutor()
    results = [executor.evaluate_candidate(payload, selected_segment_index, seed) for seed in seeds]

    report = build_probe_report(
        payload=payload,
        selected_segment_index=selected_segment_index,
        signature=signature,
        middle_station=middle_station,
        results=results,
        mode="heuristic-only",
    )

    print_section("START")
    print(f"source_json = {input_json}")
    print(f"route_id = {route_brief.get('id')}")
    print(f"segment = {selected_segment_index}")

    print_section("ROUTE")
    print_json(route_brief)

    print_section("ANCHOR PROBLEM SIGNATURE")
    print_json(signature)

    print_section("MIDDLE STATION")
    print_json(middle_station)

    print_section("CANDIDATE SEEDS")
    print_json([asdict(x) for x in seeds])

    print_section("HEURISTIC PROBE RESULTS")
    print_candidate_table(results)

    print_section("EXECUTION PLAN")
    print_execution_plan(middle_station, results)

    print_section("PROBE REPORT")
    print_json(report)

    if args.save_report:
        out_dir = Path(args.debug_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        route_id = route_brief.get("id") or "unknown"
        out_path = out_dir / f"anchor_repair_probe_route_{route_id}_segment_{selected_segment_index}.json"
        dump_json(out_path, report)
        print_section("FILE SAVED")
        print(out_path)

    print()
    print("Статус:")
    print("- Сейчас это ТЕСТОВЫЙ probe-скрипт.")
    print("- Он уже помогает диагностировать, когда есть именно anchor signature.")
    print("- Чтобы реально разрешать такие кейсы, нужно подключить реальные graph callbacks.")
    print("- После этого этот же файл можно дорастить до executable repair probe.")


if __name__ == "__main__":
    main()
