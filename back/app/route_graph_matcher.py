import json
import logging
import os
import math
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from heapq import heappop, heappush
from typing import Any, Callable

from sqlalchemy import text

from app.db import engine
from app.matcher_logging import (
    StageTimer,
    append_error,
    build_exception_payload,
    log_event,
)

ProgressCallback = Callable[[int, str, dict[str, Any] | None], None]

GRAPH_CACHE_TTL_SECONDS = 600
MAX_CANDIDATES_PER_STOP = 8
MAX_ROUTE_REGION_CODES = 6

MAX_TOPOLOGY_LINK_OPTIONS_PER_STATION = 6
TOPOLOGY_FALLBACK_NODE_NEIGHBORS = 6
TOPOLOGY_FALLBACK_NODE_MAX_DISTANCE_KM = 3.0

TOPOLOGY_AUGMENT_NODE_NEIGHBORS = 12
TOPOLOGY_AUGMENT_NODE_MAX_DISTANCE_KM = 8.0
TOPOLOGY_LINK_OPTIONS_FINAL_LIMIT = 18

LOCAL_RESCUE_NODE_RADIUS_KM = 2.0
LOCAL_RESCUE_NODE_LIMIT = 10
LOCAL_RESCUE_EXTRA_PENALTY = 1.25

NEARBY_EDGE_ATTACH_RADII_METERS = (400, 600)
NEARBY_EDGE_ATTACH_LIMIT_PER_RADIUS = 10
NEARBY_EDGE_ATTACH_MAX_ENTRY_KM = 2.5

LOCKED_STORED_NAME_SCORE_MIN = 0.72
LOCKED_STORED_NAME_SCORE_STRICT = 0.82
LOCKED_EXACT_CODE_PRIORITY_DELTA = 0.08

ROUTE_LOCK_BIG_DISTANCE_REJECTION_KM = 150.0

COMPONENT_BRIDGE_SMALL_COMPONENT_MAX_SIZE = 500
COMPONENT_BRIDGE_MAX_GAP_KM = 2.0
COMPONENT_BRIDGE_PAIR_LIMIT = 8
COMPONENT_BRIDGE_EXTRA_PENALTY = 0.20
COMPONENT_BRIDGE_SOURCE_PENALTY = 0.05
COMPONENT_BRIDGE_GAP_SCORE_WEIGHT = 0.35

TOPOLOGY_LINK_CONNECTOR_SCORE_WEIGHT = 0.15
TOPOLOGY_FALLBACK_LINK_SCORE_PENALTY = 0.05
TOPOLOGY_NON_PRIMARY_LINK_SCORE_PENALTY = 0.01
TRANSITION_DISTANCE_SCORE_WEIGHT = 4.0

ABSURD_PATH_MIN_GEO_KM = 5.0
ABSURD_PATH_MAX_GEO_RATIO = 2.2
ABSURD_PATH_MAX_GEO_EXTRA_KM = 35.0
ABSURD_PATH_MAX_RZD_RATIO = 2.0
ABSURD_PATH_MAX_RZD_EXTRA_KM = 40.0

STATION_TRANSFER_MAX_LINK_KM = 1.5
STATION_TRANSFER_MAX_LINKS_PER_STATION = 8
STATION_TRANSFER_MAX_PAIR_KM = 3.0
STATION_TRANSFER_EDGE_SOURCE = "runtime_station_transfer_connector"

NORMALIZATION_REPLACEMENTS = {
    "ПАСС": "ПАССАЖИРСКИЙ",
    "ПАСС.": "ПАССАЖИРСКИЙ",
    "ГЛ": "ГЛАВНЫЙ",
    "ГЛ.": "ГЛАВНЫЙ",
    "СОРТ": "СОРТИРОВОЧНЫЙ",
    "СОРТ.": "СОРТИРОВОЧНЫЙ",
    "ЮЖ": "ЮЖНЫЙ",
    "ЮЖ.": "ЮЖНЫЙ",
    "СЕВ": "СЕВЕРНЫЙ",
    "СЕВ.": "СЕВЕРНЫЙ",
    "ЗАП": "ЗАПАДНЫЙ",
    "ЗАП.": "ЗАПАДНЫЙ",
    "ВОСТ": "ВОСТОЧНЫЙ",
    "ВОСТ.": "ВОСТОЧНЫЙ",
}

DROP_NAME_TOKENS = {
    "СТ",
    "СТАНЦИЯ",
    "ОП",
    "ОСТАНОВОЧНЫЙ",
    "ПУНКТ",
    "ПЛАТФОРМА",
    "РАЗЪЕЗД",
}

_GRAPH_CACHE_LOCK = threading.Lock()
_GRAPH_CACHE: dict[str, Any] = {
    "cache_by_region_key": {},
}


ANCHOR_REPAIR_MAX_CANDIDATES_PER_STOP = 6
ANCHOR_REPAIR_SCORE_FAILURE = 1_000_000.0
ANCHOR_REPAIR_ACCEPT_DELTA = 5.0
ANCHOR_NODE_REPAIR_MAX_OPTIONS = 24
ANCHOR_NODE_REPAIR_ACCEPT_DELTA = 3.0
ANCHOR_NODE_REPAIR_FAIL_SCORE = 1_000_000.0

SYNTHETIC_GAP_MAX_KM = 1.2
SYNTHETIC_GAP_PAIR_LIMIT = 16
SYNTHETIC_GAP_EXTRA_PENALTY = 1.5


TRACE_LOGGER = logging.getLogger("route_matcher_trace")


def matcher_trace_enabled(route_id: int | str | None = None) -> bool:
    value = os.getenv("MATCHER_TRACE_ROUTE_ID", "").strip()

    if not value:
        return False

    if value in {"1", "true", "TRUE", "all", "ALL", "*"}:
        return True

    if route_id is None:
        return False

    return str(route_id) == value


def matcher_trace(
    label: str,
    payload: dict[str, Any],
    *,
    route_id: int | str | None = None,
) -> None:
    if not matcher_trace_enabled(route_id):
        return

    try:
        message = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except Exception:
        message = str(payload)

    TRACE_LOGGER.warning("[MATCHER TRACE] %s\n%s", label, message)



def _link_source_rank(source: str | None) -> int:
    source = str(source or "")
    ranks = {
        "station_link": 0,
        "fallback_nearest_node": 1,
    }
    return ranks.get(source, 9)


def _link_priority(item: dict[str, Any]) -> tuple[int, int, float, str]:
    return (
        _link_source_rank(item.get("source")),
        0 if item.get("is_primary") else 1,
        float(item.get("link_distance_km") or 999999.0),
        str(item.get("node_hash") or ""),
    )


@dataclass
class Candidate:
    station_id: int
    region_code: str | None
    name: str
    lon: float
    lat: float
    effective_score: float
    name_score: float
    code_match: bool
    anchor: bool
    is_main_rail_station: bool
    match_method: str
    match_reason: str
    code_value: str | None = None

    @property
    def node_cost(self) -> float:
        return max(0.0, (1.0 - self.effective_score) * 8.0)


def emit_progress(
    callback: ProgressCallback | None,
    percent: int,
    stage_code: str,
    detail: dict[str, Any] | None = None,
) -> None:
    if callback is None:
        return
    callback(percent, stage_code, detail)


def append_error_once(
    diagnostics: dict[str, Any] | None,
    *,
    stage: str,
    exc: BaseException,
    extra: dict[str, Any] | None = None,
) -> None:
    if diagnostics is None:
        return

    signature = f"{stage}|{exc.__class__.__name__}|{str(exc)}"
    seen = diagnostics.setdefault("_error_signatures", [])
    if signature in seen:
        return

    seen.append(signature)
    append_error(diagnostics, stage=stage, exc=exc, extra=extra)


def cleanup_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    diagnostics.pop("_error_signatures", None)
    return diagnostics


def normalize_station_name(value: str | None) -> str:
    if not value:
        return ""

    normalized = value.upper().replace("Ё", "Е")
    normalized = normalized.replace("-", " ")
    normalized = normalized.replace("—", " ")
    normalized = normalized.replace("–", " ")
    normalized = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def canonical_tokens(value: str | None) -> list[str]:
    normalized = normalize_station_name(value)
    if not normalized:
        return []

    tokens: list[str] = []

    for raw_token in normalized.split():
        token = NORMALIZATION_REPLACEMENTS.get(raw_token, raw_token)
        if token in DROP_NAME_TOKENS:
            continue
        tokens.append(token)

    return tokens


def canonical_name(value: str | None) -> str:
    return " ".join(canonical_tokens(value))


def sequence_ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def token_prefix_match(raw_token: str, candidate_token: str) -> bool:
    if not raw_token or not candidate_token:
        return False

    if raw_token == candidate_token:
        return True

    if len(raw_token) >= 3 and candidate_token.startswith(raw_token):
        return True

    if len(candidate_token) >= 3 and raw_token.startswith(candidate_token):
        return True

    if len(raw_token) == 1 and candidate_token.startswith(raw_token):
        return True

    return False


def token_overlap_score(raw_tokens: list[str], candidate_tokens: list[str]) -> float:
    if not raw_tokens or not candidate_tokens:
        return 0.0

    matched = 0
    used_candidate_indexes: set[int] = set()

    for raw_token in raw_tokens:
        for idx, candidate_token in enumerate(candidate_tokens):
            if idx in used_candidate_indexes:
                continue
            if token_prefix_match(raw_token, candidate_token):
                matched += 1
                used_candidate_indexes.add(idx)
                break

    return matched / max(1, len(raw_tokens))


def reverse_token_overlap_score(raw_tokens: list[str], candidate_tokens: list[str]) -> float:
    if not raw_tokens or not candidate_tokens:
        return 0.0

    matched = 0
    used_raw_indexes: set[int] = set()

    for candidate_token in candidate_tokens:
        for idx, raw_token in enumerate(raw_tokens):
            if idx in used_raw_indexes:
                continue
            if token_prefix_match(raw_token, candidate_token):
                matched += 1
                used_raw_indexes.add(idx)
                break

    return matched / max(1, len(candidate_tokens))


def compute_name_similarity(raw_name: str | None, candidate_name: str | None) -> float:
    raw_canonical = canonical_name(raw_name)
    candidate_canonical = canonical_name(candidate_name)

    if not raw_canonical or not candidate_canonical:
        return 0.0

    if raw_canonical == candidate_canonical:
        return 1.0

    raw_tokens = raw_canonical.split()
    candidate_tokens = candidate_canonical.split()

    ratio = sequence_ratio(raw_canonical, candidate_canonical)
    overlap = token_overlap_score(raw_tokens, candidate_tokens)
    reverse_overlap = reverse_token_overlap_score(raw_tokens, candidate_tokens)

    first_token_bonus = 0.0
    if raw_tokens and candidate_tokens and token_prefix_match(raw_tokens[0], candidate_tokens[0]):
        first_token_bonus = 0.10

    score = (
        ratio * 0.45
        + overlap * 0.35
        + reverse_overlap * 0.20
        + first_token_bonus
    )

    return max(0.0, min(1.0, score))


def parse_geometry_coords(geometry_json: str | None) -> list[list[float]]:
    if not geometry_json:
        return []

    try:
        geometry = json.loads(geometry_json)
    except Exception:
        return []

    geometry_type = geometry.get("type")
    coords = geometry.get("coordinates")

    if geometry_type == "LineString" and isinstance(coords, list):
        try:
            return [[float(x), float(y)] for x, y in coords]
        except Exception:
            return []

    return []


def reverse_coords(coords: list[list[float]]) -> list[list[float]]:
    return list(reversed(coords))


def merge_coordinate_sequences(sequences: list[list[list[float]]]) -> list[list[float]]:
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


def build_linestring_or_multilinestring(
    segment_coordinate_groups: list[list[list[float]]],
) -> dict[str, Any] | None:
    non_empty_groups = [group for group in segment_coordinate_groups if group]

    if not non_empty_groups:
        return None

    if len(non_empty_groups) == 1:
        coords = non_empty_groups[0]
        if len(coords) < 2:
            return None
        return {
            "type": "LineString",
            "coordinates": coords,
        }

    normalized_groups = [group for group in non_empty_groups if len(group) >= 2]
    if not normalized_groups:
        return None

    return {
        "type": "MultiLineString",
        "coordinates": normalized_groups,
    }


def build_simple_linestring(coords: list[list[float]]) -> dict[str, Any] | None:
    normalized: list[list[float]] = []

    for coord in coords:
        if len(coord) != 2:
            continue
        if normalized and normalized[-1] == coord:
            continue
        normalized.append(coord)

    if len(normalized) < 2:
        return None

    return {
        "type": "LineString",
        "coordinates": normalized,
    }


def safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(1e-12, 1.0 - a)))
    return radius_km * c


def region_codes_cache_key(region_codes: list[str] | None) -> str:
    if not region_codes:
        return "__all__"
    return "|".join(sorted(region_codes))


def unique_non_empty(values: list[str | None]) -> list[str]:
    result: list[str] = []
    seen = set()

    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        result.append(value)

    return result


def build_scope_key(region_codes: list[str]) -> str:
    return "|".join(sorted(unique_non_empty(region_codes)))


