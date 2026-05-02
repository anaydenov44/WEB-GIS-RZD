import json
import math
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import engine
from app.route_import_service import import_route_payload
from app.rzd_client import RzdClient


def dump_rzd_debug_payload(
    *,
    label: str,
    payload: object,
    meta: dict | None = None,
) -> None:
    debug_dir = Path("debug_rzd")
    debug_dir.mkdir(exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    safe_label = "".join(
        ch if ch.isalnum() or ch in "-_" else "_"
        for ch in label
    )

    path = debug_dir / f"{timestamp}_{safe_label}.json"

    with path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "meta": meta or {},
                "payload": payload,
            },
            f,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

    print(f"RZD DEBUG DUMP saved: {path}")


RZD_NEARBY_STATION_RADIUS_KM = 5.0
RZD_NEARBY_STATION_LIMIT = 5
RZD_CODES_PER_STATION_LIMIT = 2
RZD_MAX_CODE_PAIR_ATTEMPTS = 10
RZD_ZONE_ROUTES_LIMIT = 40


def format_rzd_date(value: date | str) -> str:
    """
    RZD API ожидает дату в формате dd.mm.yyyy.

    Поддерживаем:
    - date object
    - ISO string yyyy-mm-dd
    - уже готовую строку dd.mm.yyyy
    """
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")

    text = str(value).strip()

    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
        return text

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).strftime("%d.%m.%Y")

    return text


