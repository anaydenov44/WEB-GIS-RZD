from __future__ import annotations

import argparse
import copy
import datetime as dt
import glob
import importlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


# ------------------------------------------------------------------------------
# import helpers
# ------------------------------------------------------------------------------

def import_route_matcher_module():
    candidates = [
        "app.route_graph_matcher",
        "app.services.route_graph_matcher",
        "app.services.route_matcher",
        "app.route_matcher",
        "app.matchers.route_matcher",
        "app.core.route_matcher",
        "route_graph_matcher",
        "route_matcher",
    ]
    errors: List[str] = []

    for name in candidates:
        try:
            mod = importlib.import_module(name)
            this_file = Path(__file__).resolve()
            mod_file = Path(getattr(mod, "__file__", "")).resolve() if getattr(mod, "__file__", None) else None
            if mod_file and mod_file == this_file:
                errors.append(f"- {name}: imported self")
                continue
            return mod
        except Exception as exc:
            errors.append(f"- {name}: {exc}")

    lines = ["Не удалось импортировать route_matcher. Проверенные варианты:"] + errors
    raise ImportError("\n".join(lines))


def first_dict_value(d: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        if key in d:
            return d[key]
    return default


# ------------------------------------------------------------------------------
# console compacting
# ------------------------------------------------------------------------------

def compact_geometry(geometry: Any) -> Any:
    if not isinstance(geometry, dict):
        return geometry
    out = dict(geometry)
    coords = out.get("coordinates")
    if isinstance(coords, list):
        count = len(coords)
        head = coords[:3]
        tail = coords[-2:] if count > 5 else []
        out["coordinates"] = {
            "points_count": count,
            "head": head,
            "tail": tail,
            "omitted": max(0, count - len(head) - len(tail)),
        }
    return out


def compact_for_console(value: Any, *, max_list_items: int = 8) -> Any:
    if isinstance(value, float):
        return round(value, 4)

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            if key == "geometry":
                out[key] = compact_geometry(item)
                continue
            out[key] = compact_for_console(item, max_list_items=max_list_items)
        return out

    if isinstance(value, list):
        if len(value) <= max_list_items:
            return [compact_for_console(v, max_list_items=max_list_items) for v in value]
        head = [compact_for_console(v, max_list_items=max_list_items) for v in value[:4]]
        tail = [compact_for_console(v, max_list_items=max_list_items) for v in value[-2:]]
        return head + [{"...": f"omitted {len(value) - 6} items"}] + tail

    return value


def dumps_console(value: Any) -> str:
    return json.dumps(compact_for_console(value), ensure_ascii=False, indent=2)


def print_header(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


# ------------------------------------------------------------------------------
# payload IO
# ------------------------------------------------------------------------------

def save_payload(payload: Dict[str, Any], route_id: Optional[int], segment_index: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    route_part = str(route_id) if route_id is not None else "from_json"
    path = out_dir / f"route_root_cause_{route_part}_segment_{segment_index}_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_payload_from_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_latest_saved_payload(route_id: int, segment_index: int, out_dir: Path) -> Optional[Path]:
    pattern = str(out_dir / f"route_root_cause_{route_id}_segment_{segment_index}_*.json")
    files = [Path(p) for p in glob.glob(pattern)]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


# ------------------------------------------------------------------------------
# direct build hooks
# ------------------------------------------------------------------------------

def try_build_payload_direct(rm: Any, route_id: int, segment_index: int) -> Optional[Dict[str, Any]]:
    ready_api_names = [
        "debug_route_root_cause",
        "build_route_root_cause_debug_payload",
        "build_route_debug_payload",
        "collect_route_root_cause_payload",
    ]

    for name in ready_api_names:
        fn = getattr(rm, name, None)
        if callable(fn):
            try:
                payload = fn(route_id, segment_index)
                if isinstance(payload, dict):
                    return payload
            except TypeError:
                try:
                    payload = fn(route_id=route_id, segment_index=segment_index)
                    if isinstance(payload, dict):
                        return payload
                except Exception:
                    pass
            except Exception:
                pass

    return None


# ------------------------------------------------------------------------------
# payload navigation
# ------------------------------------------------------------------------------

def get_segment_debugs(payload: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    raw = first_dict_value(
        payload,
        [
            "all_segment_debugs",
            "segment_debugs",
            "segments_debug",
            "segments",
        ],
        default={},
    )

    if isinstance(raw, dict):
        out: Dict[int, Dict[str, Any]] = {}
        for key, value in raw.items():
            idx: Optional[int] = None
            try:
                idx = int(key)
            except Exception:
                if isinstance(value, dict) and value.get("segment_index") is not None:
                    idx = int(value.get("segment_index"))
            if idx is not None and isinstance(value, dict):
                out[idx] = value
        return out

    if isinstance(raw, list):
        out = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            idx = item.get("segment_index")
            if idx is not None:
                out[int(idx)] = item
        return out

    return {}


def get_segment(seg_debugs: Dict[int, Dict[str, Any]], idx: int) -> Optional[Dict[str, Any]]:
    return seg_debugs.get(idx)


def get_stop_name(seg: Optional[Dict[str, Any]], side: str) -> Optional[str]:
    if not isinstance(seg, dict):
        return None
    side_obj = seg.get(f"{side}_stop")
    if isinstance(side_obj, dict):
        return first_dict_value(
            side_obj,
            ["station_name_raw", "station_name", "name", "title"],
            default=None,
        )
    return first_dict_value(seg, [f"{side}_station_name", f"{side}_stop_name"], default=None)


def get_station_candidates(payload: Dict[str, Any]) -> Dict[str, Any]:
    return first_dict_value(
        payload,
        [
            "segment_station_candidates",
            "station_candidates",
        ],
        default={},
    ) or {}


def get_station_complexity(payload: Dict[str, Any]) -> Dict[str, Any]:
    return first_dict_value(
        payload,
        [
            "station_link_complexity_inference",
            "station_complexity_inference",
        ],
        default={},
    ) or {}


def get_root_hypotheses(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = first_dict_value(payload, ["root_cause_hypotheses", "hypotheses"], default=[])
    return raw if isinstance(raw, list) else []


def get_mode_order(seg: Dict[str, Any]) -> List[str]:
    preferred = [
        "station_links_only",
        "station_links_plus_nearby_edges_400m",
        "station_links_plus_nearby_edges_600m",
        "station_links_plus_local_rescue",
    ]
    return [m for m in preferred if m in seg]


def get_best_rejected(seg: Dict[str, Any], mode_name: str) -> Optional[Dict[str, Any]]:
    mode = seg.get(mode_name)
    if isinstance(mode, dict):
        br = mode.get("best_rejected")
        return br if isinstance(br, dict) else None
    return None


def get_ratio(seg: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(seg, dict):
        return None
    value = seg.get("ratio_graph_to_rzd")
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


# ------------------------------------------------------------------------------
# hypotheses / summary
# ------------------------------------------------------------------------------

def build_anchor_probe_summary(payload: Dict[str, Any], segment_index: int) -> Dict[str, Any]:
    seg_debugs = get_segment_debugs(payload)
    selected = get_segment(seg_debugs, segment_index)
    nxt = get_segment(seg_debugs, segment_index + 1)
    complexity = get_station_complexity(payload)

    selected_ratio = get_ratio(selected)
    from_link_est = complexity.get("from_station_link_count_estimate")
    to_link_est = complexity.get("to_station_link_count_estimate")

    suspected_station = (
        get_stop_name(selected, "to")
        or get_stop_name(nxt, "from")
        or get_stop_name(selected, "from")
    )

    next_normal_path_found = bool(nxt and nxt.get("path_found"))
    next_mode = nxt.get("chosen_search_mode") if isinstance(nxt, dict) else None
    next_is_bridge_only = next_mode == "isolated_component_bridge_last_resort"

    same_component_huge_detour = bool(selected_ratio is not None and selected_ratio > 1.8)
    next_component_break = False
    if isinstance(nxt, dict):
        br = get_best_rejected(nxt, "station_links_only")
        if isinstance(br, dict) and br.get("rejected_reason") == "no_graph_path":
            next_component_break = True

    is_primary_vokzal_problem = bool(
        (from_link_est or 0) >= 6 and
        not next_component_break and
        not next_is_bridge_only
    )

    verdict = "unclear"
    if same_component_huge_detour and (next_component_break or next_is_bridge_only):
        verdict = "primary_anchor_or_component_problem"
    elif same_component_huge_detour and (from_link_est or 0) >= 6:
        verdict = "anchor_problem_with_big_station_amplifier"
    elif same_component_huge_detour:
        verdict = "same_component_detour_problem"

    actions = [
        "Проверить topology attachment у промежуточной станции.",
        "Попробовать альтернативные anchor/component для конечной станции выбранного сегмента.",
        "Сравнивать входящий и исходящий сегменты совместно, а не по одному.",
        "Не рисовать absurd_detour путь как fallback.",
    ]

    return {
        "verdict": verdict,
        "suspected_station": suspected_station,
        "is_primary_vokzal_problem": is_primary_vokzal_problem,
        "selected_segment_ratio_graph_to_rzd": selected_ratio,
        "selected_from_station_link_estimate": from_link_est,
        "selected_to_station_link_estimate": to_link_est,
        "next_segment_path_found": next_normal_path_found,
        "next_segment_chosen_search_mode": next_mode,
        "recommended_next_actions": actions,
    }


# ------------------------------------------------------------------------------
# heuristic repair probe
# ------------------------------------------------------------------------------

def _seed_candidate(node_hash: Optional[str], source: Optional[str], mode: str, side: str) -> Optional[Dict[str, Any]]:
    if not node_hash:
        return None
    return {
        "node_hash": node_hash,
        "sources": [source] if source else [],
        "seen_in": [f"{side}:{mode}"],
        "is_current_locked_station_link": bool(source == "station_link"),
    }


def collect_mid_station_candidate_seeds(payload: Dict[str, Any], segment_index: int) -> List[Dict[str, Any]]:
    seg_debugs = get_segment_debugs(payload)
    selected = get_segment(seg_debugs, segment_index)
    nxt = get_segment(seg_debugs, segment_index + 1)

    seeds: Dict[str, Dict[str, Any]] = {}

    def upsert(seed: Optional[Dict[str, Any]]) -> None:
        if not seed:
            return
        key = seed["node_hash"]
        current = seeds.get(key)
        if current is None:
            seeds[key] = seed
            return
        for src in seed["sources"]:
            if src not in current["sources"]:
                current["sources"].append(src)
        for item in seed["seen_in"]:
            if item not in current["seen_in"]:
                current["seen_in"].append(item)
        current["is_current_locked_station_link"] = current["is_current_locked_station_link"] or seed["is_current_locked_station_link"]

    for mode in get_mode_order(selected or {}):
        br = get_best_rejected(selected or {}, mode)
        if not br:
            continue
        upsert(_seed_candidate(br.get("to_node_hash"), br.get("to_source"), mode, "incoming_to_mid"))

    for mode in get_mode_order(nxt or {}):
        br = get_best_rejected(nxt or {}, mode)
        if not br:
            continue
        upsert(_seed_candidate(br.get("from_node_hash"), br.get("from_source"), mode, "outgoing_from_mid"))

    return list(seeds.values())


def rank_mid_station_candidates_heuristic(payload: Dict[str, Any], segment_index: int) -> List[Dict[str, Any]]:
    seg_debugs = get_segment_debugs(payload)
    selected = get_segment(seg_debugs, segment_index)
    nxt = get_segment(seg_debugs, segment_index + 1)
    selected_ratio = get_ratio(selected)
    next_station_links_only_br = get_best_rejected(nxt or {}, "station_links_only")
    next_has_component_break = bool(
        next_station_links_only_br and next_station_links_only_br.get("rejected_reason") == "no_graph_path"
    )

    ranked: List[Dict[str, Any]] = []

    for seed in collect_mid_station_candidate_seeds(payload, segment_index):
        score = 0.0
        reasons: List[str] = []

        if seed["is_current_locked_station_link"]:
            score -= 80.0
            reasons.append("Это текущий locked station_link, он уже участвует в плохом кейсе.")
        else:
            score += 20.0
            reasons.append("Это альтернативный локальный anchor-кандидат, а не текущий locked station_link.")

        if any(src and str(src).startswith("edge_") for src in seed["sources"]):
            score += 15.0
            reasons.append("Кандидат приходит из nearby edge search, это хороший сигнал для anchor-repair probe.")

        if selected_ratio is not None:
            score -= min(90.0, abs(selected_ratio - 1.0) * 25.0)

        if next_has_component_break and seed["is_current_locked_station_link"]:
            score -= 90.0
            reasons.append("На следующем сегменте именно этот anchor выглядит как источник component break.")

        ranked.append(
            {
                "node_hash": seed["node_hash"],
                "sources": seed["sources"],
                "seen_in": seed["seen_in"],
                "heuristic_score": round(score, 3),
                "reasons": reasons,
            }
        )

    ranked.sort(key=lambda x: x["heuristic_score"], reverse=True)
    return ranked


def build_mid_station_anchor_repair_probe(payload: Dict[str, Any], segment_index: int) -> Dict[str, Any]:
    summary = build_anchor_probe_summary(payload, segment_index)
    ranked = rank_mid_station_candidates_heuristic(payload, segment_index)

    if not ranked:
        return {
            "probe_type": "heuristic_only",
            "suspected_station": summary.get("suspected_station"),
            "status": "no_candidate_seeds",
            "message": "Не удалось собрать seed-кандидаты из debug output.",
        }

    return {
        "probe_type": "heuristic_only",
        "suspected_station": summary.get("suspected_station"),
        "status": "candidate_seeds_ranked",
        "warning": (
            "Это пока не forced-reroute repair. "
            "Здесь идет эвристический рейтинг anchor seed-кандидатов из уже собранного debug output."
        ),
        "top_candidates": ranked[:10],
        "recommended_strategy": [
            "Взять top-1 / top-2 node_hash из этого списка.",
            "Прогнать оба сегмента через forced anchor на промежуточной станции.",
            "Выбрать anchor, у которого входящий сегмент перестает быть absurd_detour, а исходящий строится без isolated_component_bridge_last_resort.",
        ],
    }


# ------------------------------------------------------------------------------
# console rendering
# ------------------------------------------------------------------------------

def print_segment_block(seg: Optional[Dict[str, Any]], idx: int) -> None:
    print(
        f"[segment {idx}] "
        f"{get_stop_name(seg, 'from') or '?'} -> {get_stop_name(seg, 'to') or '?'} | "
        f"path_found={seg.get('path_found') if seg else None} | "
        f"chosen_search_mode={seg.get('chosen_search_mode') if seg else None} | "
        f"delta_rzd_km={seg.get('delta_rzd_km') if seg else None} | "
        f"ratio_graph_to_rzd={seg.get('ratio_graph_to_rzd') if seg else None}"
    )

    if not isinstance(seg, dict):
        return

    for mode in get_mode_order(seg):
        mode_obj = seg.get(mode)
        if not isinstance(mode_obj, dict):
            continue
        print(
            f"  {mode}: pairs_checked={mode_obj.get('pairs_checked')} | "
            f"successful_pairs_count={mode_obj.get('successful_pairs_count')} | "
            f"rejected_reason_counts={mode_obj.get('rejected_reason_counts')}"
        )
        best_rejected = mode_obj.get("best_rejected")
        if best_rejected:
            print(f"    best_rejected={dumps_console(best_rejected)}")


def print_hypotheses(hypotheses: List[Dict[str, Any]]) -> None:
    if not hypotheses:
        print("Нет hypotheses.")
        return
    for i, hyp in enumerate(hypotheses, start=1):
        print(f"{i}. [{hyp.get('severity')}] {hyp.get('code')}")
        print(f"   {hyp.get('message')}")
        details = hyp.get("details")
        if details is not None:
            print(f"   details={dumps_console(details)}")


# ------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("route_id", nargs="?", type=int)
    parser.add_argument("--segment", required=True, type=int)
    parser.add_argument("--from-json", dest="from_json", type=str, default=None)
    parser.add_argument("--out-dir", dest="out_dir", type=str, default="debug_output")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    route_id = args.route_id
    segment_index = args.segment
    out_dir = Path(args.out_dir)

    payload: Dict[str, Any]
    source_label: str

    if args.from_json:
        json_path = Path(args.from_json)
        payload = load_payload_from_json(json_path)
        source_label = f"json:{json_path}"
    else:
        direct_payload: Optional[Dict[str, Any]] = None

        try:
            rm = import_route_matcher_module()
            print("route_matcher module:", rm.__name__, getattr(rm, "__file__", None))
            if route_id is not None:
                direct_payload = try_build_payload_direct(rm, route_id, segment_index)
        except Exception as exc:
            print(f"route_matcher import warning: {exc}")

        if direct_payload is not None:
            payload = direct_payload
            source_label = "direct_builder"
        else:
            if route_id is None:
                raise SystemExit(
                    "Нужен route_id, если не используется --from-json и нет direct builder."
                )

            latest_json = find_latest_saved_payload(route_id, segment_index, out_dir)
            if latest_json is None:
                raise SystemExit(
                    "Не найден direct builder и нет сохраненного json для этого route/segment.\n"
                    "Сначала запусти свой основной рабочий дебагер, чтобы он сохранил json в debug_output,\n"
                    "или передай файл явно через --from-json."
                )

            payload = load_payload_from_json(latest_json)
            source_label = f"latest_saved_json:{latest_json}"

    payload = copy.deepcopy(payload)
    payload["anchor_probe_summary"] = build_anchor_probe_summary(payload, segment_index)
    payload["mid_station_anchor_repair_probe"] = build_mid_station_anchor_repair_probe(payload, segment_index)

    seg_debugs = get_segment_debugs(payload)
    selected = get_segment(seg_debugs, segment_index)
    previous = get_segment(seg_debugs, segment_index - 1)
    nxt = get_segment(seg_debugs, segment_index + 1)

    print_header("START")
    print(f"source = {source_label}")
    print(f"route_id = {route_id}")
    print(f"segment = {segment_index}")

    for block_name in ["route", "inferred_regions", "network"]:
        block = payload.get(block_name)
        if block is not None:
            print_header(block_name.upper())
            print(dumps_console(block))

    print_header("SELECTED SEGMENT DEBUG")
    print_segment_block(selected, segment_index)

    print_header("NEIGHBOR SEGMENTS")
    print_segment_block(previous, segment_index - 1)
    print_segment_block(selected, segment_index)
    print_segment_block(nxt, segment_index + 1)

    complexity = get_station_complexity(payload)
    if complexity:
        print_header("STATION LINK COMPLEXITY INFERENCE")
        print(dumps_console(complexity))

    station_candidates = get_station_candidates(payload)
    if station_candidates:
        print_header("SEGMENT STATION CANDIDATES")
        print(dumps_console(station_candidates))

    print_header("ROOT CAUSE HYPOTHESES")
    print_hypotheses(get_root_hypotheses(payload))

    print_header("ANCHOR PROBE SUMMARY")
    print(dumps_console(payload["anchor_probe_summary"]))

    print_header("MID-STATION ANCHOR REPAIR PROBE")
    print(dumps_console(payload["mid_station_anchor_repair_probe"]))

    saved_path = save_payload(payload, route_id, segment_index, out_dir)
    print_header("FILE SAVED")
    print(str(saved_path))


if __name__ == "__main__":
    main()