def build_region_filter_clause(
    region_codes: list[str] | None,
    *,
    column_name: str,
    params: dict[str, Any],
    param_prefix: str,
) -> str:
    if not region_codes:
        return ""

    placeholders: list[str] = []

    for index, code in enumerate(region_codes):
        param_name = f"{param_prefix}_{index}"
        params[param_name] = code
        placeholders.append(f":{param_name}")

    return f" AND {column_name} IN ({', '.join(placeholders)}) "


def load_route(route_id: int) -> dict[str, Any]:
    route_query = text("""
        SELECT
            id,
            source_system,
            external_route_id,
            train_number,
            route_name,
            origin_station_name,
            destination_station_name,
            origin_station_code,
            destination_station_code,
            snapshot_date,
            operates_from,
            operates_to,
            is_active,
            notes,
            created_at,
            updated_at
        FROM routes
        WHERE id = :route_id
        LIMIT 1;
    """)

    stops_query = text("""
        SELECT
            rs.id,
            rs.route_id,
            rs.stop_sequence,
            rs.station_name_raw,
            rs.station_code_rzd,
            rs.station_id AS stored_station_id,
            rs.arrival_time,
            rs.departure_time,
            rs.stop_duration_minutes,
            rs.distance_km,
            rs.is_origin,
            rs.is_destination,
            rs.match_method AS stored_match_method,
            rs.match_confidence AS stored_match_confidence,
            s.name AS stored_station_name,
            s.region_code AS stored_station_region_code,
            ST_X(s.geom) AS stored_lon,
            ST_Y(s.geom) AS stored_lat,
            s.is_visible_default AS stored_station_visible,
            s.uic_ref AS stored_station_uic,
            s.esr_user AS stored_station_esr
        FROM route_stops rs
        LEFT JOIN stations s ON s.id = rs.station_id
        WHERE rs.route_id = :route_id
        ORDER BY rs.stop_sequence, rs.id;
    """)

    with engine.connect() as connection:
        route_row = connection.execute(route_query, {"route_id": route_id}).first()
        if route_row is None:
            raise ValueError("Route not found")

        stop_rows = connection.execute(stops_query, {"route_id": route_id}).fetchall()

    route = dict(route_row._mapping)
    stops = [dict(row._mapping) for row in stop_rows]

    return {
        "route": route,
        "stops": stops,
    }


def _build_station_catalog_from_rows(station_rows: list[Any]) -> tuple[
    dict[int, dict[str, Any]],
    list[dict[str, Any]],
    dict[str, list[int]],
    dict[str, list[int]],
]:
    stations_by_id: dict[int, dict[str, Any]] = {}
    catalog: list[dict[str, Any]] = []
    code_index_uic: dict[str, list[int]] = defaultdict(list)
    code_index_esr: dict[str, list[int]] = defaultdict(list)

    for row in station_rows:
        item = dict(row._mapping)
        station_id = int(item["id"])
        station_name = item.get("name") or ""

        station_data = {
            "station_id": station_id,
            "region_code": item.get("region_code"),
            "name": station_name,
            "lon": float(item["lon"]),
            "lat": float(item["lat"]),
            "uic_ref": (item.get("uic_ref") or "").strip() or None,
            "esr_user": (item.get("esr_user") or "").strip() or None,
            "is_main_rail_station": bool(item.get("is_main_rail_station")),
            "normalized_name": canonical_name(station_name),
            "tokens": canonical_tokens(station_name),
        }
        stations_by_id[station_id] = station_data
        catalog.append(station_data)

        if station_data["uic_ref"]:
            code_index_uic[station_data["uic_ref"]].append(station_id)
        if station_data["esr_user"]:
            code_index_esr[station_data["esr_user"]].append(station_id)

    return stations_by_id, catalog, dict(code_index_uic), dict(code_index_esr)