def normalize_station_code(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def parse_distance_km(value: Any) -> float | None:
    """
    Приводит Distance из RZD API к float.

    Возможные варианты:
    - 123
    - "123"
    - "123 км"
    - "123,5"
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().replace(",", ".")
    if not text:
        return None

    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None

    return float(match.group(0))


def parse_waiting_time_minutes(value: Any) -> int | None:
    """
    Приводит WaitingTime из RZD API к минутам.

    Возможные варианты:
    - 5
    - "5"
    - "00:05"
    - "1:20"
    - "1 ч 20 мин"
    - "20 мин"
    """
    if value is None:
        return None

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return int(value)

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return int(text)

    hhmm_match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if hhmm_match:
        return int(hhmm_match.group(1)) * 60 + int(hhmm_match.group(2))

    hour_match = re.search(r"(\d+)\s*ч", text, flags=re.IGNORECASE)
    minute_match = re.search(r"(\d+)\s*м", text, flags=re.IGNORECASE)

    if hour_match or minute_match:
        hours = int(hour_match.group(1)) if hour_match else 0
        minutes = int(minute_match.group(1)) if minute_match else 0
        return hours * 60 + minutes

    number_match = re.search(r"\d+", text)
    if number_match:
        return int(number_match.group(0))

    return None


def normalize_time_value(value: Any) -> str | None:
    """
    Оставляем время строкой, потому что текущая схема route_stops
    уже работает с arrival_time/departure_time как строковыми значениями.
    """
    if value is None:
        return None

    text = str(value).strip()
    return text or None


def extract_rzd_stop_name(stop: dict[str, Any]) -> str | None:
    """
    Достает название остановки из разных вариантов payload РЖД.

    Важно: итоговый маршрут дальше строится только по таким официальным
    остановкам РЖД. OSM-станции могут быть только match-привязкой,
    но не самостоятельными остановками маршрута.
    """
    for key in (
        "station_name",
        "stationName",
        "station",
        "name",
        "Name",
    ):
        value = stop.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()

    return None


def extract_rzd_stop_code(stop: dict[str, Any]) -> str | None:
    """Достает официальный код остановки из разных вариантов payload РЖД."""
    for key in (
        "station_code",
        "stationCode",
        "expressCode",
        "ExpressCode",
        "code",
        "Code",
    ):
        value = stop.get(key)
        normalized = normalize_station_code(value)
        if normalized:
            return normalized

    return None


def is_route_label_stop(stop: dict[str, Any]) -> bool:
    """
    Отсекает строки-заголовки вида "[АРХАНГЕЛ Г , МОСКВА ЯР]".

    Такие записи описывают направление/сегмент, а не остановку.
    Если у записи есть официальный код, оставляем ее: код сильнее
    эвристики по названию.
    """
    name = extract_rzd_stop_name(stop) or ""
    code = extract_rzd_stop_code(stop)
    text = name.strip()

    if code or not text:
        return False

    return (
        text.startswith("[")
        or text.endswith("]")
        or "," in text
        or " - " in text
    )


def normalize_official_rzd_stops(stops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Финальная защита перед импортом: список route_stops должен состоять
    только из официальных остановок РЖД.

    Здесь мы не добавляем nearby OSM-станции в начало/конец маршрута.
    Если OSM-match понадобится, он должен появиться позже как station_id
    у существующей официальной остановки, а не как новая остановка.
    """
    result: list[dict[str, Any]] = []

    for stop in stops:
        raw_stop = {
            "name": stop.get("station_name_raw") or stop.get("station_name_matched"),
            "code": stop.get("station_code_rzd"),
        }

        if is_route_label_stop(raw_stop):
            continue

        result.append(
            {
                **stop,
                "stop_sequence": len(result) + 1,
            }
        )

    return result


def search_rzd_station_codes(
    query: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Поиск станций через существующий RzdClient.search_station_codes.

    Ничего не пишет в БД.
    Используется для autocomplete станции Б или уточнения станции А.
    """
    query = query.strip()
    if len(query) < 2:
        raise ValueError("query должен содержать минимум 2 символа")

    client = RzdClient()
    payload = client.search_station_codes(query)

    items: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    for item in payload.get("items", []):
        name = item.get("name")
        code = normalize_station_code(item.get("code"))

        if not name or not code:
            continue

        if code in seen_codes:
            continue

        seen_codes.add(code)

        items.append(
            {
                "name": str(name),
                "code": code,
                "raw": item.get("raw"),
            }
        )

    return items[:limit]


def search_rzd_routes(
    *,
    origin_code: str,
    destination_code: str,
    dep_date: date | str,
    check_seats: bool = False,
    include_transfers: bool = False,
) -> dict[str, Any]:
    """
    Поиск поездов между двумя РЖД-кодами станций.

    Это адаптер над RzdClient.search_routes.
    """
    origin_code = origin_code.strip()
    destination_code = destination_code.strip()

    if not origin_code:
        raise ValueError("origin_code is required")

    if not destination_code:
        raise ValueError("destination_code is required")

    client = RzdClient()

    payload = client.search_routes(
        code0=origin_code,
        code1=destination_code,
        dep_date=format_rzd_date(dep_date),
        check_seats=check_seats,
        include_transfers=include_transfers,
    )

    items: list[dict[str, Any]] = []

    for item in payload.get("items", []):
        train_number = item.get("number")
        if not train_number:
            continue

        items.append(
            {
                "train_number": str(train_number),
                "brand": item.get("brand"),
                "carrier": item.get("carrier"),
                "departure_date": item.get("date0"),
                "arrival_date": item.get("date1"),
                "departure_time": item.get("time0"),
                "arrival_time": item.get("time1"),
                "origin_name": item.get("route0"),
                "destination_name": item.get("route1"),
                "time_in_way": item.get("timeInWay"),
                "raw": item.get("raw"),
            }
        )

    return {
        "request": payload.get("request"),
        "items": items,
        "total": len(items),
        "raw": payload.get("raw"),
    }


def search_rzd_routes_calendar(
    *,
    origin_code: str,
    destination_code: str,
    start_date: date | str | None = None,
    days_ahead: int = 2,
    check_seats: bool = False,
    include_transfers: bool = False,
    pause_seconds: float = 0.35,
) -> dict[str, Any]:
    """
    Ищет поезда между двумя кодами станций не на одну дату,
    а на диапазон ближайших дней.

    Важно:
    - не падает целиком, если один день дал ошибку;
    - возвращает найденные поезда с датой поиска;
    - ограничиваем days_ahead, чтобы не перегружать RZD API.
    """
    origin_code = origin_code.strip()
    destination_code = destination_code.strip()

    if not origin_code:
        raise ValueError("origin_code is required")

    if not destination_code:
        raise ValueError("destination_code is required")

    days_ahead = max(1, min(int(days_ahead), 30))

    if start_date is None:
        current_start_date = date.today()
    elif isinstance(start_date, date):
        current_start_date = start_date
    else:
        text = str(start_date).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            current_start_date = date.fromisoformat(text)
        elif re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", text):
            current_start_date = date(
                int(text[6:10]),
                int(text[3:5]),
                int(text[0:2]),
            )
        else:
            current_start_date = date.today()

    all_items: list[dict[str, Any]] = []
    date_summaries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for day_offset in range(days_ahead):
        current_date = current_start_date + timedelta(days=day_offset)
        current_date_iso = current_date.isoformat()
        current_date_rzd = format_rzd_date(current_date)

        try:
            result = search_rzd_routes(
                origin_code=origin_code,
                destination_code=destination_code,
                dep_date=current_date,
                check_seats=check_seats,
                include_transfers=include_transfers,
            )

            day_items = result.get("items") or []

            for item in day_items:
                all_items.append(
                    {
                        **item,
                        "search_date": current_date_iso,
                        "search_date_rzd": current_date_rzd,
                    }
                )

            date_summaries.append(
                {
                    "date": current_date_iso,
                    "date_rzd": current_date_rzd,
                    "status": "ok",
                    "trains_count": len(day_items),
                }
            )

        except Exception as exc:
            errors.append(
                {
                    "date": current_date_iso,
                    "date_rzd": current_date_rzd,
                    "error": str(exc),
                }
            )

            date_summaries.append(
                {
                    "date": current_date_iso,
                    "date_rzd": current_date_rzd,
                    "status": "failed",
                    "trains_count": 0,
                    "error": str(exc),
                }
            )

        if pause_seconds > 0 and day_offset < days_ahead - 1:
            time.sleep(pause_seconds)

    deduped_items: list[dict[str, Any]] = []
    seen = set()

    for item in all_items:
        key = (
            str(item.get("train_number")),
            str(item.get("search_date")),
            str(item.get("departure_time")),
            str(item.get("origin_name")),
            str(item.get("destination_name")),
        )

        if key in seen:
            continue

        seen.add(key)
        deduped_items.append(item)

    deduped_items.sort(
        key=lambda item: (
            str(item.get("search_date") or ""),
            str(item.get("departure_time") or ""),
            str(item.get("train_number") or ""),
        )
    )

    return {
        "request": {
            "origin_code": origin_code,
            "destination_code": destination_code,
            "start_date": current_start_date.isoformat(),
            "days_ahead": days_ahead,
            "check_seats": check_seats,
            "include_transfers": include_transfers,
        },
        "items": deduped_items,
        "total": len(deduped_items),
        "dates_checked": days_ahead,
        "dates_with_trains": len(
            [
                item for item in date_summaries
                if item.get("status") == "ok" and item.get("trains_count", 0) > 0
            ]
        ),
        "date_summaries": date_summaries,
        "errors": errors,
    }


def load_station_for_rzd_search(station_id: int) -> dict[str, Any]:
    query = text("""
        SELECT
            id,
            region_code,
            name,
            uic_ref,
            esr_user,
            is_main_rail_station,
            is_visible_default,
            ST_X(geom) AS lon,
            ST_Y(geom) AS lat
        FROM stations
        WHERE id = :station_id
        LIMIT 1;
    """)

    with engine.connect() as connection:
        row = connection.execute(query, {"station_id": station_id}).first()

    if row is None:
        raise ValueError(f"Station not found: {station_id}")

    return dict(row._mapping)


def normalize_rzd_name(value: str | None) -> str:
    if not value:
        return ""

    text = str(value).upper().replace("Ё", "Е")
    text = text.replace("-", " ")
    text = text.replace("—", " ")
    text = text.replace("–", " ")

    drop_tokens = [
        "СТАНЦИЯ",
        "СТ",
        "ПАССАЖИРСКАЯ",
        "ПАСС",
        "ВОКЗАЛ",
        "ОСТАНОВОЧНЫЙ",
        "ПУНКТ",
        "ОСТ",
        "ПЛАТФОРМА",
    ]

    for token in drop_tokens:
        text = text.replace(token, " ")

    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def simple_name_score(left: str | None, right: str | None) -> float:
    left_norm = normalize_rzd_name(left)
    right_norm = normalize_rzd_name(right)

    if not left_norm or not right_norm:
        return 0.0

    if left_norm == right_norm:
        return 1.0

    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())

    if not left_tokens or not right_tokens:
        return 0.0

    overlap = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))

    contains_bonus = 0.0
    if left_norm in right_norm or right_norm in left_norm:
        contains_bonus = 0.25

    return max(0.0, min(1.0, overlap + contains_bonus))


def extract_lon_lat_from_rzd_raw(raw: Any) -> tuple[float | None, float | None]:
    if not isinstance(raw, dict):
        return None, None

    lon_keys = ["lon", "lng", "longitude", "x", "X", "Longitude"]
    lat_keys = ["lat", "latitude", "y", "Y", "Latitude"]

    lon = None
    lat = None

    for key in lon_keys:
        if key in raw:
            lon = raw.get(key)
            break

    for key in lat_keys:
        if key in raw:
            lat = raw.get(key)
            break

    try:
        lon = float(str(lon).replace(",", ".")) if lon is not None else None
        lat = float(str(lat).replace(",", ".")) if lat is not None else None
    except Exception:
        return None, None

    if lon is None or lat is None:
        return None, None

    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None, None

    return lon, lat


def haversine_km_for_rzd_score(
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
) -> float:
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


def score_rzd_code_candidate_for_station(
    *,
    station: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    station_name = station.get("name")
    candidate_label = candidate.get("label")

    name_score = simple_name_score(station_name, candidate_label)

    station_uic = normalize_station_code(station.get("uic_ref"))
    candidate_code = normalize_station_code(candidate.get("code"))

    uic_match = bool(station_uic and candidate_code and station_uic == candidate_code)

    station_lon = station.get("lon")
    station_lat = station.get("lat")
    raw_lon = candidate.get("lon")
    raw_lat = candidate.get("lat")

    distance_km = None
    distance_score = 0.0
    distance_penalty = 0.0

    if (
        station_lon is not None
        and station_lat is not None
        and raw_lon is not None
        and raw_lat is not None
    ):
        distance_km = haversine_km_for_rzd_score(
            float(station_lon),
            float(station_lat),
            float(raw_lon),
            float(raw_lat),
        )

        if distance_km <= 1:
            distance_score = 20.0
        elif distance_km <= 5:
            distance_score = 15.0
        elif distance_km <= 15:
            distance_score = 8.0
        elif distance_km <= 50:
            distance_score = 2.0
        elif distance_km <= 150:
            distance_penalty = 25.0
        else:
            distance_penalty = 60.0

    source = candidate.get("source")

    source_bonus = 0.0
    if source == "rzd_suggester":
        source_bonus = 8.0
    elif source == "osm_uic_ref":
        source_bonus = 5.0
    elif source == "nearby_main_station":
        source_bonus = 3.0

    uic_bonus = 30.0 if uic_match else 0.0

    weak_name_penalty = 0.0
    if name_score < 0.35:
        weak_name_penalty = 35.0
    elif name_score < 0.55:
        weak_name_penalty = 12.0

    final_score = (
        name_score * 70.0
        + uic_bonus
        + distance_score
        + source_bonus
        - distance_penalty
        - weak_name_penalty
    )

    confidence = max(0.0, min(1.0, final_score / 120.0))

    return {
        **candidate,
        "name_score": round(name_score, 4),
        "uic_match": uic_match,
        "distance_km": round(distance_km, 3) if distance_km is not None else None,
        "distance_score": round(distance_score, 3),
        "distance_penalty": round(distance_penalty, 3),
        "weak_name_penalty": round(weak_name_penalty, 3),
        "final_score": round(final_score, 3),
        "confidence": round(confidence, 4),
    }


def resolve_rzd_code_for_station(
    station_id: int,
    *,
    suggester_limit: int = 10,
) -> dict[str, Any]:
    station = load_station_for_rzd_search(station_id)
    station_name = station.get("name") or ""

    candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    def add_candidate(
        *,
        source: str,
        code: Any,
        label: str | None,
        raw: Any = None,
        base_priority: int,
        lon: float | None = None,
        lat: float | None = None,
    ) -> None:
        normalized_code = normalize_station_code(code)
        if not normalized_code:
            return

        if normalized_code in seen_codes:
            return

        seen_codes.add(normalized_code)

        candidates.append(
            {
                "source": source,
                "code": normalized_code,
                "label": label,
                "base_priority": base_priority,
                "lon": lon,
                "lat": lat,
                "raw": raw,
            }
        )

    # 1. OSM uic_ref — хороший кандидат для passenger RZD API.
    add_candidate(
        source="osm_uic_ref",
        code=station.get("uic_ref"),
        label=station_name,
        raw=None,
        base_priority=20,
    )

    # 2. RZD suggester по названию OSM-станции.
    if station_name:
        try:
            suggester_items = search_rzd_station_codes(
                station_name,
                limit=suggester_limit,
            )

            diagnostics.append(
                {
                    "stage": "rzd_suggester",
                    "query": station_name,
                    "status": "ok",
                    "items_count": len(suggester_items),
                }
            )

            for index, item in enumerate(suggester_items):
                raw = item.get("raw")
                lon, lat = extract_lon_lat_from_rzd_raw(raw)

                add_candidate(
                    source="rzd_suggester",
                    code=item.get("code"),
                    label=item.get("name"),
                    raw=raw,
                    base_priority=10 + index,
                    lon=lon,
                    lat=lat,
                )

        except Exception as exc:
            diagnostics.append(
                {
                    "stage": "rzd_suggester",
                    "query": station_name,
                    "status": "failed",
                    "error": str(exc),
                }
            )

    # 3. ESR не используем для поиска, только diagnostics.
    if station.get("esr_user"):
        diagnostics.append(
            {
                "stage": "osm_esr_user",
                "code": str(station.get("esr_user")),
                "status": "not_used_for_train_search",
                "reason": "ESR can cause SYSTEM_ERROR in passenger RZD timetable API",
            }
        )

    scored_candidates = [
        score_rzd_code_candidate_for_station(
            station=station,
            candidate=candidate,
        )
        for candidate in candidates
    ]

    scored_candidates.sort(
        key=lambda item: (
            -float(item.get("final_score") or 0.0),
            int(item.get("base_priority") or 999),
            str(item.get("source") or ""),
            str(item.get("code") or ""),
        )
    )

    recommended = scored_candidates[0] if scored_candidates else None

    return {
        "station": station,
        "recommended_code": recommended.get("code") if recommended else None,
        "recommended_source": recommended.get("source") if recommended else None,
        "recommended_label": recommended.get("label") if recommended else None,
        "confidence": recommended.get("confidence") if recommended else 0.0,
        "candidates": scored_candidates,
        "diagnostics": diagnostics,
    }


def filter_rzd_code_candidates_for_search(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []

    for candidate in candidates:
        confidence = float(candidate.get("confidence") or 0.0)
        name_score = float(candidate.get("name_score") or 0.0)
        distance_km = candidate.get("distance_km")

        if confidence < 0.30:
            continue

        if name_score < 0.25 and not candidate.get("uic_match"):
            continue

        if distance_km is not None and float(distance_km) > 150:
            continue

        result.append(candidate)

    return result



def load_station_zone_candidates(
    station_id: int,
    *,
    radius_km: float = RZD_NEARBY_STATION_RADIUS_KM,
    limit: int = RZD_NEARBY_STATION_LIMIT,
) -> list[dict[str, Any]]:
    """
    Возвращает не одну точную станцию, а зону выбора:
    1. выбранная станция;
    2. ближайшие главные станции;
    3. ближайшие станции с uic_ref;
    4. ближайшие станции, через которые уже есть маршруты.
    """
    selected_station = load_station_for_rzd_search(station_id)

    if selected_station.get("lon") is None or selected_station.get("lat") is None:
        return [
            {
                **selected_station,
                "zone_source": "selected",
                "distance_from_selected_km": 0.0,
                "known_routes_count": 0,
            }
        ]

    selected_station = {
        **selected_station,
        "zone_source": "selected",
        "distance_from_selected_km": 0.0,
        "known_routes_count": 0,
    }

    if limit <= 1 or radius_km <= 0:
        return [selected_station]

    query = text("""
        WITH origin AS (
            SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS geom
        ),
        nearby AS (
            SELECT
                s.id,
                s.region_code,
                s.name,
                s.uic_ref,
                s.esr_user,
                s.is_main_rail_station,
                s.is_visible_default,
                ST_X(s.geom) AS lon,
                ST_Y(s.geom) AS lat,
                ST_Distance(s.geom::geography, origin.geom::geography) / 1000.0 AS distance_km
            FROM stations s
            CROSS JOIN origin
            WHERE
                s.id <> :station_id
                AND s.geom IS NOT NULL
                AND s.is_visible_default = TRUE
                AND ST_DWithin(
                    s.geom::geography,
                    origin.geom::geography,
                    :radius_m
                )
                AND (
                    s.is_main_rail_station = TRUE
                    OR (s.uic_ref IS NOT NULL AND s.uic_ref <> '')
                    OR EXISTS (
                        SELECT 1
                        FROM route_stops rsx
                        WHERE rsx.station_id = s.id
                        LIMIT 1
                    )
                )
        ),
        route_counts AS (
            SELECT
                rs.station_id,
                COUNT(DISTINCT rs.route_id) AS routes_count
            FROM route_stops rs
            WHERE rs.station_id IN (SELECT id FROM nearby)
            GROUP BY rs.station_id
        )
        SELECT
            n.*,
            COALESCE(rc.routes_count, 0) AS known_routes_count
        FROM nearby n
        LEFT JOIN route_counts rc ON rc.station_id = n.id
        ORDER BY
            n.is_main_rail_station DESC,
            CASE WHEN n.uic_ref IS NOT NULL AND n.uic_ref <> '' THEN 0 ELSE 1 END,
            COALESCE(rc.routes_count, 0) DESC,
            n.distance_km ASC,
            n.name NULLS LAST,
            n.id
        LIMIT :nearby_limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "station_id": station_id,
                "lon": float(selected_station["lon"]),
                "lat": float(selected_station["lat"]),
                "radius_m": float(radius_km) * 1000.0,
                "nearby_limit": max(0, limit - 1),
            },
        ).fetchall()

    nearby_items = []
    for row in rows:
        item = dict(row._mapping)
        nearby_items.append(
            {
                **item,
                "zone_source": "nearby_station",
                "distance_from_selected_km": round(float(item.get("distance_km") or 0.0), 3),
            }
        )

    return [selected_station, *nearby_items]


def load_routes_for_station_zone(
    station_id: int,
    *,
    radius_km: float = RZD_NEARBY_STATION_RADIUS_KM,
    limit: int = RZD_ZONE_ROUTES_LIMIT,
) -> list[dict[str, Any]]:
    station = load_station_for_rzd_search(station_id)

    if station.get("lon") is None or station.get("lat") is None:
        return []

    query = text("""
        WITH origin AS (
            SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS geom
        ),
        zone_stations AS (
            SELECT
                s.id,
                s.name,
                s.region_code,
                s.uic_ref,
                s.is_main_rail_station,
                ST_Distance(s.geom::geography, origin.geom::geography) / 1000.0 AS distance_km
            FROM stations s
            CROSS JOIN origin
            WHERE
                s.geom IS NOT NULL
                AND s.is_visible_default = TRUE
                AND ST_DWithin(
                    s.geom::geography,
                    origin.geom::geography,
                    :radius_m
                )
        )
        SELECT DISTINCT ON (r.id)
            r.id,
            r.source_system,
            r.external_route_id,
            r.train_number,
            r.route_name,
            r.origin_station_name,
            r.destination_station_name,
            r.snapshot_date,
            r.operates_from,
            r.operates_to,
            r.is_active,
            r.notes,
            rs.stop_sequence,
            zs.id AS zone_station_id,
            zs.name AS zone_station_name,
            zs.region_code AS zone_station_region_code,
            zs.distance_km AS zone_station_distance_km,
            zs.is_main_rail_station AS zone_station_is_main,
            COUNT(*) OVER (PARTITION BY r.id) AS route_hits_count
        FROM zone_stations zs
        JOIN route_stops rs ON rs.station_id = zs.id
        JOIN routes r ON r.id = rs.route_id
        WHERE r.is_active = TRUE
        ORDER BY
            r.id,
            zs.distance_km ASC,
            zs.is_main_rail_station DESC,
            rs.stop_sequence ASC
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "lon": float(station["lon"]),
                "lat": float(station["lat"]),
                "radius_m": float(radius_km) * 1000.0,
                "limit": limit,
            },
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row._mapping)
        if item.get("zone_station_distance_km") is not None:
            item["zone_station_distance_km"] = round(
                float(item["zone_station_distance_km"]),
                3,
            )
        items.append(item)

    return items


def build_zone_rzd_code_candidates(
    *,
    selected_station_id: int,
    radius_km: float = RZD_NEARBY_STATION_RADIUS_KM,
    station_limit: int = RZD_NEARBY_STATION_LIMIT,
    codes_per_station_limit: int = RZD_CODES_PER_STATION_LIMIT,
) -> dict[str, Any]:
    station_candidates = load_station_zone_candidates(
        selected_station_id,
        radius_km=radius_km,
        limit=station_limit,
    )

    all_code_candidates: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    seen = set()

    for station_index, station in enumerate(station_candidates):
        try:
            resolved = resolve_rzd_code_for_station(int(station["id"]))
            filtered_codes = filter_rzd_code_candidates_for_search(
                resolved.get("candidates") or []
            )

            diagnostics.append(
                {
                    "station_id": station["id"],
                    "station_name": station.get("name"),
                    "zone_source": station.get("zone_source"),
                    "distance_from_selected_km": station.get("distance_from_selected_km"),
                    "resolve_status": "ok",
                    "resolved_candidates_count": len(resolved.get("candidates") or []),
                    "filtered_candidates_count": len(filtered_codes),
                    "resolve_diagnostics": resolved.get("diagnostics") or [],
                }
            )

            for code_index, code_candidate in enumerate(
                filtered_codes[:codes_per_station_limit]
            ):
                code = str(code_candidate.get("code") or "").strip()
                if not code:
                    continue

                key = (
                    code,
                    int(station["id"]),
                    str(code_candidate.get("source") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)

                public_code_candidate = {
                    key_name: value
                    for key_name, value in code_candidate.items()
                    if key_name != "raw"
                }

                all_code_candidates.append(
                    {
                        **public_code_candidate,
                        "station_id": int(station["id"]),
                        "station_name": station.get("name"),
                        "station_region_code": station.get("region_code"),
                        "station_zone_source": station.get("zone_source"),
                        "station_distance_from_selected_km": station.get(
                            "distance_from_selected_km"
                        ),
                        "station_is_main_rail_station": bool(
                            station.get("is_main_rail_station")
                        ),
                        "station_known_routes_count": int(
                            station.get("known_routes_count") or 0
                        ),
                        "station_candidate_index": station_index,
                        "code_candidate_index": code_index,
                    }
                )

        except Exception as exc:
            diagnostics.append(
                {
                    "station_id": station.get("id"),
                    "station_name": station.get("name"),
                    "zone_source": station.get("zone_source"),
                    "distance_from_selected_km": station.get("distance_from_selected_km"),
                    "resolve_status": "failed",
                    "error": str(exc),
                }
            )

    all_code_candidates.sort(
        key=lambda item: (
            0 if item.get("station_zone_source") == "selected" else 1,
            float(item.get("station_distance_from_selected_km") or 0.0),
            -float(item.get("confidence") or 0.0),
            -float(item.get("final_score") or 0.0),
            int(item.get("base_priority") or 999),
            str(item.get("code") or ""),
        )
    )

    return {
        "station_candidates": station_candidates,
        "code_candidates": all_code_candidates,
        "diagnostics": diagnostics,
    }


def build_code_pair_attempt_plan(
    *,
    origin_code_candidates: list[dict[str, Any]],
    destination_code_candidates: list[dict[str, Any]],
    max_attempts: int = RZD_MAX_CODE_PAIR_ATTEMPTS,
) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    seen_pairs = set()

    for origin_candidate in origin_code_candidates:
        for destination_candidate in destination_code_candidates:
            origin_code = str(origin_candidate.get("code") or "").strip()
            destination_code = str(destination_candidate.get("code") or "").strip()

            if not origin_code or not destination_code:
                continue

            if origin_code == destination_code:
                continue

            pair_key = (origin_code, destination_code)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            exact_pair = (
                origin_candidate.get("station_zone_source") == "selected"
                and destination_candidate.get("station_zone_source") == "selected"
            )

            similar_pair = not exact_pair

            origin_distance = float(
                origin_candidate.get("station_distance_from_selected_km") or 0.0
            )
            destination_distance = float(
                destination_candidate.get("station_distance_from_selected_km") or 0.0
            )

            confidence_sum = (
                float(origin_candidate.get("confidence") or 0.0)
                + float(destination_candidate.get("confidence") or 0.0)
            )

            pairs.append(
                {
                    "origin_candidate": origin_candidate,
                    "destination_candidate": destination_candidate,
                    "exact_pair": exact_pair,
                    "similar_pair": not exact_pair,
                    "origin_code": origin_code,
                    "destination_code": destination_code,
                    "origin_distance_km": origin_distance,
                    "destination_distance_km": destination_distance,
                    "confidence_sum": confidence_sum,
                }
            )

    pairs.sort(
        key=lambda item: (
            0 if item["exact_pair"] else 1,
            item["origin_distance_km"] + item["destination_distance_km"],
            -item["confidence_sum"],
            item["origin_code"],
            item["destination_code"],
        )
    )

    return pairs[:max_attempts]

def load_nearby_main_stations_for_rzd_search(
    station: dict[str, Any],
    *,
    radius_km: float = 7.0,
    limit: int = 4,
) -> list[dict[str, Any]]:
    lon = station.get("lon")
    lat = station.get("lat")

    if lon is None or lat is None:
        return []

    query = text("""
        WITH origin AS (
            SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS geom
        )
        SELECT
            s.id,
            s.region_code,
            s.name,
            s.uic_ref,
            s.esr_user,
            s.is_main_rail_station,
            s.is_visible_default,
            ST_X(s.geom) AS lon,
            ST_Y(s.geom) AS lat,
            ST_Distance(s.geom::geography, origin.geom::geography) / 1000.0 AS distance_km
        FROM stations s
        CROSS JOIN origin
        WHERE
            s.geom IS NOT NULL
            AND s.is_visible_default = TRUE
            AND s.id <> :station_id
            AND ST_DWithin(
                s.geom::geography,
                origin.geom::geography,
                :radius_m
            )
            AND (
                s.is_main_rail_station = TRUE
                OR s.uic_ref IS NOT NULL
            )
        ORDER BY
            s.is_main_rail_station DESC,
            CASE WHEN s.uic_ref IS NOT NULL AND s.uic_ref <> '' THEN 0 ELSE 1 END,
            distance_km ASC,
            s.name NULLS LAST,
            s.id
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "station_id": station["id"],
                "lon": float(lon),
                "lat": float(lat),
                "radius_m": radius_km * 1000.0,
                "limit": limit,
            },
        ).fetchall()

    return [dict(row._mapping) for row in rows]