def load_global_station_catalog(
    *,
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    logger_context = logger_context or {}

    station_query = text("""
        SELECT
            s.id,
            s.region_code,
            s.name,
            s.uic_ref,
            s.esr_user,
            s.is_main_rail_station,
            ST_X(s.geom) AS lon,
            ST_Y(s.geom) AS lat
        FROM stations s
        WHERE s.is_visible_default = TRUE
          AND s.geom IS NOT NULL;
    """)

    with StageTimer(
        "global_station_catalog_query",
        diagnostics=diagnostics,
        logger_context=logger_context,
    ):
        with engine.connect() as connection:
            station_rows = connection.execute(station_query).fetchall()

    stations_by_id, catalog, code_index_uic, code_index_esr = _build_station_catalog_from_rows(station_rows)

    if diagnostics is not None:
        diagnostics.setdefault("catalog", {})
        diagnostics["catalog"]["visible_station_rows_count"] = len(station_rows)

    log_event(
        "info",
        "global_station_catalog_loaded",
        visible_stations_count=len(stations_by_id),
        **logger_context,
    )

    return {
        "stations_by_id": stations_by_id,
        "catalog": catalog,
        "code_index_uic": code_index_uic,
        "code_index_esr": code_index_esr,
    }


def candidate_from_station_data(
    station_data: dict[str, Any],
    *,
    name_score: float,
    code_match: bool,
    anchor: bool,
    match_method: str,
    match_reason: str,
    code_value: str | None = None,
) -> Candidate:
    effective_score = name_score

    if code_match:
        effective_score = max(effective_score, 0.92)
    if anchor:
        effective_score = max(effective_score, 0.97)
    if station_data.get("is_main_rail_station"):
        effective_score = min(1.0, effective_score + 0.03)

    return Candidate(
        station_id=int(station_data["station_id"]),
        region_code=station_data.get("region_code"),
        name=station_data["name"],
        lon=float(station_data["lon"]),
        lat=float(station_data["lat"]),
        effective_score=max(0.01, min(1.0, effective_score)),
        name_score=max(0.0, min(1.0, name_score)),
        code_match=code_match,
        anchor=anchor,
        is_main_rail_station=bool(station_data.get("is_main_rail_station")),
        match_method=match_method,
        match_reason=match_reason,
        code_value=code_value,
    )


def build_candidates_for_stop(
    stop: dict[str, Any],
    catalog_payload: dict[str, Any],
) -> list[Candidate]:
    stations_by_id = catalog_payload["stations_by_id"]
    catalog = catalog_payload["catalog"]
    code_index_uic = catalog_payload["code_index_uic"]
    code_index_esr = catalog_payload["code_index_esr"]

    station_name_raw = stop.get("station_name_raw")
    station_code_rzd = (stop.get("station_code_rzd") or "").strip() or None
    stored_station_id = stop.get("stored_station_id")

    candidates_by_station_id: dict[int, Candidate] = {}

    def add_candidate(candidate: Candidate) -> None:
        existing = candidates_by_station_id.get(candidate.station_id)
        if existing is None or candidate.effective_score > existing.effective_score:
            candidates_by_station_id[candidate.station_id] = candidate

    if stored_station_id is not None:
        station_data = stations_by_id.get(int(stored_station_id))
        if station_data is not None:
            add_candidate(
                candidate_from_station_data(
                    station_data,
                    name_score=max(
                        0.75,
                        compute_name_similarity(station_name_raw, station_data["name"]),
                    ),
                    code_match=False,
                    anchor=True,
                    match_method="existing_visible_station_id",
                    match_reason="stored_visible_station_id",
                )
            )

    if station_code_rzd:
        for station_id in code_index_esr.get(station_code_rzd, []):
            station_data = stations_by_id.get(int(station_id))
            if station_data is None:
                continue
            add_candidate(
                candidate_from_station_data(
                    station_data,
                    name_score=max(
                        0.80,
                        compute_name_similarity(station_name_raw, station_data["name"]),
                    ),
                    code_match=True,
                    anchor=False,
                    match_method="exact_visible_esr_code",
                    match_reason="exact_visible_esr_code",
                    code_value=station_code_rzd,
                )
            )

        for station_id in code_index_uic.get(station_code_rzd, []):
            station_data = stations_by_id.get(int(station_id))
            if station_data is None:
                continue
            add_candidate(
                candidate_from_station_data(
                    station_data,
                    name_score=max(
                        0.78,
                        compute_name_similarity(station_name_raw, station_data["name"]),
                    ),
                    code_match=True,
                    anchor=False,
                    match_method="exact_visible_uic_code",
                    match_reason="exact_visible_uic_code",
                    code_value=station_code_rzd,
                )
            )

    scored: list[tuple[float, dict[str, Any]]] = []

    for station_data in catalog:
        score = compute_name_similarity(station_name_raw, station_data["name"])
        if score < 0.18:
            continue

        bonus = 0.03 if station_data.get("is_main_rail_station") else 0.0
        scored.append((min(1.0, score + bonus), station_data))

    scored.sort(key=lambda item: item[0], reverse=True)

    for score, station_data in scored[:MAX_CANDIDATES_PER_STOP]:
        add_candidate(
            candidate_from_station_data(
                station_data,
                name_score=score,
                code_match=False,
                anchor=False,
                match_method="name_candidate",
                match_reason="name_similarity",
            )
        )

    if not candidates_by_station_id:
        relaxed_scored: list[tuple[float, dict[str, Any]]] = []

        for station_data in catalog:
            score = compute_name_similarity(station_name_raw, station_data["name"])
            relaxed_scored.append((score, station_data))

        relaxed_scored.sort(key=lambda item: item[0], reverse=True)

        for score, station_data in relaxed_scored[: min(5, len(relaxed_scored))]:
            add_candidate(
                candidate_from_station_data(
                    station_data,
                    name_score=max(0.05, score),
                    code_match=False,
                    anchor=False,
                    match_method="fallback_name_candidate",
                    match_reason="fallback_name_similarity",
                )
            )

    candidates = list(candidates_by_station_id.values())
    candidates.sort(
        key=lambda item: (
            item.code_match,
            item.anchor,
            item.effective_score,
            item.is_main_rail_station,
            -item.station_id,
        ),
        reverse=True,
    )

    return candidates[:MAX_CANDIDATES_PER_STOP]


def derive_route_region_hints(
    stops: list[dict[str, Any]],
    candidates_per_stop: list[list[Candidate]],
) -> dict[str, Any]:
    stored_region_codes: list[str] = []
    anchor_region_codes: list[str] = []
    exact_region_codes: list[str] = []
    top1_region_codes: list[str] = []
    top1_high_conf_region_codes: list[str] = []

    for stop, candidates in zip(stops, candidates_per_stop):
        stored_station_region_code = stop.get("stored_station_region_code")
        stored_station_visible = bool(stop.get("stored_station_visible"))

        if stored_station_region_code and stored_station_visible:
            stored_region_codes.append(stored_station_region_code)

        if not candidates:
            continue

        top_candidate = candidates[0]
        if top_candidate.region_code:
            top1_region_codes.append(top_candidate.region_code)

        if top_candidate.region_code and (
            top_candidate.anchor
            or top_candidate.code_match
            or top_candidate.effective_score >= 0.88
        ):
            top1_high_conf_region_codes.append(top_candidate.region_code)

        for candidate in candidates[:2]:
            if candidate.region_code and candidate.anchor:
                anchor_region_codes.append(candidate.region_code)
                break

        for candidate in candidates[:2]:
            if candidate.region_code and candidate.code_match:
                exact_region_codes.append(candidate.region_code)
                break

    strict_region_codes = unique_non_empty(
        stored_region_codes
        + anchor_region_codes
        + exact_region_codes
        + top1_high_conf_region_codes
    )[:MAX_ROUTE_REGION_CODES]

    return {
        "stored_region_codes": unique_non_empty(stored_region_codes),
        "anchor_region_codes": unique_non_empty(anchor_region_codes),
        "exact_region_codes": unique_non_empty(exact_region_codes),
        "top1_region_codes": unique_non_empty(top1_region_codes),
        "top1_high_conf_region_codes": unique_non_empty(top1_high_conf_region_codes),
        "strict_region_codes": strict_region_codes,
    }


def expand_corridor_region_codes(region_codes: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def add(code: str | None) -> None:
        if not code:
            return
        if code in seen:
            return
        seen.add(code)
        result.append(code)

    for code in region_codes:
        add(code)

    codes = set(result)

    # Москва / центр → Урал почти всегда требует Поволжье как транзитный коридор.
    if "central_fd" in codes and "ural_fd" in codes:
        add("volga_fd")

    # Центр → Сибирь требует Поволжье и Урал.
    if "central_fd" in codes and "siberian_fd" in codes:
        add("volga_fd")
        add("ural_fd")

    # Центр → Дальний Восток требует весь восточный коридор.
    if "central_fd" in codes and "far_eastern_fd" in codes:
        add("volga_fd")
        add("ural_fd")
        add("siberian_fd")

    # Урал → Дальний Восток обычно требует Сибирь.
    if "ural_fd" in codes and "far_eastern_fd" in codes:
        add("siberian_fd")

    # Поволжье → Дальний Восток обычно требует Урал и Сибирь.
    if "volga_fd" in codes and "far_eastern_fd" in codes:
        add("ural_fd")
        add("siberian_fd")

    return result[:MAX_ROUTE_REGION_CODES]


def infer_route_region_codes(
    stops: list[dict[str, Any]],
    candidates_per_stop: list[list[Candidate]],
    *,
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
) -> list[str]:
    logger_context = logger_context or {}

    region_hints = derive_route_region_hints(stops, candidates_per_stop)

    region_scores: dict[str, float] = defaultdict(float)
    first_seen_index: dict[str, int] = {}

    for stop_index, candidates in enumerate(candidates_per_stop):
        if not candidates:
            continue

        top_candidate = candidates[0]
        if not top_candidate.region_code:
            continue

        weight = max(0.1, top_candidate.effective_score) * 3.0

        if top_candidate.code_match:
            weight += 2.0
        if top_candidate.anchor:
            weight += 2.5
        if top_candidate.effective_score >= 0.88:
            weight += 0.8

        region_scores[top_candidate.region_code] += weight
        first_seen_index.setdefault(top_candidate.region_code, stop_index)

    scored_regions = sorted(
        region_scores.items(),
        key=lambda item: (-item[1], first_seen_index.get(item[0], 10_000), item[0]),
    )
    scored_region_codes = [region_code for region_code, _ in scored_regions]

    inferred_region_codes = unique_non_empty(
        region_hints["strict_region_codes"]
        + region_hints["top1_region_codes"]
        + scored_region_codes
    )[:MAX_ROUTE_REGION_CODES]

    payload = {
        "inferred_region_codes": inferred_region_codes,
        "strict_region_codes": region_hints["strict_region_codes"],
        "stored_region_codes": region_hints["stored_region_codes"],
        "anchor_region_codes": region_hints["anchor_region_codes"],
        "exact_region_codes": region_hints["exact_region_codes"],
        "top1_region_codes": region_hints["top1_region_codes"],
        "top1_high_conf_region_codes": region_hints["top1_high_conf_region_codes"],
        "region_scores": {
            region_code: round(score, 4)
            for region_code, score in scored_regions
        },
    }

    if diagnostics is not None:
        diagnostics["inferred_route_regions"] = payload

    log_event(
        "info",
        "route_regions_inferred",
        **payload,
        **logger_context,
    )

    expanded_region_codes = expand_corridor_region_codes(inferred_region_codes)

    if diagnostics is not None:
        diagnostics["inferred_route_regions"]["expanded_region_codes"] = expanded_region_codes

    log_event(
        "info",
        "route_regions_expanded_for_corridor",
        original_region_codes=inferred_region_codes,
        expanded_region_codes=expanded_region_codes,
        **logger_context,
    )

    return expanded_region_codes


def add_topology_edge_to_adjacency(
    adjacency: dict[str, list[dict[str, Any]]],
    *,
    from_node_hash: str,
    to_node_hash: str,
    edge_id: int | None,
    length_km: float,
    geometry_coords: list[list[float]],
    edge_source: str | None = None,
    is_virtual_connector: bool = False,
    reversed_direction: bool = False,
) -> None:
    if not from_node_hash or not to_node_hash:
        return

    if from_node_hash == to_node_hash:
        return

    if length_km <= 0:
        return

    adjacency[from_node_hash].append(
        {
            "edge_id": edge_id,
            "id": edge_id,
            "from_node_hash": from_node_hash,
            "to_node_hash": to_node_hash,
            "length_km": float(length_km),
            "geometry_coords": geometry_coords or [],
            "edge_source": edge_source,
            "is_virtual_connector": bool(is_virtual_connector),
            "reversed_direction": bool(reversed_direction),
        }
    )


def add_runtime_station_transfer_edges(
    *,
    adjacency: dict[str, list[dict[str, Any]]],
    station_links: dict[int, list[dict[str, Any]]],
    stations_by_id: dict[int, dict[str, Any]],
) -> int:
    """
    Добавляет runtime-переходы внутри станции между несколькими node_hash.

    Зачем:
    если станция связана с несколькими компонентами topology graph,
    маршрут должен иметь возможность пройти через эту станцию как через узел пересадки.

    В БД ничего не пишем.
    Это только runtime-слой внутри build_network_data().
    """

    created_pairs_count = 0

    for station_id, links in station_links.items():
        station = stations_by_id.get(int(station_id))

        if not station:
            continue

        station_lon = safe_float(station.get("lon"))
        station_lat = safe_float(station.get("lat"))

        if station_lon is None or station_lat is None:
            continue

        best_by_node_hash: dict[str, dict[str, Any]] = {}

        for link in links or []:
            node_hash = str(link.get("node_hash") or "")
            if not node_hash:
                continue

            link_distance_km = safe_float(link.get("link_distance_km"))
            node_lon = safe_float(link.get("node_lon"))
            node_lat = safe_float(link.get("node_lat"))

            if link_distance_km is None or node_lon is None or node_lat is None:
                continue

            if link_distance_km > STATION_TRANSFER_MAX_LINK_KM:
                continue

            existing = best_by_node_hash.get(node_hash)

            if existing is None or link_distance_km < float(existing["link_distance_km"]):
                best_by_node_hash[node_hash] = {
                    **link,
                    "node_hash": node_hash,
                    "link_distance_km": float(link_distance_km),
                    "node_lon": float(node_lon),
                    "node_lat": float(node_lat),
                }

        transfer_links = list(best_by_node_hash.values())
        transfer_links.sort(
            key=lambda item: (
                float(item.get("link_distance_km") or 999999.0),
                str(item.get("node_hash") or ""),
            )
        )
        transfer_links = transfer_links[:STATION_TRANSFER_MAX_LINKS_PER_STATION]

        if len(transfer_links) < 2:
            continue

        for left_index in range(len(transfer_links)):
            for right_index in range(left_index + 1, len(transfer_links)):
                left = transfer_links[left_index]
                right = transfer_links[right_index]

                left_node_hash = str(left["node_hash"])
                right_node_hash = str(right["node_hash"])

                if left_node_hash == right_node_hash:
                    continue

                transfer_length_km = (
                    float(left.get("link_distance_km") or 0.0)
                    + float(right.get("link_distance_km") or 0.0)
                )

                if transfer_length_km <= 0:
                    continue

                if transfer_length_km > STATION_TRANSFER_MAX_PAIR_KM:
                    continue

                forward_geometry = [
                    [float(left["node_lon"]), float(left["node_lat"])],
                    [float(station_lon), float(station_lat)],
                    [float(right["node_lon"]), float(right["node_lat"])],
                ]

                reverse_geometry = reverse_coords(forward_geometry)

                add_topology_edge_to_adjacency(
                    adjacency,
                    from_node_hash=left_node_hash,
                    to_node_hash=right_node_hash,
                    edge_id=None,
                    length_km=transfer_length_km,
                    geometry_coords=forward_geometry,
                    edge_source=STATION_TRANSFER_EDGE_SOURCE,
                    is_virtual_connector=True,
                    reversed_direction=False,
                )

                add_topology_edge_to_adjacency(
                    adjacency,
                    from_node_hash=right_node_hash,
                    to_node_hash=left_node_hash,
                    edge_id=None,
                    length_km=transfer_length_km,
                    geometry_coords=reverse_geometry,
                    edge_source=STATION_TRANSFER_EDGE_SOURCE,
                    is_virtual_connector=True,
                    reversed_direction=True,
                )

                created_pairs_count += 1

    return created_pairs_count


def build_network_data(
    *,
    region_codes: list[str] | None,
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    logger_context = logger_context or {}
    network_diag = diagnostics.setdefault("network", {}) if diagnostics is not None else {}

    region_codes = unique_non_empty(region_codes or [])
    cache_key = region_codes_cache_key(region_codes)
    scope_key = build_scope_key(region_codes)

    emit_progress(
        progress_callback,
        54,
        "network",
        {
            "message": "Проверка кэша topology graph по округам маршрута",
            "region_codes": region_codes,
            "scope_key": scope_key,
        },
    )

    now = time.time()
    with _GRAPH_CACHE_LOCK:
        cached = _GRAPH_CACHE["cache_by_region_key"].get(cache_key)
        if cached is not None and now - float(cached["timestamp"]) <= GRAPH_CACHE_TTL_SECONDS:
            if diagnostics is not None:
                network_diag.clear()
                network_diag["cache_hit"] = True
                network_diag["cache_age_seconds"] = round(now - float(cached["timestamp"]), 2)
                network_diag["requested_region_codes"] = region_codes
                network_diag["scope_key"] = scope_key
                network_diag["stats"] = cached["data"].get("stats") or {}

            log_event(
                "info",
                "topology_network_cache_hit",
                cache_key=cache_key,
                scope_key=scope_key,
                region_codes=region_codes,
                cache_age_seconds=round(now - float(cached["timestamp"]), 2),
                **logger_context,
            )
            return cached["data"]

    if diagnostics is not None:
        network_diag.clear()
        network_diag["cache_hit"] = False
        network_diag["requested_region_codes"] = region_codes
        network_diag["scope_key"] = scope_key

    params: dict[str, Any] = {}
    station_region_clause = build_region_filter_clause(
        region_codes,
        column_name="s.region_code",
        params=params,
        param_prefix="station_region",
    )

    station_query = text(f"""
        SELECT
            s.id,
            s.region_code,
            s.name,
            s.uic_ref,
            s.esr_user,
            s.is_main_rail_station,
            ST_X(s.geom) AS lon,
            ST_Y(s.geom) AS lat
        FROM stations s
        WHERE s.is_visible_default = TRUE
          AND s.geom IS NOT NULL
          {station_region_clause};
    """)

    nodes_query = text("""
        SELECT
            n.node_hash,
            n.lon,
            n.lat
        FROM rail_graph_nodes n
        WHERE n.scope_key = :scope_key;
    """)

    edges_query = text("""
        SELECT
            e.id,
            e.source_node_hash,
            e.target_node_hash,
            e.length_km,
            e.edge_source,
            COALESCE(e.is_virtual_connector, FALSE) AS is_virtual_connector,
            ST_AsGeoJSON(e.geom) AS geometry
        FROM rail_graph_edges e
        WHERE e.scope_key = :scope_key;
    """)

    connectors_query = text("""
        SELECT
            NULL::integer AS id,
            c.source_node_hash,
            c.target_node_hash,
            c.length_km,
            'rail_graph_connector' AS edge_source,
            TRUE AS is_virtual_connector,
            ST_AsGeoJSON(c.geom) AS geometry
        FROM rail_graph_connectors c
        WHERE c.enabled = TRUE
          AND c.scope_key = :scope_key
          AND c.source_node_hash IS NOT NULL
          AND c.target_node_hash IS NOT NULL
          AND c.length_km IS NOT NULL
          AND c.length_km > 0;
    """)

    station_links_query = text("""
        SELECT
            l.station_id,
            l.node_hash,
            l.link_distance_m,
            l.is_primary,
            n.lon AS node_lon,
            n.lat AS node_lat
        FROM station_graph_links l
        JOIN rail_graph_nodes n
          ON n.scope_key = l.scope_key
         AND n.node_hash = l.node_hash
        WHERE l.scope_key = :scope_key
        ORDER BY
            l.station_id,
            l.is_primary DESC,
            l.link_distance_m,
            l.node_hash;
    """)

    emit_progress(
        progress_callback,
        58,
        "network",
        {
            "message": "Загрузка станций corridor маршрута",
            "region_codes": region_codes,
            "scope_key": scope_key,
        },
    )

    with StageTimer(
        "regional_station_catalog_query",
        diagnostics=diagnostics,
        logger_context=logger_context,
    ):
        with engine.connect() as connection:
            station_rows = connection.execute(station_query, params).fetchall()

    if diagnostics is not None:
        network_diag["regional_station_rows_count"] = len(station_rows)
        network_diag["region_codes"] = region_codes

    stations_by_id, catalog, code_index_uic, code_index_esr = _build_station_catalog_from_rows(station_rows)

    emit_progress(
        progress_callback,
        62,
        "network",
        {
            "message": "Загрузка topology graph из подготовленных таблиц",
            "scope_key": scope_key,
        },
    )

    with StageTimer(
        "topology_graph_load",
        diagnostics=diagnostics,
        logger_context=logger_context,
    ):
        with engine.connect() as connection:
            node_rows = connection.execute(nodes_query, {"scope_key": scope_key}).fetchall()
            edge_rows = connection.execute(edges_query, {"scope_key": scope_key}).fetchall()

            has_connectors_table = connection.execute(
                text("SELECT to_regclass('public.rail_graph_connectors')")
            ).scalar() is not None

            if has_connectors_table:
                connector_rows = connection.execute(
                    connectors_query,
                    {"scope_key": scope_key},
                ).fetchall()
            else:
                connector_rows = []

            station_link_rows = connection.execute(
                station_links_query,
                {"scope_key": scope_key},
            ).fetchall()

    node_coords: dict[str, dict[str, float]] = {}
    for row in node_rows:
        item = dict(row._mapping)
        node_hash = str(item["node_hash"])
        node_coords[node_hash] = {
            "lon": float(item["lon"]),
            "lat": float(item["lat"]),
        }

    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped_edge_rows_count = 0
    undirected_edge_rows_count = 0

    all_edge_rows = list(edge_rows) + list(connector_rows)

    for row in all_edge_rows:
        item = dict(row._mapping)

        edge_id = item.get("id")
        source_node_hash = str(item.get("source_node_hash") or "")
        target_node_hash = str(item.get("target_node_hash") or "")
        length_km = safe_float(item.get("length_km"))
        edge_source = item.get("edge_source") or "rail_graph_edge"
        is_virtual_connector = bool(item.get("is_virtual_connector"))

        if not source_node_hash or not target_node_hash or source_node_hash == target_node_hash:
            skipped_edge_rows_count += 1
            continue

        if length_km is None or length_km <= 0:
            skipped_edge_rows_count += 1
            continue

        geometry_coords = parse_geometry_coords(item.get("geometry"))

        if len(geometry_coords) < 2:
            source_node = node_coords.get(source_node_hash)
            target_node = node_coords.get(target_node_hash)

            if source_node and target_node:
                geometry_coords = [
                    [source_node["lon"], source_node["lat"]],
                    [target_node["lon"], target_node["lat"]],
                ]

        if len(geometry_coords) < 2:
            skipped_edge_rows_count += 1
            continue

        # Важно: здесь мы намеренно делаем граф НЕОРИЕНТИРОВАННЫМ.
        # OSM way direction в нашем проекте не считается ограничением движения.
        add_topology_edge_to_adjacency(
            adjacency,
            from_node_hash=source_node_hash,
            to_node_hash=target_node_hash,
            edge_id=edge_id,
            length_km=float(length_km),
            geometry_coords=geometry_coords,
            edge_source=edge_source,
            is_virtual_connector=is_virtual_connector,
            reversed_direction=False,
        )

        add_topology_edge_to_adjacency(
            adjacency,
            from_node_hash=target_node_hash,
            to_node_hash=source_node_hash,
            edge_id=edge_id,
            length_km=float(length_km),
            geometry_coords=reverse_coords(geometry_coords),
            edge_source=edge_source,
            is_virtual_connector=is_virtual_connector,
            reversed_direction=True,
        )

        undirected_edge_rows_count += 1

    station_links: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in station_link_rows:
        item = dict(row._mapping)
        station_id = int(item["station_id"])
        station_links[station_id].append(
            {
                "node_hash": str(item["node_hash"]),
                "link_distance_km": float(item["link_distance_m"]) / 1000.0,
                "is_primary": bool(item["is_primary"]),
                "node_lon": float(item["node_lon"]),
                "node_lat": float(item["node_lat"]),
                "source": "station_link",
            }
        )

    runtime_station_transfer_pairs_count = add_runtime_station_transfer_edges(
        adjacency=adjacency,
        station_links=station_links,
        stations_by_id=stations_by_id,
    )

    stats = {
        "network_mode": "scope_topology_graph",
        "region_codes": region_codes,
        "scope_key": scope_key,
        "visible_stations_count": len(stations_by_id),
        "adjacency_node_count": len(node_coords),

        # raw_edge_rows_count — сколько строк пришло из rail_graph_edges.
        # undirected_edge_rows_count — сколько валидных физических ребер использовано.
        # directed_edge_count — сколько направленных переходов получилось в adjacency.
        # Для неориентированного графа directed_edge_count обычно примерно в 2 раза больше.
        "raw_edge_rows_count": len(edge_rows),
        "connector_edge_rows_count": len(connector_rows),
        "undirected_edge_rows_count": undirected_edge_rows_count,
        "skipped_edge_rows_count": skipped_edge_rows_count,
        "directed_edge_count": sum(len(edges) for edges in adjacency.values()),
        "graph_is_bidirectional": True,
        "runtime_station_transfer_pairs_count": runtime_station_transfer_pairs_count,
        "runtime_station_transfer_enabled": True,

        "topology_station_links_count": sum(len(items) for items in station_links.values()),
    }

    if diagnostics is not None:
        network_diag["raw_edge_rows_count"] = len(edge_rows)
        network_diag["connector_edge_rows_count"] = len(connector_rows)
        network_diag["stats"] = stats

    if not node_coords or not adjacency:
        stats = {
            **stats,
            "network_mode": "topology_not_built",
            "adjacency_node_count": 0,
            "directed_edge_count": 0,
            "topology_station_links_count": 0,
            "build_hint": "Run python -m scripts.build_route_scope_topology --route-id <route_id>",
        }

        if diagnostics is not None:
            network_diag["stats"] = stats

        log_event(
            "warning",
            "topology_graph_missing_for_scope",
            stats=stats,
            **logger_context,
        )

        return {
            "scope_key": scope_key,
            "stations_by_id": stations_by_id,
            "catalog": catalog,
            "code_index_uic": code_index_uic,
            "code_index_esr": code_index_esr,
            "node_coords": {},
            "adjacency": {},
            "station_links": {},
            "stats": stats,
        }

    data = {
        "scope_key": scope_key,
        "stations_by_id": stations_by_id,
        "catalog": catalog,
        "code_index_uic": code_index_uic,
        "code_index_esr": code_index_esr,
        "node_coords": node_coords,
        "adjacency": dict(adjacency),
        "station_links": dict(station_links),
        "stats": stats,
    }

    with _GRAPH_CACHE_LOCK:
        _GRAPH_CACHE["cache_by_region_key"][cache_key] = {
            "timestamp": time.time(),
            "data": data,
        }

    log_event(
        "info",
        "topology_network_loaded",
        stats=stats,
        **logger_context,
    )

    return data


def _build_reverse_topology_path_result(path: dict[str, Any]) -> dict[str, Any]:
    reversed_edge_chain = []

    for edge in reversed(path.get("edge_chain") or []):
        reversed_edge_chain.append(
            {
                **edge,
                "from_node_hash": edge.get("to_node_hash"),
                "to_node_hash": edge.get("from_node_hash"),
                "geometry_coords": reverse_coords(edge.get("geometry_coords") or []),
                "reversed_direction": not bool(edge.get("reversed_direction")),
            }
        )

    return {
        "distance_km": float(path["distance_km"]),
        "node_path": list(reversed(path.get("node_path") or [])),
        "coordinates": reverse_coords(path.get("coordinates") or []),
        "edge_chain": reversed_edge_chain,
        "hop_count": int(path.get("hop_count") or 0),
    }


def dijkstra_topology_path(
    adjacency: dict[str, list[dict[str, Any]]],
    node_coords: dict[str, dict[str, float]],
    start_node_hash: str,
    end_node_hash: str,
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
) -> dict[str, Any] | None:
    cache_key = (start_node_hash, end_node_hash)
    if cache_key in path_cache:
        return path_cache[cache_key]

    reverse_key = (end_node_hash, start_node_hash)
    reverse_cached = path_cache.get(reverse_key)
    if reverse_cached is not None:
        reversed_path = _build_reverse_topology_path_result(reverse_cached)
        path_cache[cache_key] = reversed_path
        return reversed_path

    if start_node_hash == end_node_hash:
        coords = []
        node = node_coords.get(start_node_hash)
        if node is not None:
            coords = [[node["lon"], node["lat"]], [node["lon"], node["lat"]]]
        result = {
            "distance_km": 0.0,
            "node_path": [start_node_hash],
            "coordinates": coords,
            "edge_chain": [],
            "hop_count": 0,
        }
        path_cache[cache_key] = result
        return result

    queue: list[tuple[float, str]] = [(0.0, start_node_hash)]
    distances: dict[str, float] = {start_node_hash: 0.0}
    previous: dict[str, tuple[str, dict[str, Any]]] = {}

    while queue:
        current_distance, current_node_hash = heappop(queue)

        if current_node_hash == end_node_hash:
            break

        if current_distance > distances.get(current_node_hash, math.inf):
            continue

        for edge in adjacency.get(current_node_hash, []):
            next_node_hash = str(edge["to_node_hash"])
            edge_length = float(edge["length_km"])
            next_distance = current_distance + edge_length

            if next_distance < distances.get(next_node_hash, math.inf):
                distances[next_node_hash] = next_distance
                previous[next_node_hash] = (current_node_hash, edge)
                heappush(queue, (next_distance, next_node_hash))

    if end_node_hash not in distances:
        path_cache[cache_key] = None
        return None

    node_path = [end_node_hash]
    edge_chain_reversed: list[dict[str, Any]] = []
    current_node_hash = end_node_hash

    while current_node_hash != start_node_hash:
        prev_node_hash, edge = previous[current_node_hash]
        edge_chain_reversed.append(
            {
                "edge_id": edge.get("edge_id") or edge.get("id"),
                "id": edge.get("edge_id") or edge.get("id"),
                "from_node_hash": prev_node_hash,
                "to_node_hash": current_node_hash,
                "length_km": float(edge["length_km"]),
                "geometry_coords": edge.get("geometry_coords") or [],
                "edge_source": edge.get("edge_source"),
                "is_virtual_connector": bool(edge.get("is_virtual_connector")),
                "reversed_direction": bool(edge.get("reversed_direction")),
            }
        )
        node_path.append(prev_node_hash)
        current_node_hash = prev_node_hash

    node_path.reverse()
    edge_chain = list(reversed(edge_chain_reversed))

    merged_coords = merge_coordinate_sequences(
        [edge.get("geometry_coords") or [] for edge in edge_chain]
    )
    if len(merged_coords) < 2:
        start_node = node_coords.get(start_node_hash)
        end_node = node_coords.get(end_node_hash)
        if start_node and end_node:
            merged_coords = [
                [start_node["lon"], start_node["lat"]],
                [end_node["lon"], end_node["lat"]],
            ]

    result = {
        "distance_km": float(distances[end_node_hash]),
        "node_path": node_path,
        "coordinates": merged_coords,
        "edge_chain": edge_chain,
        "hop_count": max(0, len(node_path) - 1),
    }
    path_cache[cache_key] = result
    return result


def build_topology_node_catalog(network: dict[str, Any]) -> list[dict[str, Any]]:
    cached = network.get("_topology_node_catalog")
    if cached is not None:
        return cached

    node_coords = network.get("node_coords") or {}
    result: list[dict[str, Any]] = []

    for node_hash, coord in node_coords.items():
        result.append(
            {
                "node_hash": node_hash,
                "lon": float(coord["lon"]),
                "lat": float(coord["lat"]),
            }
        )

    network["_topology_node_catalog"] = result
    return result

def build_connected_components_cache(network: dict[str, Any]) -> dict[str, Any]:
    cached = network.get("_connected_components_cache")
    if cached is not None:
        return cached

    adjacency = network.get("adjacency") or {}
    node_coords = network.get("node_coords") or {}

    component_id_by_node: dict[str, int] = {}
    component_nodes: dict[int, list[dict[str, Any]]] = defaultdict(list)
    component_sizes: dict[int, int] = {}

    component_id = 0

    for start_node_hash in node_coords.keys():
        if start_node_hash in component_id_by_node:
            continue

        component_id += 1
        stack = [start_node_hash]
        size = 0

        while stack:
            node_hash = stack.pop()
            if node_hash in component_id_by_node:
                continue

            component_id_by_node[node_hash] = component_id
            coord = node_coords.get(node_hash)
            if coord is not None:
                component_nodes[component_id].append(
                    {
                        "node_hash": node_hash,
                        "lon": float(coord["lon"]),
                        "lat": float(coord["lat"]),
                    }
                )

            size += 1

            for edge in adjacency.get(node_hash, []):
                next_node_hash = str(edge["to_node_hash"])
                if next_node_hash not in component_id_by_node:
                    stack.append(next_node_hash)

        component_sizes[component_id] = size

    result = {
        "component_id_by_node": component_id_by_node,
        "component_nodes": dict(component_nodes),
        "component_sizes": component_sizes,
    }
    network["_connected_components_cache"] = result
    return result


def annotate_link_options_with_components(
    options: list[dict[str, Any]],
    network: dict[str, Any],
) -> list[dict[str, Any]]:
    components_cache = build_connected_components_cache(network)
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


def _component_bridge_cache_key(component_a: int, component_b: int) -> tuple[int, int]:
    if component_a <= component_b:
        return (component_a, component_b)
    return (component_b, component_a)


def find_component_bridge_candidates(
    network: dict[str, Any],
    component_a: int,
    component_b: int,
) -> list[dict[str, Any]]:
    cache = network.setdefault("_component_bridge_pairs_cache", {})
    cache_key = _component_bridge_cache_key(component_a, component_b)

    cached = cache.get(cache_key)
    if cached is not None:
        result = list(cached)
        if component_a <= component_b:
            return result

        reversed_result: list[dict[str, Any]] = []
        for item in result:
            reversed_result.append(
                {
                    "from_component_id": component_a,
                    "to_component_id": component_b,
                    "from_node_hash": item["to_node_hash"],
                    "to_node_hash": item["from_node_hash"],
                    "from_lon": item["to_lon"],
                    "from_lat": item["to_lat"],
                    "to_lon": item["from_lon"],
                    "to_lat": item["from_lat"],
                    "gap_km": item["gap_km"],
                }
            )
        return reversed_result

    components_cache = build_connected_components_cache(network)
    component_nodes = components_cache["component_nodes"]
    component_sizes = components_cache["component_sizes"]

    nodes_a = component_nodes.get(component_a) or []
    nodes_b = component_nodes.get(component_b) or []
    if not nodes_a or not nodes_b:
        cache[cache_key] = []
        return []

    size_a = component_sizes.get(component_a, len(nodes_a))
    size_b = component_sizes.get(component_b, len(nodes_b))

    if size_a <= size_b:
        small_nodes = nodes_a
        large_nodes = nodes_b
        small_component_id = component_a
        large_component_id = component_b
    else:
        small_nodes = nodes_b
        large_nodes = nodes_a
        small_component_id = component_b
        large_component_id = component_a

    best_pairs: list[dict[str, Any]] = []

    for small_node in small_nodes:
        for large_node in large_nodes:
            gap_km = haversine_km(
                float(small_node["lon"]),
                float(small_node["lat"]),
                float(large_node["lon"]),
                float(large_node["lat"]),
            )
            if gap_km > COMPONENT_BRIDGE_MAX_GAP_KM:
                continue

            if small_component_id == component_a:
                pair_item = {
                    "from_component_id": component_a,
                    "to_component_id": component_b,
                    "from_node_hash": str(small_node["node_hash"]),
                    "to_node_hash": str(large_node["node_hash"]),
                    "from_lon": float(small_node["lon"]),
                    "from_lat": float(small_node["lat"]),
                    "to_lon": float(large_node["lon"]),
                    "to_lat": float(large_node["lat"]),
                    "gap_km": gap_km,
                }
            else:
                pair_item = {
                    "from_component_id": component_a,
                    "to_component_id": component_b,
                    "from_node_hash": str(large_node["node_hash"]),
                    "to_node_hash": str(small_node["node_hash"]),
                    "from_lon": float(large_node["lon"]),
                    "from_lat": float(large_node["lat"]),
                    "to_lon": float(small_node["lon"]),
                    "to_lat": float(small_node["lat"]),
                    "gap_km": gap_km,
                }

            best_pairs.append(pair_item)

    best_pairs.sort(
        key=lambda item: (
            float(item["gap_km"]),
            item["from_node_hash"],
            item["to_node_hash"],
        )
    )
    best_pairs = best_pairs[:COMPONENT_BRIDGE_PAIR_LIMIT]

    if cache_key == (component_a, component_b):
        cache[cache_key] = list(best_pairs)
        return best_pairs

    reversed_for_cache: list[dict[str, Any]] = []
    for item in best_pairs:
        reversed_for_cache.append(
            {
                "from_component_id": component_b,
                "to_component_id": component_a,
                "from_node_hash": item["to_node_hash"],
                "to_node_hash": item["from_node_hash"],
                "from_lon": item["to_lon"],
                "from_lat": item["to_lat"],
                "to_lon": item["from_lon"],
                "to_lat": item["from_lat"],
                "gap_km": item["gap_km"],
            }
        )
    cache[cache_key] = reversed_for_cache

    return best_pairs


def try_isolated_component_bridge_rescue(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    all_start_links: list[dict[str, Any]],
    all_end_links: list[dict[str, Any]],
) -> dict[str, Any] | None:
    components_cache = build_connected_components_cache(network)
    component_sizes = components_cache["component_sizes"]
    node_coords = network["node_coords"]
    adjacency = network["adjacency"]

    annotated_start_links = annotate_link_options_with_components(all_start_links, network)
    annotated_end_links = annotate_link_options_with_components(all_end_links, network)

    start_components = {
        int(item["component_id"])
        for item in annotated_start_links
        if item.get("component_id") is not None
    }
    end_components = {
        int(item["component_id"])
        for item in annotated_end_links
        if item.get("component_id") is not None
    }

    if not start_components or not end_components:
        return None

    if start_components & end_components:
        return None

    component_pairs: list[tuple[int, int]] = []

    for start_component_id in start_components:
        for end_component_id in end_components:
            start_size = int(component_sizes.get(start_component_id, 0))
            end_size = int(component_sizes.get(end_component_id, 0))

            if min(start_size, end_size) > COMPONENT_BRIDGE_SMALL_COMPONENT_MAX_SIZE:
                continue

            component_pairs.append((start_component_id, end_component_id))

    if not component_pairs:
        return None

    best_result: dict[str, Any] | None = None
    best_score = math.inf

    for start_component_id, end_component_id in component_pairs:
        bridge_candidates = find_component_bridge_candidates(
            network,
            start_component_id,
            end_component_id,
        )
        if not bridge_candidates:
            continue

        eligible_start_links = [
            item for item in annotated_start_links
            if item.get("component_id") == start_component_id
        ]
        eligible_end_links = [
            item for item in annotated_end_links
            if item.get("component_id") == end_component_id
        ]

        if not eligible_start_links or not eligible_end_links:
            continue

        for bridge in bridge_candidates:
            for start_link in eligible_start_links:
                path_before_bridge = dijkstra_topology_path(
                    adjacency=adjacency,
                    node_coords=node_coords,
                    start_node_hash=str(start_link["node_hash"]),
                    end_node_hash=str(bridge["from_node_hash"]),
                    path_cache=path_cache,
                )
                if path_before_bridge is None:
                    continue

                for end_link in eligible_end_links:
                    path_after_bridge = dijkstra_topology_path(
                        adjacency=adjacency,
                        node_coords=node_coords,
                        start_node_hash=str(bridge["to_node_hash"]),
                        end_node_hash=str(end_link["node_hash"]),
                        path_cache=path_cache,
                    )
                    if path_after_bridge is None:
                        continue

                    render_total_distance_km = (
                        float(start_link["link_distance_km"])
                        + float(path_before_bridge["distance_km"])
                        + float(bridge["gap_km"])
                        + float(path_after_bridge["distance_km"])
                        + float(end_link["link_distance_km"])
                    )

                    outlier_penalty, outlier_diag = compute_topology_path_outlier_penalty(
                        previous_stop=previous_stop,
                        current_stop=current_stop,
                        previous_candidate=previous_candidate,
                        current_candidate=current_candidate,
                        render_total_distance_km=render_total_distance_km,
                    )

                    transition_cost, transition_diag = compute_transition_cost(
                        previous_stop=previous_stop,
                        next_stop=current_stop,
                        render_total_distance_km=render_total_distance_km,
                        hop_count=(
                            int(path_before_bridge.get("hop_count") or 0)
                            + int(path_after_bridge.get("hop_count") or 0)
                            + 1
                        ),
                    )
                    if transition_cost is None:
                        continue

                    connector_penalty = (
                        float(start_link["link_distance_km"])
                        + float(end_link["link_distance_km"])
                    ) * TOPOLOGY_LINK_CONNECTOR_SCORE_WEIGHT

                    source_penalty = 0.0
                    if start_link.get("source") != "station_link":
                        source_penalty += COMPONENT_BRIDGE_SOURCE_PENALTY
                    if end_link.get("source") != "station_link":
                        source_penalty += COMPONENT_BRIDGE_SOURCE_PENALTY

                    bridge_penalty = (
                        COMPONENT_BRIDGE_EXTRA_PENALTY
                        + float(bridge["gap_km"]) * COMPONENT_BRIDGE_GAP_SCORE_WEIGHT
                    )

                    final_score = (
                        float(transition_cost) * TRANSITION_DISTANCE_SCORE_WEIGHT
                        + outlier_penalty
                        + connector_penalty
                        + source_penalty
                        + bridge_penalty
                    )

                    merged_coords = merge_coordinate_sequences(
                        [
                            [[previous_candidate.lon, previous_candidate.lat], [float(start_link["node_lon"]), float(start_link["node_lat"])] ]
                            if [previous_candidate.lon, previous_candidate.lat] != [float(start_link["node_lon"]), float(start_link["node_lat"])]
                            else [],
                            path_before_bridge.get("coordinates") or [],
                            [
                                [float(bridge["from_lon"]), float(bridge["from_lat"])],
                                [float(bridge["to_lon"]), float(bridge["to_lat"])],
                            ],
                            path_after_bridge.get("coordinates") or [],
                            [[float(end_link["node_lon"]), float(end_link["node_lat"])], [current_candidate.lon, current_candidate.lat]]
                            if [float(end_link["node_lon"]), float(end_link["node_lat"])] != [current_candidate.lon, current_candidate.lat]
                            else [],
                        ]
                    )

                    if len(merged_coords) < 2:
                        continue

                    if final_score < best_score:
                        best_score = final_score
                        best_result = {
                            "render_method": "topology_component_bridge",
                            "search_mode": "isolated_component_bridge_last_resort",
                            "start_link": start_link,
                            "end_link": end_link,
                            "bridge": bridge,
                            "coordinates": merged_coords,
                            "graph_distance_km": (
                                float(path_before_bridge["distance_km"])
                                + float(path_after_bridge["distance_km"])
                                + float(bridge["gap_km"])
                            ),
                            "connector_start_km": float(start_link["link_distance_km"]),
                            "connector_end_km": float(end_link["link_distance_km"]),
                            "bridge_gap_km": float(bridge["gap_km"]),
                            "total_score_km": render_total_distance_km,
                            "graph_edge_count": (
                                len(path_before_bridge.get("edge_chain") or [])
                                + len(path_after_bridge.get("edge_chain") or [])
                                + 1
                            ),
                            "edge_groups": [
                                {
                                    "kind": "graph_path",
                                    "edge_chain": path_before_bridge.get("edge_chain") or [],
                                },
                                {
                                    "kind": "component_bridge",
                                    "geometry_coords": [
                                        [float(bridge["from_lon"]), float(bridge["from_lat"])],
                                        [float(bridge["to_lon"]), float(bridge["to_lat"])],
                                    ],
                                    "length_km": float(bridge["gap_km"]),
                                },
                                {
                                    "kind": "graph_path",
                                    "edge_chain": path_after_bridge.get("edge_chain") or [],
                                },
                            ],
                            "transition_diag": {
                                **transition_diag,
                                "connector_start_km": float(start_link["link_distance_km"]),
                                "connector_end_km": float(end_link["link_distance_km"]),
                                "bridge_gap_km": float(bridge["gap_km"]),
                                "render_total_distance_km": render_total_distance_km,
                                "outlier_diag": outlier_diag,
                                "bridge_from_component_id": bridge["from_component_id"],
                                "bridge_to_component_id": bridge["to_component_id"],
                            },
                        }

    return best_result



def _normalize_link_options(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    for item in options:
        normalized.append(
            {
                "node_hash": str(item["node_hash"]),
                "link_distance_km": float(item["link_distance_km"]),
                "is_primary": bool(item.get("is_primary")),
                "node_lon": float(item["node_lon"]),
                "node_lat": float(item["node_lat"]),
                "source": item.get("source") or "station_link",
            }
        )

    normalized.sort(
        key=lambda x: (
            _link_source_rank(x.get("source")),
            0 if x["is_primary"] else 1,
            x["link_distance_km"],
            x["node_hash"],
        )
    )
    return normalized


def get_station_link_options_for_candidate(
    candidate: Candidate,
    network: dict[str, Any],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    station_links = network.get("station_links") or {}

    direct_links = station_links.get(candidate.station_id) or []
    direct_options = _normalize_link_options(direct_links)

    cached_nearest = fallback_node_cache.get(candidate.station_id)
    if cached_nearest is not None:
        nearest_options = cached_nearest
    else:
        node_catalog = build_topology_node_catalog(network)

        nearest_nodes: list[dict[str, Any]] = []

        for node in node_catalog:
            distance_km = haversine_km(
                candidate.lon,
                candidate.lat,
                float(node["lon"]),
                float(node["lat"]),
            )

            if distance_km > TOPOLOGY_AUGMENT_NODE_MAX_DISTANCE_KM:
                continue

            nearest_nodes.append(
                {
                    "node_hash": node["node_hash"],
                    "link_distance_km": distance_km,
                    "is_primary": False,
                    "node_lon": float(node["lon"]),
                    "node_lat": float(node["lat"]),
                    "source": "fallback_nearest_node",
                }
            )

        nearest_nodes.sort(key=lambda item: (item["link_distance_km"], item["node_hash"]))

        nearest_options = _normalize_link_options(
            nearest_nodes[:TOPOLOGY_AUGMENT_NODE_NEIGHBORS]
        )

        fallback_node_cache[candidate.station_id] = nearest_options

    merged_by_node: dict[str, dict[str, Any]] = {}

    for item in nearest_options + direct_options:
        node_hash = str(item["node_hash"])
        existing = merged_by_node.get(node_hash)

        if existing is None:
            merged_by_node[node_hash] = item
            continue

        if _link_priority(item) < _link_priority(existing):
            merged_by_node[node_hash] = item

    merged = list(merged_by_node.values())
    merged.sort(key=_link_priority)

    return merged[:TOPOLOGY_LINK_OPTIONS_FINAL_LIMIT]


def get_rzd_delta_km(
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
) -> float | None:
    previous_distance = safe_float(previous_stop.get("distance_km"))
    current_distance = safe_float(current_stop.get("distance_km"))

    if previous_distance is None or current_distance is None:
        return None

    return max(0.0, current_distance - previous_distance)


def compute_topology_path_outlier_penalty(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    render_total_distance_km: float,
) -> tuple[float, dict[str, Any]]:
    geo_distance_km = haversine_km(
        previous_candidate.lon,
        previous_candidate.lat,
        current_candidate.lon,
        current_candidate.lat,
    )

    rzd_delta_km = get_rzd_delta_km(previous_stop, current_stop)

    penalty = 0.0
    details: dict[str, Any] = {
        "geo_distance_km": round(geo_distance_km, 3),
        "rzd_delta_km": round(rzd_delta_km, 3) if rzd_delta_km is not None else None,
        "render_total_distance_km": round(render_total_distance_km, 3),
        "outlier_penalty": 0.0,
        "outlier_reason": None,
    }

    if rzd_delta_km is not None and rzd_delta_km >= 1.0:
        distance_error = abs(render_total_distance_km - rzd_delta_km)
        relative_error = distance_error / max(rzd_delta_km, 1.0)

        penalty += distance_error * 0.65
        penalty += relative_error * 28.0

        if render_total_distance_km > max(rzd_delta_km * 2.5, rzd_delta_km + 55.0):
            penalty += 80.0
            details["outlier_reason"] = "longer_than_rzd_delta"

    elif geo_distance_km >= 5.0:
        distance_error = max(0.0, render_total_distance_km - geo_distance_km)
        relative_error = distance_error / max(geo_distance_km, 1.0)

        penalty += distance_error * 0.25
        penalty += relative_error * 12.0

        if render_total_distance_km > max(geo_distance_km * 4.0, geo_distance_km + 120.0):
            penalty += 60.0
            details["outlier_reason"] = "longer_than_geo_distance"

    details["outlier_penalty"] = round(penalty, 3)
    return penalty, details


def compute_transition_cost(
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

    if delta_rzd >= 5.0:
        max_reasonable_graph_distance = max(
            delta_rzd * ABSURD_PATH_MAX_RZD_RATIO,
            delta_rzd + ABSURD_PATH_MAX_RZD_EXTRA_KM,
        )

        if graph_distance > max_reasonable_graph_distance:
            return None, {
                "delta_rzd_km": delta_rzd,
                "graph_distance_km": graph_distance,
                "distance_error_km": abs(graph_distance - delta_rzd),
                "relative_error": abs(graph_distance - delta_rzd) / max(delta_rzd, 1.0),
                "hop_count": hop_count,
                "rejected_reason": "graph_path_absurdly_long_for_rzd_delta",
                "max_reasonable_graph_distance_km": max_reasonable_graph_distance,
            }

    distance_error = abs(graph_distance - delta_rzd)
    relative_error = distance_error / max(delta_rzd, 10.0)

    cost = (
        distance_error * 0.45
        + relative_error * 22.0
        + max(0, hop_count - 1) * 0.04
    )

    return cost, {
        "delta_rzd_km": delta_rzd,
        "graph_distance_km": graph_distance,
        "distance_error_km": distance_error,
        "relative_error": relative_error,
        "hop_count": hop_count,
    }



def candidate_name_similarity_for_stop(stop: dict[str, Any], candidate: Candidate) -> float:
    return compute_name_similarity(stop.get("station_name_raw"), candidate.name)


def should_trust_stored_visible_candidate(
    stop: dict[str, Any],
    candidate: Candidate,
    candidates: list[Candidate],
) -> bool:
    stored_name_score = candidate_name_similarity_for_stop(stop, candidate)
    if stored_name_score >= LOCKED_STORED_NAME_SCORE_STRICT:
        return True

    if stored_name_score < LOCKED_STORED_NAME_SCORE_MIN:
        return False

    exact_code_candidates = [item for item in candidates if item.code_match]
    if exact_code_candidates:
        best_exact = max(
            exact_code_candidates,
            key=lambda item: (
                candidate_name_similarity_for_stop(stop, item),
                item.effective_score,
                item.name_score,
                -item.station_id,
            ),
        )
        best_exact_score = candidate_name_similarity_for_stop(stop, best_exact)
        if best_exact_score >= stored_name_score + LOCKED_EXACT_CODE_PRIORITY_DELTA:
            return False

    return True


def choose_locked_candidate_for_stop(
    stop: dict[str, Any],
    candidates: list[Candidate],
) -> tuple[Candidate | None, str]:
    if not candidates:
        return None, "no_candidates"

    stored_station_id = stop.get("stored_station_id")
    stored_station_visible = bool(stop.get("stored_station_visible"))

    if stored_station_id is not None and stored_station_visible:
        for candidate in candidates:
            if int(candidate.station_id) == int(stored_station_id):
                if should_trust_stored_visible_candidate(stop, candidate, candidates):
                    return candidate, "lock_trusted_stored_visible_station"
                break

    exact_code_candidates = [candidate for candidate in candidates if candidate.code_match]
    if exact_code_candidates:
        best_exact = max(
            exact_code_candidates,
            key=lambda item: (
                candidate_name_similarity_for_stop(stop, item),
                item.effective_score,
                item.name_score,
                item.anchor,
                -item.station_id,
            ),
        )
        return best_exact, "lock_exact_code_candidate"

    anchor_candidates = [candidate for candidate in candidates if candidate.anchor]
    if anchor_candidates:
        best_anchor = max(
            anchor_candidates,
            key=lambda item: (
                candidate_name_similarity_for_stop(stop, item),
                item.effective_score,
                item.name_score,
                -item.station_id,
            ),
        )
        return best_anchor, "lock_anchor_candidate"

    best_name_candidate = max(
        candidates,
        key=lambda item: (
            candidate_name_similarity_for_stop(stop, item),
            item.name_score,
            item.effective_score,
            item.code_match,
            item.anchor,
            -item.station_id,
        ),
    )
    return best_name_candidate, "lock_best_available_candidate_fallback"


def compute_lock_candidate_cost(
    stop: dict[str, Any],
    candidate: Candidate,
) -> float:
    name_score = candidate_name_similarity_for_stop(stop, candidate)
    stored_station_id = stop.get("stored_station_id")
    stored_station_visible = bool(stop.get("stored_station_visible"))

    cost = 0.0

    cost += max(0.0, 1.0 - candidate.effective_score) * 8.0
    cost += max(0.0, 0.85 - name_score) * 10.0

    if candidate.code_match:
        cost -= 5.0
    if candidate.anchor:
        cost -= 2.0

    if stored_station_id is not None and stored_station_visible:
        if int(candidate.station_id) == int(stored_station_id):
            if should_trust_stored_visible_candidate(stop, candidate, [candidate]):
                cost -= 4.0
            else:
                cost += 8.0
        elif name_score < LOCKED_STORED_NAME_SCORE_MIN and not candidate.code_match:
            cost += 2.0

    if candidate.region_code is None:
        cost += 1.0

    return cost


def compute_lock_transition_cost(
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
) -> tuple[float | None, dict[str, Any]]:
    delta_rzd = None
    previous_distance = safe_float(previous_stop.get("distance_km"))
    current_distance = safe_float(current_stop.get("distance_km"))
    if previous_distance is not None and current_distance is not None:
        delta_rzd = max(0.0, current_distance - previous_distance)

    geo_distance_km = haversine_km(
        previous_candidate.lon,
        previous_candidate.lat,
        current_candidate.lon,
        current_candidate.lat,
    )

    if delta_rzd is None:
        cost = geo_distance_km * 0.02
        if previous_candidate.region_code and current_candidate.region_code:
            if previous_candidate.region_code != current_candidate.region_code:
                cost += 0.5
        return cost, {
            "delta_rzd_km": None,
            "geo_distance_km": geo_distance_km,
            "distance_error_km": None,
            "relative_error": None,
        }

    if delta_rzd <= 1.0:
        if geo_distance_km > 5.0:
            return None, {
                "delta_rzd_km": delta_rzd,
                "geo_distance_km": geo_distance_km,
                "distance_error_km": abs(geo_distance_km - delta_rzd),
                "relative_error": None,
                "rejected_reason": "tiny_rzd_delta_but_geo_far",
            }
        return geo_distance_km * 0.1, {
            "delta_rzd_km": delta_rzd,
            "geo_distance_km": geo_distance_km,
            "distance_error_km": abs(geo_distance_km - delta_rzd),
            "relative_error": None,
        }

    if geo_distance_km > delta_rzd * 1.35 + 35.0:
        return None, {
            "delta_rzd_km": delta_rzd,
            "geo_distance_km": geo_distance_km,
            "distance_error_km": abs(geo_distance_km - delta_rzd),
            "relative_error": abs(geo_distance_km - delta_rzd) / max(delta_rzd, 1.0),
            "rejected_reason": "geo_distance_absurd_for_rzd_delta",
        }

    if geo_distance_km > ROUTE_LOCK_BIG_DISTANCE_REJECTION_KM and delta_rzd < geo_distance_km * 0.35:
        return None, {
            "delta_rzd_km": delta_rzd,
            "geo_distance_km": geo_distance_km,
            "distance_error_km": abs(geo_distance_km - delta_rzd),
            "relative_error": abs(geo_distance_km - delta_rzd) / max(delta_rzd, 1.0),
            "rejected_reason": "candidate_far_away_from_route_logic",
        }

    ratio = geo_distance_km / max(delta_rzd, 1.0)
    cost = 0.0

    if ratio > 1.0:
        cost += (ratio - 1.0) * 18.0
    elif ratio < 0.15:
        cost += (0.15 - ratio) * 35.0
    elif ratio < 0.30:
        cost += (0.30 - ratio) * 10.0

    if previous_candidate.region_code and current_candidate.region_code:
        if previous_candidate.region_code != current_candidate.region_code:
            cost += 0.4

    return cost, {
        "delta_rzd_km": delta_rzd,
        "geo_distance_km": geo_distance_km,
        "distance_error_km": abs(geo_distance_km - delta_rzd),
        "relative_error": abs(geo_distance_km - delta_rzd) / max(delta_rzd, 1.0),
    }


def lock_route_stop_candidates(
    stops: list[dict[str, Any]],
    candidates_per_stop: list[list[Candidate]],
    *,
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
) -> tuple[list[Candidate | None], list[dict[str, Any]]]:
    logger_context = logger_context or {}

    if not stops:
        return [], []

    states: list[dict[int, dict[str, Any]]] = []
    lock_logs: list[dict[str, Any]] = []

    first_state: dict[int, dict[str, Any]] = {}
    first_candidates = candidates_per_stop[0] if candidates_per_stop else []

    for candidate_index, candidate in enumerate(first_candidates):
        first_state[candidate_index] = {
            "candidate": candidate,
            "cost": compute_lock_candidate_cost(stops[0], candidate),
            "prev_index": None,
        }

    if not first_state:
        if diagnostics is not None:
            diagnostics["locked_stop_candidates"] = []
        return [None for _ in stops], []

    states.append(first_state)

    for stop_index in range(1, len(stops)):
        current_candidates = candidates_per_stop[stop_index]
        previous_state = states[-1]
        current_state: dict[int, dict[str, Any]] = {}

        for current_candidate_index, current_candidate in enumerate(current_candidates):
            current_candidate_cost = compute_lock_candidate_cost(stops[stop_index], current_candidate)

            best_total_cost = math.inf
            best_prev_index: int | None = None

            for previous_candidate_index, previous_payload in previous_state.items():
                previous_candidate = previous_payload["candidate"]

                transition_cost, _transition_diag = compute_lock_transition_cost(
                    stops[stop_index - 1],
                    stops[stop_index],
                    previous_candidate,
                    current_candidate,
                )
                if transition_cost is None:
                    continue

                total_cost = float(previous_payload["cost"]) + current_candidate_cost + float(transition_cost)
                if total_cost < best_total_cost:
                    best_total_cost = total_cost
                    best_prev_index = previous_candidate_index

            if best_prev_index is not None:
                current_state[current_candidate_index] = {
                    "candidate": current_candidate,
                    "cost": best_total_cost,
                    "prev_index": best_prev_index,
                }

        if not current_state:
            fallback_selected: list[Candidate | None] = []
            for stop, candidates in zip(stops, candidates_per_stop):
                selected_candidate, _reason = choose_locked_candidate_for_stop(stop, candidates)
                fallback_selected.append(selected_candidate)

            for stop, selected_candidate in zip(stops, fallback_selected):
                log_item = {
                    "stop_sequence": stop.get("stop_sequence"),
                    "station_name_raw": stop.get("station_name_raw"),
                    "station_code_rzd": stop.get("station_code_rzd"),
                    "stored_station_id": stop.get("stored_station_id"),
                    "locked_station_id": selected_candidate.station_id if selected_candidate else None,
                    "locked_station_name": selected_candidate.name if selected_candidate else None,
                    "locked_station_region_code": selected_candidate.region_code if selected_candidate else None,
                    "locked_match_method": selected_candidate.match_method if selected_candidate else None,
                    "locked_score": round(selected_candidate.effective_score, 4) if selected_candidate else None,
                    "lock_reason": "fallback_per_stop_lock_after_route_dp_failed",
                }
                lock_logs.append(log_item)

            if diagnostics is not None:
                diagnostics["locked_stop_candidates"] = lock_logs
                diagnostics.setdefault("solver_notes", [])
                diagnostics["solver_notes"].append("route_lock_dp_failed_fallback_to_per_stop")

            return fallback_selected, lock_logs

        states.append(current_state)

    last_state = states[-1]
    best_last_index, best_last_payload = min(last_state.items(), key=lambda item: item[1]["cost"])

    selected_candidates_reversed: list[Candidate | None] = []
    current_index = best_last_index

    for state_index in range(len(states) - 1, -1, -1):
        payload = states[state_index][current_index]
        selected_candidates_reversed.append(payload["candidate"])
        prev_index = payload["prev_index"]
        if prev_index is None:
            break
        current_index = prev_index

    locked_candidates = list(reversed(selected_candidates_reversed))

    if len(locked_candidates) != len(stops):
        fallback_selected: list[Candidate | None] = []
        for stop, candidates in zip(stops, candidates_per_stop):
            selected_candidate, _reason = choose_locked_candidate_for_stop(stop, candidates)
            fallback_selected.append(selected_candidate)

        for stop, selected_candidate in zip(stops, fallback_selected):
            log_item = {
                "stop_sequence": stop.get("stop_sequence"),
                "station_name_raw": stop.get("station_name_raw"),
                "station_code_rzd": stop.get("station_code_rzd"),
                "stored_station_id": stop.get("stored_station_id"),
                "locked_station_id": selected_candidate.station_id if selected_candidate else None,
                "locked_station_name": selected_candidate.name if selected_candidate else None,
                "locked_station_region_code": selected_candidate.region_code if selected_candidate else None,
                "locked_match_method": selected_candidate.match_method if selected_candidate else None,
                "locked_score": round(selected_candidate.effective_score, 4) if selected_candidate else None,
                "lock_reason": "fallback_per_stop_lock_after_reconstruction_failed",
            }
            lock_logs.append(log_item)

        if diagnostics is not None:
            diagnostics["locked_stop_candidates"] = lock_logs
            diagnostics.setdefault("solver_notes", [])
            diagnostics["solver_notes"].append("route_lock_reconstruction_failed_fallback_to_per_stop")

        return fallback_selected, lock_logs

    for stop, selected_candidate in zip(stops, locked_candidates):
        log_item = {
            "stop_sequence": stop.get("stop_sequence"),
            "station_name_raw": stop.get("station_name_raw"),
            "station_code_rzd": stop.get("station_code_rzd"),
            "stored_station_id": stop.get("stored_station_id"),
            "locked_station_id": selected_candidate.station_id if selected_candidate else None,
            "locked_station_name": selected_candidate.name if selected_candidate else None,
            "locked_station_region_code": selected_candidate.region_code if selected_candidate else None,
            "locked_match_method": selected_candidate.match_method if selected_candidate else None,
            "locked_score": round(selected_candidate.effective_score, 4) if selected_candidate else None,
            "lock_reason": "route_distance_first_dp_lock",
        }
        lock_logs.append(log_item)

        log_event(
            "info",
            "route_stop_candidate_locked",
            **log_item,
            **logger_context,
        )

    if diagnostics is not None:
        diagnostics["locked_stop_candidates"] = lock_logs

    return locked_candidates, lock_logs



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

            outlier_penalty, outlier_diag = compute_topology_path_outlier_penalty(
                previous_stop=previous_stop,
                current_stop=current_stop,
                previous_candidate=previous_candidate,
                current_candidate=current_candidate,
                render_total_distance_km=render_total_distance_km,
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
            ) * TOPOLOGY_LINK_CONNECTOR_SCORE_WEIGHT

            source_penalty = 0.0

            if start_link.get("source") != "station_link":
                source_penalty += TOPOLOGY_FALLBACK_LINK_SCORE_PENALTY
            elif not start_link.get("is_primary"):
                source_penalty += TOPOLOGY_NON_PRIMARY_LINK_SCORE_PENALTY

            if end_link.get("source") != "station_link":
                source_penalty += TOPOLOGY_FALLBACK_LINK_SCORE_PENALTY
            elif not end_link.get("is_primary"):
                source_penalty += TOPOLOGY_NON_PRIMARY_LINK_SCORE_PENALTY

            final_score = (
                float(transition_cost) * TRANSITION_DISTANCE_SCORE_WEIGHT
                + outlier_penalty
                + connector_penalty
                + source_penalty
            )

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
                        "outlier_diag": outlier_diag,
                    },
                    "final_score": final_score,
                }

    return best_option



def _topology_result_source_rank(source: str | None) -> int:
    source = source or ""

    if source == "station_link":
        return 0
    if source == "fallback_nearest_node":
        return 1

    return 9


def choose_best_topology_path_result(
    results: list[dict[str, Any] | None],
) -> dict[str, Any] | None:
    valid_results = [item for item in results if item is not None]
    if not valid_results:
        return None

    def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        start_link = item.get("start_link") or {}
        end_link = item.get("end_link") or {}

        connector_start_km = float(item.get("connector_start_km") or 0.0)
        connector_end_km = float(item.get("connector_end_km") or 0.0)
        total_connector_km = connector_start_km + connector_end_km
        max_connector_km = max(connector_start_km, connector_end_km)

        source_rank_sum = (
            _topology_result_source_rank(start_link.get("source"))
            + _topology_result_source_rank(end_link.get("source"))
        )

        return (
            float(item.get("final_score") or 999999.0),
            max_connector_km,
            total_connector_km,
            source_rank_sum,
            float(item.get("graph_distance_km") or 999999.0),
            int(item.get("graph_edge_count") or 999999),
        )

    return min(valid_results, key=sort_key)


def build_topology_path_between_candidates(
    *,
    previous_stop: dict[str, Any],
    current_stop: dict[str, Any],
    previous_candidate: Candidate,
    current_candidate: Candidate,
    network: dict[str, Any],
    path_cache: dict[tuple[str, str], dict[str, Any] | None],
    fallback_node_cache: dict[int, list[dict[str, Any]]],
    trace_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    trace_context = trace_context or {}

    route_id = trace_context.get("route_id")
    segment_index = trace_context.get("segment_index")

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

    candidate_results: list[dict[str, Any]] = []

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
        candidate_results.append(direct_result)

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
        candidate_results.append(bridge_result)

    if not candidate_results:
        matcher_trace(
            "segment_no_path",
            {
                "route_id": route_id,
                "segment_index": segment_index,
                "from_stop_sequence": previous_stop.get("stop_sequence"),
                "to_stop_sequence": current_stop.get("stop_sequence"),
                "from_station_id": previous_candidate.station_id,
                "from_station_name": previous_candidate.name,
                "to_station_id": current_candidate.station_id,
                "to_station_name": current_candidate.name,
                "start_links": [
                    {
                        "node_hash": item.get("node_hash"),
                        "source": item.get("source"),
                        "is_primary": item.get("is_primary"),
                        "link_distance_km": round(float(item.get("link_distance_km") or 0), 4),
                    }
                    for item in start_links[:12]
                ],
                "end_links": [
                    {
                        "node_hash": item.get("node_hash"),
                        "source": item.get("source"),
                        "is_primary": item.get("is_primary"),
                        "link_distance_km": round(float(item.get("link_distance_km") or 0), 4),
                    }
                    for item in end_links[:12]
                ],
            },
            route_id=route_id,
        )
        return None

    def result_sort_key(item: dict[str, Any]) -> tuple[float, float, float, int]:
        connector_start_km = float(item.get("connector_start_km") or 0.0)
        connector_end_km = float(item.get("connector_end_km") or 0.0)

        return (
            float(item.get("final_score") or 999999.0),
            float(item.get("total_score_km") or 999999.0),
            connector_start_km + connector_end_km,
            int(item.get("graph_edge_count") or 999999),
        )

    best_result = min(candidate_results, key=result_sort_key)

    matcher_trace(
        "segment_path_choice",
        {
            "route_id": route_id,
            "segment_index": segment_index,
            "from_stop_sequence": previous_stop.get("stop_sequence"),
            "to_stop_sequence": current_stop.get("stop_sequence"),
            "from_station_id": previous_candidate.station_id,
            "from_station_name": previous_candidate.name,
            "to_station_id": current_candidate.station_id,
            "to_station_name": current_candidate.name,
            "chosen": {
                "render_method": best_result.get("render_method"),
                "search_mode": best_result.get("search_mode"),
                "final_score": round(float(best_result.get("final_score") or 0), 4),
                "graph_distance_km": round(float(best_result.get("graph_distance_km") or 0), 3),
                "total_score_km": round(float(best_result.get("total_score_km") or 0), 3),
                "connector_start_km": round(float(best_result.get("connector_start_km") or 0), 4),
                "connector_end_km": round(float(best_result.get("connector_end_km") or 0), 4),
                "bridge_gap_km": (
                    round(float(best_result.get("bridge_gap_km")), 4)
                    if best_result.get("bridge_gap_km") is not None
                    else None
                ),
                "graph_edge_count": best_result.get("graph_edge_count"),
                "from_entry_node_hash": (best_result.get("start_link") or {}).get("node_hash"),
                "to_entry_node_hash": (best_result.get("end_link") or {}).get("node_hash"),
                "from_entry_source": (best_result.get("start_link") or {}).get("source"),
                "to_entry_source": (best_result.get("end_link") or {}).get("source"),
                "cost_diag": best_result.get("transition_diag"),
            },
            "all_candidates": [
                {
                    "render_method": item.get("render_method"),
                    "search_mode": item.get("search_mode"),
                    "final_score": round(float(item.get("final_score") or 0), 4),
                    "graph_distance_km": round(float(item.get("graph_distance_km") or 0), 3),
                    "total_score_km": round(float(item.get("total_score_km") or 0), 3),
                    "connector_start_km": round(float(item.get("connector_start_km") or 0), 4),
                    "connector_end_km": round(float(item.get("connector_end_km") or 0), 4),
                    "bridge_gap_km": (
                        round(float(item.get("bridge_gap_km")), 4)
                        if item.get("bridge_gap_km") is not None
                        else None
                    ),
                    "graph_edge_count": item.get("graph_edge_count"),
                    "from_entry_node_hash": (item.get("start_link") or {}).get("node_hash"),
                    "to_entry_node_hash": (item.get("end_link") or {}).get("node_hash"),
                    "from_entry_source": (item.get("start_link") or {}).get("source"),
                    "to_entry_source": (item.get("end_link") or {}).get("source"),
                    "cost_diag": item.get("transition_diag"),
                }
                for item in sorted(candidate_results, key=result_sort_key)
            ],
        },
        route_id=route_id,
    )

    return best_result


def build_route_geometry_between_locked_candidates(
    stops: list[dict[str, Any]],
    locked_candidates: list[Candidate | None],
    candidates_per_stop: list[list[Candidate]],
    network: dict[str, Any],
    *,
    diagnostics: dict[str, Any] | None = None,
    logger_context: dict[str, Any] | None = None,
) -> tuple[
    dict[str, Any] | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    logger_context = logger_context or {}

    path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    fallback_node_cache: dict[int, list[dict[str, Any]]] = {}

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

        pair_path = build_topology_path_between_candidates(
            previous_stop=previous_stop,
            current_stop=current_stop,
            previous_candidate=previous_candidate,
            current_candidate=current_candidate,
            network=network,
            path_cache=path_cache,
            fallback_node_cache=fallback_node_cache,
            trace_context={
                "route_id": logger_context.get("route_id"),
                "segment_index": index,
                "from_stop_sequence": previous_stop.get("stop_sequence"),
                "to_stop_sequence": current_stop.get("stop_sequence"),
            },
        )

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
                            edge_geometry = build_simple_linestring(edge_coords)
                            if edge_geometry is None:
                                continue

                            edge_index += 1
                            network_segments.append(
                                {
                                    "segment_index": index,
                                    "edge_index": edge_index,
                                    "edge_id": edge.get("edge_id") or edge.get("id"),
                                    "id": edge.get("edge_id") or edge.get("id"),
                                    "from_node_hash": edge.get("from_node_hash"),
                                    "to_node_hash": edge.get("to_node_hash"),
                                    "length_km": edge.get("length_km"),
                                    "edge_source": edge.get("edge_source"),
                                    "is_virtual_connector": bool(edge.get("is_virtual_connector")),
                                    "reversed_direction": bool(edge.get("reversed_direction")),
                                    "segment_source": "graph_locked_station_path",
                                    "geometry": edge_geometry,
                                }
                            )

                    elif edge_group.get("kind") == "component_bridge":
                        edge_geometry = build_simple_linestring(edge_group.get("geometry_coords") or [])
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
                    edge_geometry = build_simple_linestring(edge_coords)
                    if edge_geometry is None:
                        continue

                    edge_index += 1
                    network_segments.append(
                        {
                            "segment_index": index,
                            "edge_index": edge_index,
                            "edge_id": edge.get("edge_id") or edge.get("id"),
                            "id": edge.get("edge_id") or edge.get("id"),
                            "from_node_hash": edge.get("from_node_hash"),
                            "to_node_hash": edge.get("to_node_hash"),
                            "length_km": edge.get("length_km"),
                            "edge_source": edge.get("edge_source"),
                            "is_virtual_connector": bool(edge.get("is_virtual_connector")),
                            "reversed_direction": bool(edge.get("reversed_direction")),
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
                "anchor_repair_applied": bool(pair_path.get("anchor_repair_applied")),
                "anchor_repair_summary": pair_path.get("anchor_repair_summary"),
            }
            transition_logs.append(transition_log)

            log_event(
                "info",
                "locked_station_segment_rendered_on_topology_graph",
                **transition_log,
                **logger_context,
            )
            continue

        if current_group:
            segment_coordinate_groups.append(current_group)
            current_group = []

        segment_items.append(
            {
                "segment_index": index,
                "from_station_id": previous_candidate.station_id,
                "to_station_id": current_candidate.station_id,
                "from_station_name": previous_candidate.name,
                "to_station_name": current_candidate.name,
                "geometry": None,
                "segment_source": "missing_graph_path",
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
            "segment_render_method": "missing_graph_path",
            "path_found": False,
            "fallback_used": False,
            "reason": "topology_graph_path_not_found_for_locked_stations",
        }
        transition_logs.append(transition_log)

        log_event(
            "warning",
            "locked_station_segment_missing_graph_path",
            **transition_log,
            **logger_context,
        )

    if current_group:
        segment_coordinate_groups.append(current_group)

    geometry = build_linestring_or_multilinestring(segment_coordinate_groups)

    if diagnostics is not None:
        diagnostics["transition_diagnostics"] = transition_logs
        diagnostics["locked_station_rendering"] = {
            "segments_count": len(transition_logs),
            "segments_with_graph_path": sum(1 for item in transition_logs if item.get("path_found")),
            "segments_with_fallback": sum(1 for item in transition_logs if item.get("fallback_used")),
        }

    return geometry, segment_items, network_segments, transition_logs

def build_fallback_geometry_from_selected_candidates(
    selected_candidates: list[Candidate | None],
) -> dict[str, Any] | None:
    coords: list[list[float]] = []

    for candidate in selected_candidates:
        if candidate is None:
            continue
        coords.append([candidate.lon, candidate.lat])

    return build_simple_linestring(coords)


def build_feature_collection(
    route: dict[str, Any],
    geometry: dict[str, Any] | None,
    geometry_source: str | None,
    network_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []

    if geometry is not None:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_kind": "route_path",
                    "route_id": route["id"],
                    "train_number": route.get("train_number"),
                    "route_name": route.get("route_name"),
                    "geometry_source": geometry_source,
                },
                "geometry": geometry,
            }
        )

    for segment in network_segments or []:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_kind": "route_network_segment",
                    "route_id": route["id"],
                    "segment_index": segment.get("segment_index"),
                    "edge_index": segment.get("edge_index"),
                    "edge_id": segment.get("edge_id") or segment.get("id"),
                    "length_km": segment.get("length_km"),
                    "edge_source": segment.get("edge_source"),
                    "is_virtual_connector": segment.get("is_virtual_connector"),
                    "reversed_direction": segment.get("reversed_direction"),
                    "segment_source": segment.get("segment_source"),
                },
                "geometry": segment.get("geometry"),
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def persist_route_stop_matches(
    route_id: int,
    stops: list[dict[str, Any]],
) -> None:
    with engine.begin() as connection:
        for stop in stops:
            station_id_to_save = stop.get("station_id")

            connection.execute(
                text("""
                    UPDATE route_stops
                    SET
                        station_id = :station_id,
                        match_method = :match_method,
                        match_confidence = :match_confidence
                    WHERE id = :route_stop_id
                      AND route_id = :route_id;
                """),
                {
                    "route_stop_id": stop["id"],
                    "route_id": route_id,
                    "station_id": station_id_to_save,
                    "match_method": stop.get("match_method"),
                    "match_confidence": stop.get("match_confidence"),
                },
            )

        connection.execute(
            text("""
                UPDATE routes
                SET updated_at = NOW()
                WHERE id = :route_id;
            """),
            {"route_id": route_id},
        )


def resolve_route_for_map(
    route_id: int,
    *,
    persist: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "route_id": route_id,
        "timings_ms": {},
        "catalog": {},
        "network": {},
        "candidate_logs": [],
        "locked_stop_candidates": [],
        "transition_diagnostics": [],
        "solver_notes": [],
        "fallback_mode": {
            "used": False,
            "reason": None,
        },
        "errors": [],
    }
    logger_context = {"route_id": route_id}

    log_event(
        "info",
        "resolve_route_for_map_started",
        route_id=route_id,
        persist=persist,
    )

    try:
        emit_progress(progress_callback, 8, "loading", {"route_id": route_id})

        with StageTimer(
            "load_route",
            diagnostics=diagnostics,
            logger_context=logger_context,
        ):
            payload = load_route(route_id)
            route = payload["route"]
            stops = payload["stops"]

        emit_progress(
            progress_callback,
            14,
            "loading",
            {
                "route_id": route_id,
                "stops_count": len(stops),
            },
        )

        emit_progress(progress_callback, 20, "candidates", {"route_id": route_id})

        with StageTimer(
            "load_global_station_catalog",
            diagnostics=diagnostics,
            logger_context=logger_context,
        ):
            catalog_payload = load_global_station_catalog(
                diagnostics=diagnostics,
                logger_context=logger_context,
            )

        candidates_per_stop: list[list[Candidate]] = []
        candidate_logs: list[dict[str, Any]] = []
        total_stops = max(1, len(stops))

        with StageTimer(
            "candidate_generation",
            diagnostics=diagnostics,
            logger_context=logger_context,
        ):
            for index, stop in enumerate(stops):
                candidates = build_candidates_for_stop(stop, catalog_payload)
                candidates_per_stop.append(candidates)

                stop_candidate_log = {
                    "stop_sequence": stop["stop_sequence"],
                    "station_name_raw": stop.get("station_name_raw"),
                    "station_code_rzd": stop.get("station_code_rzd"),
                    "stored_station_id": stop.get("stored_station_id"),
                    "stored_station_region_code": stop.get("stored_station_region_code"),
                    "candidate_count": len(candidates),
                    "candidates": [
                        {
                            "station_id": candidate.station_id,
                            "region_code": candidate.region_code,
                            "station_name": candidate.name,
                            "effective_score": round(candidate.effective_score, 4),
                            "name_score": round(candidate.name_score, 4),
                            "match_method": candidate.match_method,
                            "match_reason": candidate.match_reason,
                            "code_match": candidate.code_match,
                            "anchor": candidate.anchor,
                        }
                        for candidate in candidates
                    ],
                }
                candidate_logs.append(stop_candidate_log)

                log_event(
                    "info",
                    "stop_candidates_generated",
                    **stop_candidate_log,
                    **logger_context,
                )

                emit_progress(
                    progress_callback,
                    20 + int(((index + 1) / total_stops) * 22),
                    "candidates",
                    {
                        "processed_stops": index + 1,
                        "total_stops": total_stops,
                    },
                )

        diagnostics["candidate_logs"] = candidate_logs

        with StageTimer(
            "infer_route_regions",
            diagnostics=diagnostics,
            logger_context=logger_context,
        ):
            inferred_region_codes = infer_route_region_codes(
                stops=stops,
                candidates_per_stop=candidates_per_stop,
                diagnostics=diagnostics,
                logger_context=logger_context,
            )

        emit_progress(
            progress_callback,
            50,
            "network",
            {
                "route_id": route_id,
                "region_codes": inferred_region_codes,
            },
        )

        with StageTimer(
            "build_network",
            diagnostics=diagnostics,
            logger_context=logger_context,
        ):
            network = build_network_data(
                region_codes=inferred_region_codes,
                diagnostics=diagnostics,
                logger_context=logger_context,
                progress_callback=progress_callback,
            )

        emit_progress(progress_callback, 72, "solving", {"route_id": route_id})

        with StageTimer(
            "lock_stop_candidates",
            diagnostics=diagnostics,
            logger_context=logger_context,
        ):
            locked_candidates, _lock_logs = lock_route_stop_candidates(
                stops=stops,
                candidates_per_stop=candidates_per_stop,
                diagnostics=diagnostics,
                logger_context=logger_context,
            )

        geometry: dict[str, Any] | None = None
        geometry_source: str | None = None
        segment_items: list[dict[str, Any]] = []
        network_segments: list[dict[str, Any]] = []
        transition_logs: list[dict[str, Any]] = []

        network_mode = (network.get("stats") or {}).get("network_mode")

        if network_mode == "scope_topology_graph" and network.get("adjacency"):
            emit_progress(progress_callback, 88, "geometry", {"route_id": route_id})

            with StageTimer(
                "build_locked_station_topology_geometry",
                diagnostics=diagnostics,
                logger_context=logger_context,
            ):
                (
                    geometry,
                    segment_items,
                    network_segments,
                    transition_logs,
                ) = build_route_geometry_between_locked_candidates(
                    stops=stops,
                    locked_candidates=locked_candidates,
                    candidates_per_stop=candidates_per_stop,
                    network=network,
                    diagnostics=diagnostics,
                    logger_context=logger_context,
                )

            if network_segments:
                geometry_source = "graph_path"
            elif geometry is not None:
                geometry_source = "fallback_station_chain"
                diagnostics["fallback_mode"] = {
                    "used": True,
                    "reason": "locked_station_pairwise_graph_paths_not_found",
                }
        else:
            diagnostics["fallback_mode"] = {
                "used": True,
                "reason": "topology_graph_unavailable",
            }

        if geometry is None:
            fallback_geometry = build_fallback_geometry_from_selected_candidates(locked_candidates)
            if fallback_geometry is not None:
                geometry = fallback_geometry
                geometry_source = "fallback_station_chain"

        locked_rendering = diagnostics.get("locked_station_rendering") or {}
        if (locked_rendering.get("segments_with_fallback") or 0) > 0:
            diagnostics["fallback_mode"] = {
                "used": True,
                "reason": "partial_pairwise_fallback",
            }

        resolved_stops: list[dict[str, Any]] = []
        matched_stops_count = 0
        unresolved_stops_count = 0
        stop_resolution_output_logs: list[dict[str, Any]] = []

        with StageTimer(
            "resolve_stop_output",
            diagnostics=diagnostics,
            logger_context=logger_context,
        ):
            for index, stop in enumerate(stops):
                candidate = locked_candidates[index] if index < len(locked_candidates) else None

                if candidate is None:
                    resolved_stop = {
                        **stop,
                        "station_id": None,
                        "matched_station_name": None,
                        "station_name_matched": None,
                        "lon": None,
                        "lat": None,
                        "match_method": None,
                        "match_confidence": None,
                        "match_reason": "unresolved",
                    }
                    unresolved_stops_count += 1
                else:
                    match_method = candidate.match_method
                    if match_method not in {
                        "existing_visible_station_id",
                        "exact_visible_esr_code",
                        "exact_visible_uic_code",
                    }:
                        match_method = "locked_station_match"

                    confidence = round(max(0.05, min(0.99, candidate.effective_score)), 4)

                    resolved_stop = {
                        **stop,
                        "station_id": candidate.station_id,
                        "matched_station_name": candidate.name,
                        "station_name_matched": candidate.name,
                        "lon": candidate.lon,
                        "lat": candidate.lat,
                        "match_method": match_method,
                        "match_confidence": confidence,
                        "match_reason": candidate.match_reason,
                    }
                    matched_stops_count += 1

                resolved_stops.append(resolved_stop)

                stop_log = {
                    "stop_sequence": stop.get("stop_sequence"),
                    "station_name_raw": stop.get("station_name_raw"),
                    "station_code_rzd": stop.get("station_code_rzd"),
                    "station_id": resolved_stop.get("station_id"),
                    "matched_station_name": resolved_stop.get("matched_station_name"),
                    "match_method": resolved_stop.get("match_method"),
                    "match_confidence": resolved_stop.get("match_confidence"),
                    "match_reason": resolved_stop.get("match_reason"),
                }
                stop_resolution_output_logs.append(stop_log)

                log_event(
                    "info",
                    "stop_resolution_output",
                    **stop_log,
                    **logger_context,
                )

        diagnostics["stop_resolution_output_logs"] = stop_resolution_output_logs
        diagnostics["network_segments_count"] = len(network_segments)

        if persist:
            emit_progress(progress_callback, 92, "saving", {"route_id": route_id})
            with StageTimer(
                "persist_route_stop_matches",
                diagnostics=diagnostics,
                logger_context=logger_context,
            ):
                persist_route_stop_matches(route_id, resolved_stops)

        summary = {
            "route_id": route_id,
            "stops_count": len(resolved_stops),
            "matched_stops_count": matched_stops_count,
            "unresolved_stops_count": unresolved_stops_count,
            "geometry_ready": geometry is not None,
            "geometry_source": geometry_source,
            "network_segments_count": len(network_segments),
            "graph_stats": network["stats"],
            "fallback_mode_used": bool(diagnostics.get("fallback_mode", {}).get("used")),
            "fallback_mode_reason": diagnostics.get("fallback_mode", {}).get("reason"),
            "locked_segments_with_graph_path": (
                diagnostics.get("locked_station_rendering", {}).get("segments_with_graph_path") or 0
            ),
            "locked_segments_with_fallback": (
                diagnostics.get("locked_station_rendering", {}).get("segments_with_fallback") or 0
            ),
        }

        feature_collection = build_feature_collection(
            route,
            geometry,
            geometry_source,
            network_segments,
        )
        cleaned_diagnostics = cleanup_diagnostics(diagnostics)

        result = {
            "route": route,
            "item": {
                **route,
                "stops": resolved_stops,
                "geometry": geometry,
                "geometry_source": geometry_source,
                "network_segments": network_segments,
                "diagnostics": cleaned_diagnostics,
                "matched_stops_count": matched_stops_count,
                "unresolved_stops_count": unresolved_stops_count,
                "stops_count": len(resolved_stops),
            },
            "stops": resolved_stops,
            "geometry": geometry,
            "geometry_source": geometry_source,
            "network_segments": network_segments,
            "geojson": feature_collection,
            "segments": segment_items,
            "summary": summary,
            "diagnostics": cleaned_diagnostics,
        }

        emit_progress(progress_callback, 94, "geometry", summary)

        log_event(
            "info",
            "resolve_route_for_map_finished",
            route_id=route_id,
            summary=summary,
        )

        return result

    except Exception as exc:
        append_error_once(
            diagnostics,
            stage="resolve_route_for_map",
            exc=exc,
            extra={"persist": persist},
        )
        log_event(
            "error",
            "resolve_route_for_map_failed",
            route_id=route_id,
            persist=persist,
            exception=build_exception_payload(exc),
        )
        raise