def extend_candidates_with_nearby_main_stations(
    *,
    base_station: dict[str, Any],
    existing_candidates: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result = list(existing_candidates)
    seen_codes = {
        str(item.get("code"))
        for item in result
        if item.get("code")
    }

    nearby_stations = load_nearby_main_stations_for_rzd_search(base_station)

    diagnostics.append(
        {
            "stage": "nearby_main_stations",
            "status": "ok",
            "items_count": len(nearby_stations),
        }
    )

    for nearby_station in nearby_stations:
        code = normalize_station_code(nearby_station.get("uic_ref"))
        if not code or code in seen_codes:
            continue

        seen_codes.add(code)

        candidate = {
            "source": "nearby_main_station",
            "code": code,
            "label": nearby_station.get("name"),
            "base_priority": 80,
            "lon": nearby_station.get("lon"),
            "lat": nearby_station.get("lat"),
            "nearby_station_id": nearby_station.get("id"),
            "nearby_distance_km": round(float(nearby_station.get("distance_km") or 0.0), 3),
            "raw": None,
        }

        result.append(
            score_rzd_code_candidate_for_station(
                station=base_station,
                candidate=candidate,
            )
        )

    result.sort(
        key=lambda item: (
            -float(item.get("final_score") or 0.0),
            int(item.get("base_priority") or 999),
            str(item.get("source") or ""),
            str(item.get("code") or ""),
        )
    )

    return result



def search_rzd_routes_calendar_by_stations(
    *,
    origin_station_id: int,
    destination_station_id: int,
    start_date: date | str | None = None,
    days_ahead: int = 2,
    check_seats: bool = False,
    nearby_radius_km: float = RZD_NEARBY_STATION_RADIUS_KM,
    nearby_station_limit: int = RZD_NEARBY_STATION_LIMIT,
    max_code_pair_attempts: int = RZD_MAX_CODE_PAIR_ATTEMPTS,
) -> dict[str, Any]:
    """
    A→B поиск теперь работает не как точная пара station_id,
    а как поиск по зонам вокруг выбранных станций.

    Логика:
    1. выбранная А;
    2. nearby-кандидаты вокруг А;
    3. выбранная Б;
    4. nearby-кандидаты вокруг Б;
    5. подбор РЖД-кодов;
    6. перебор пар кодов;
    7. календарный поиск.
    """
    origin_station = load_station_for_rzd_search(origin_station_id)
    destination_station = load_station_for_rzd_search(destination_station_id)

    days_ahead = max(1, min(int(days_ahead), 30))
    nearby_radius_km = max(0.5, min(float(nearby_radius_km), 15.0))
    nearby_station_limit = max(1, min(int(nearby_station_limit), 10))
    max_code_pair_attempts = max(1, min(int(max_code_pair_attempts), 30))

    origin_zone = build_zone_rzd_code_candidates(
        selected_station_id=origin_station_id,
        radius_km=nearby_radius_km,
        station_limit=nearby_station_limit,
    )

    destination_zone = build_zone_rzd_code_candidates(
        selected_station_id=destination_station_id,
        radius_km=nearby_radius_km,
        station_limit=nearby_station_limit,
    )

    origin_code_candidates = origin_zone["code_candidates"]
    destination_code_candidates = destination_zone["code_candidates"]

    origin_zone_routes = load_routes_for_station_zone(
        origin_station_id,
        radius_km=nearby_radius_km,
        limit=RZD_ZONE_ROUTES_LIMIT,
    )
    destination_zone_routes = load_routes_for_station_zone(
        destination_station_id,
        radius_km=nearby_radius_km,
        limit=RZD_ZONE_ROUTES_LIMIT,
    )

    common_payload = {
        "origin_station": origin_station,
        "destination_station": destination_station,
        "nearby_radius_km": nearby_radius_km,
        "origin_station_candidates": origin_zone["station_candidates"],
        "destination_station_candidates": destination_zone["station_candidates"],
        "origin_code_candidates": origin_code_candidates,
        "destination_code_candidates": destination_code_candidates,
        "origin_code_diagnostics": origin_zone["diagnostics"],
        "destination_code_diagnostics": destination_zone["diagnostics"],
        "origin_zone_routes": origin_zone_routes,
        "destination_zone_routes": destination_zone_routes,
    }

    if not origin_code_candidates:
        return {
            **common_payload,
            "items": [],
            "total": 0,
            "status": "no_origin_codes",
            "exact_found": False,
            "similar_used": False,
            "message": "Для зоны отправления не найден подходящий код РЖД API",
            "code_attempts": [],
            "date_summaries": [],
            "errors": [],
        }

    if not destination_code_candidates:
        return {
            **common_payload,
            "items": [],
            "total": 0,
            "status": "no_destination_codes",
            "exact_found": False,
            "similar_used": False,
            "message": "Для зоны назначения не найден подходящий код РЖД API",
            "code_attempts": [],
            "date_summaries": [],
            "errors": [],
        }

    pair_attempts = build_code_pair_attempt_plan(
        origin_code_candidates=origin_code_candidates,
        destination_code_candidates=destination_code_candidates,
        max_attempts=max_code_pair_attempts,
    )

    code_attempts: list[dict[str, Any]] = []
    best_empty_result: dict[str, Any] | None = None
    exact_pair_was_tried = False

    for pair_index, pair in enumerate(pair_attempts):
        origin_candidate = pair["origin_candidate"]
        destination_candidate = pair["destination_candidate"]

        exact_pair_was_tried = exact_pair_was_tried or bool(pair["exact_pair"])

        attempt = {
            "attempt_index": pair_index + 1,
            "origin_code": pair["origin_code"],
            "origin_source": origin_candidate.get("source"),
            "origin_label": origin_candidate.get("label"),
            "origin_confidence": origin_candidate.get("confidence"),
            "origin_station_id": origin_candidate.get("station_id"),
            "origin_station_name": origin_candidate.get("station_name"),
            "origin_station_zone_source": origin_candidate.get("station_zone_source"),
            "origin_station_distance_from_selected_km": origin_candidate.get(
                "station_distance_from_selected_km"
            ),
            "destination_code": pair["destination_code"],
            "destination_source": destination_candidate.get("source"),
            "destination_label": destination_candidate.get("label"),
            "destination_confidence": destination_candidate.get("confidence"),
            "destination_station_id": destination_candidate.get("station_id"),
            "destination_station_name": destination_candidate.get("station_name"),
            "destination_station_zone_source": destination_candidate.get("station_zone_source"),
            "destination_station_distance_from_selected_km": destination_candidate.get(
                "station_distance_from_selected_km"
            ),
            "exact_pair": bool(pair["exact_pair"]),
            "similar_pair": bool(pair["similar_pair"]),
            "status": "started",
            "trains_count": 0,
        }

        try:
            result = search_rzd_routes_calendar(
                origin_code=pair["origin_code"],
                destination_code=pair["destination_code"],
                start_date=start_date,
                days_ahead=days_ahead,
                check_seats=check_seats,
                include_transfers=False,
            )

            items = result.get("items") or []

            attempt["status"] = "ok"
            attempt["trains_count"] = len(items)
            attempt["dates_checked"] = result.get("dates_checked")
            attempt["dates_with_trains"] = result.get("dates_with_trains")
            attempt["errors_count"] = len(result.get("errors") or [])

            code_attempts.append(attempt)

            if best_empty_result is None:
                best_empty_result = result

            if items:
                exact_found = bool(pair["exact_pair"])
                similar_used = not exact_found

                normalized_items = []
                for item in items:
                    normalized_items.append(
                        {
                            **item,
                            "used_origin_code": pair["origin_code"],
                            "used_origin_code_source": origin_candidate.get("source"),
                            "used_origin_station_id": origin_candidate.get("station_id"),
                            "used_origin_station_name": origin_candidate.get("station_name"),
                            "used_origin_station_zone_source": origin_candidate.get(
                                "station_zone_source"
                            ),
                            "used_destination_code": pair["destination_code"],
                            "used_destination_code_source": destination_candidate.get("source"),
                            "used_destination_station_id": destination_candidate.get("station_id"),
                            "used_destination_station_name": destination_candidate.get(
                                "station_name"
                            ),
                            "used_destination_station_zone_source": destination_candidate.get(
                                "station_zone_source"
                            ),
                            "similar_used": similar_used,
                        }
                    )

                message = (
                    "Найден точный маршрут по выбранным станциям."
                    if exact_found
                    else (
                        "Точный маршрут по выбранным станциям не найден. "
                        "Показаны похожие варианты через ближайшие подходящие станции."
                    )
                )

                return {
                    **result,
                    **common_payload,
                    "items": normalized_items,
                    "total": len(normalized_items),
                    "status": "found" if exact_found else "similar_found",
                    "exact_found": exact_found,
                    "similar_used": similar_used,
                    "message": message,
                    "fallback_message": None if exact_found else message,
                    "used_origin_code": pair["origin_code"],
                    "used_origin_code_source": origin_candidate.get("source"),
                    "used_origin_station_id": origin_candidate.get("station_id"),
                    "used_origin_station_name": origin_candidate.get("station_name"),
                    "used_destination_code": pair["destination_code"],
                    "used_destination_code_source": destination_candidate.get("source"),
                    "used_destination_station_id": destination_candidate.get("station_id"),
                    "used_destination_station_name": destination_candidate.get("station_name"),
                    "code_attempts": code_attempts,
                    "exact_pair_was_tried": exact_pair_was_tried,
                }

        except Exception as exc:
            attempt["status"] = "failed"
            attempt["error"] = str(exc)
            code_attempts.append(attempt)

    fallback = best_empty_result or {
        "items": [],
        "total": 0,
        "dates_checked": days_ahead,
        "dates_with_trains": 0,
        "date_summaries": [],
        "errors": [],
    }

    return {
        **fallback,
        **common_payload,
        "items": [],
        "total": 0,
        "status": "not_found",
        "exact_found": False,
        "similar_used": False,
        "message": (
            "По выбранным станциям и ближайшим подходящим станциям маршруты не найдены."
        ),
        "fallback_message": None,
        "code_attempts": code_attempts,
        "exact_pair_was_tried": exact_pair_was_tried,
    }


def get_rzd_train_stops(
    *,
    train_number: str,
    dep_date: date | str,
) -> list[dict[str, Any]]:
    """
    Получает список остановок поезда через существующий RzdClient.

    Возвращает данные уже в формате, близком к route_stops.
    """
    train_number = train_number.strip()
    if not train_number:
        raise ValueError("train_number is required")

    client = RzdClient()

    payload = client.get_train_station_list(
        train_number=train_number,
        dep_date=format_rzd_date(dep_date),
    )

    dump_rzd_debug_payload(
        label="basic_route_raw_response",
        payload=payload,
        meta={
            "stage": "get_rzd_train_stops_raw_response",
            "train_number": train_number,
            "departure_date": format_rzd_date(dep_date),
        },
    )

    stops: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str | None, str | None, str | None]] = set()

    for item in payload.get("items", []):
        # В RZD payload иногда попадают не станции, а route labels
        # вроде "[АРХАНГЕЛ Г , МОСКВА ЯР]". Не превращаем их
        # в официальные остановки маршрута.
        if is_route_label_stop(item):
            continue

        station_name = extract_rzd_stop_name(item)
        station_code = extract_rzd_stop_code(item)
        arrival_time = normalize_time_value(item.get("arrival_time"))
        departure_time = normalize_time_value(item.get("departure_time"))

        if not station_name:
            continue

        dedup_key = (
            str(station_name),
            station_code,
            arrival_time,
            departure_time,
        )

        if dedup_key in seen_keys:
            continue

        seen_keys.add(dedup_key)

        stops.append(
            {
                "stop_sequence": len(stops) + 1,
                "station_name_raw": str(station_name),
                "station_code_rzd": station_code,
                "arrival_time": arrival_time,
                "departure_time": departure_time,
                "stop_duration_minutes": parse_waiting_time_minutes(item.get("waiting_time")),
                "distance_km": parse_distance_km(item.get("distance")),
                "raw": item.get("raw"),
            }
        )

    print(
        "RZD BASIC ROUTE PARSED STOPS:",
        {
            "stops_count": len(stops),
            "first_10": stops[:10],
            "last_10": stops[-10:],
        },
    )

    return stops


def build_import_payload_from_rzd_train(
    *,
    train_number: str,
    dep_date: date | str,
    stops: list[dict[str, Any]],
    origin_code: str | None = None,
    destination_code: str | None = None,
    origin_station_name: str | None = None,
    destination_station_name: str | None = None,
    route_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """
    Превращает список остановок RZD API в payload,
    совместимый с import_route_payload.
    """
    # Защита на финальном этапе импорта: route_payload строится
    # только из официальных остановок РЖД. OSM-станции не добавляются
    # как новые route_stops, а могут быть только match-привязкой позже.
    stops = normalize_official_rzd_stops(stops)

    print(
        "RZD IMPORT NORMALIZED STOPS:",
        {
            "normalized_stops_count": len(stops),
            "first_10": stops[:10],
            "last_10": stops[-10:],
        },
    )

    if len(stops) < 2:
        raise ValueError("RZD API returned less than 2 official stops for selected train")

    first_stop = stops[0]
    last_stop = stops[-1]

    resolved_origin_name = origin_station_name or first_stop.get("station_name_raw")
    resolved_destination_name = destination_station_name or last_stop.get("station_name_raw")
    resolved_origin_code = origin_code or first_stop.get("station_code_rzd")
    resolved_destination_code = destination_code or last_stop.get("station_code_rzd")

    normalized_dep_date = format_rzd_date(dep_date)

    external_route_id = ":".join(
        [
            "rzd_api",
            str(train_number),
            normalized_dep_date,
            str(resolved_origin_code or ""),
            str(resolved_destination_code or ""),
        ]
    )

    return {
        "source_system": "rzd_api",
        "external_route_id": external_route_id,
        "train_number": train_number,
        "route_name": route_name or f"Поезд {train_number}",
        "origin_station_name": resolved_origin_name,
        "destination_station_name": resolved_destination_name,
        "origin_station_code": resolved_origin_code,
        "destination_station_code": resolved_destination_code,
        "snapshot_date": date.today().isoformat(),
        "is_active": True,
        "notes": notes or "Imported from RZD API by user request",
        "stops": [
            {
                "stop_sequence": stop["stop_sequence"],
                "station_name_raw": stop["station_name_raw"],
                "station_code_rzd": stop.get("station_code_rzd"),
                "arrival_time": stop.get("arrival_time"),
                "departure_time": stop.get("departure_time"),
                "stop_duration_minutes": stop.get("stop_duration_minutes"),
                "distance_km": stop.get("distance_km"),
            }
            for stop in stops
        ],
    }


def import_selected_rzd_train(
    *,
    train_number: str,
    dep_date: date | str,
    origin_code: str | None = None,
    destination_code: str | None = None,
    origin_station_name: str | None = None,
    destination_station_name: str | None = None,
    route_name: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """
    Главная функция для endpoint'а /api/rzd/trains/import.

    Последовательность:
    1. Получить остановки выбранного поезда через RzdClient.
    2. Собрать payload под текущую модель routes / route_stops.
    3. Передать payload в существующий import_route_payload.
    """
    train_number = train_number.strip()
    if not train_number:
        raise ValueError("train_number is required")

    stops = get_rzd_train_stops(
        train_number=train_number,
        dep_date=dep_date,
    )

    route_payload = build_import_payload_from_rzd_train(
        train_number=train_number,
        dep_date=dep_date,
        stops=stops,
        origin_code=origin_code,
        destination_code=destination_code,
        origin_station_name=origin_station_name,
        destination_station_name=destination_station_name,
        route_name=route_name,
        notes=notes,
    )

    return import_route_payload(
        route_payload,
        source_name="rzd_api",
        requested_scope="user_selected_train",
    )


def preview_selected_rzd_train(
    *,
    train_number: str,
    dep_date: date | str,
) -> dict[str, Any]:
    """
    Необязательная вспомогательная функция.

    Можно использовать позже для preview:
    показать список остановок перед импортом.
    В main.py сейчас не используется.
    """
    stops = get_rzd_train_stops(
        train_number=train_number,
        dep_date=dep_date,
    )

    return {
        "train_number": train_number,
        "dep_date": format_rzd_date(dep_date),
        "stops": stops,
        "stops_count": len(stops),
    }
