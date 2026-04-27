import json
import re
from typing import Any

from sqlalchemy import text

from app.db import engine


def normalize_station_name(value: str | None) -> str:
    if not value:
        return ""

    normalized = value.strip().upper().replace("Ё", "Е")
    normalized = re.sub(r"[^A-ZА-Я0-9]+", "", normalized)
    return normalized


def create_route_sync_run(connection, source_name: str, requested_scope: str | None) -> int:
    return connection.execute(
        text("""
            INSERT INTO route_sync_runs (
                source_name,
                requested_scope,
                status
            )
            VALUES (
                :source_name,
                :requested_scope,
                'running'
            )
            RETURNING id;
        """),
        {
            "source_name": source_name,
            "requested_scope": requested_scope,
        },
    ).scalar_one()


def finish_route_sync_run(
    connection,
    run_id: int,
    status: str,
    routes_raw_count: int | None = None,
    route_stops_raw_count: int | None = None,
    routes_core_count: int | None = None,
    route_stops_core_count: int | None = None,
    notes: str | None = None,
) -> None:
    connection.execute(
        text("""
            UPDATE route_sync_runs
            SET
                finished_at = NOW(),
                status = :status,
                routes_raw_count = :routes_raw_count,
                route_stops_raw_count = :route_stops_raw_count,
                routes_core_count = :routes_core_count,
                route_stops_core_count = :route_stops_core_count,
                notes = :notes
            WHERE id = :run_id;
        """),
        {
            "run_id": run_id,
            "status": status,
            "routes_raw_count": routes_raw_count,
            "route_stops_raw_count": route_stops_raw_count,
            "routes_core_count": routes_core_count,
            "route_stops_core_count": route_stops_core_count,
            "notes": notes,
        },
    )


def validate_station_id(connection, station_id: int | None) -> bool:
    if station_id is None:
        return False

    row = connection.execute(
        text("""
            SELECT id
            FROM stations
            WHERE id = :station_id;
        """),
        {"station_id": station_id},
    ).first()

    return row is not None


def find_station_by_rzd_mapping(connection, station_code_rzd: str | None):
    if not station_code_rzd:
        return None

    rows = connection.execute(
        text("""
            SELECT station_id
            FROM rzd_station_matches
            WHERE station_code_rzd = :station_code_rzd
              AND is_active = TRUE
            LIMIT 1;
        """),
        {"station_code_rzd": str(station_code_rzd).strip()},
    ).fetchall()

    if len(rows) == 1:
        return {
            "station_id": rows[0]._mapping["station_id"],
            "match_method": "rzd_station_match_table",
            "match_confidence": 1.00,
        }

    return None


def find_station_by_exact_code(connection, station_code_rzd: str | None):
    if not station_code_rzd:
        return None

    station_code_rzd = str(station_code_rzd).strip()
    if not station_code_rzd:
        return None

    esr_rows = connection.execute(
        text("""
            SELECT id
            FROM stations
            WHERE NULLIF(TRIM(esr_user), '') = :station_code
            ORDER BY is_visible_default DESC, id
            LIMIT 2;
        """),
        {"station_code": station_code_rzd},
    ).fetchall()

    if len(esr_rows) == 1:
        return {
            "station_id": esr_rows[0]._mapping["id"],
            "match_method": "exact_esr_code",
            "match_confidence": 0.95,
        }

    uic_rows = connection.execute(
        text("""
            SELECT id
            FROM stations
            WHERE NULLIF(TRIM(uic_ref), '') = :station_code
            ORDER BY is_visible_default DESC, id
            LIMIT 2;
        """),
        {"station_code": station_code_rzd},
    ).fetchall()

    if len(uic_rows) == 1:
        return {
            "station_id": uic_rows[0]._mapping["id"],
            "match_method": "exact_uic_code",
            "match_confidence": 0.90,
        }

    return None


def find_station_by_exact_name(connection, station_name_raw: str | None):
    normalized_name = normalize_station_name(station_name_raw)
    if not normalized_name:
        return None

    rows = connection.execute(
        text("""
            SELECT id
            FROM stations
            WHERE
                UPPER(
                    REGEXP_REPLACE(
                        REPLACE(COALESCE(name, ''), 'Ё', 'Е'),
                        '[^A-ZА-Я0-9]+',
                        '',
                        'g'
                    )
                ) = :normalized_name
            ORDER BY is_visible_default DESC, id
            LIMIT 2;
        """),
        {"normalized_name": normalized_name},
    ).fetchall()

    if len(rows) == 1:
        return {
            "station_id": rows[0]._mapping["id"],
            "match_method": "exact_name",
            "match_confidence": 0.75,
        }

    return None


def resolve_station_match(
    connection,
    station_name_raw: str | None,
    station_code_rzd: str | None,
    explicit_station_id: int | None,
):
    if explicit_station_id is not None:
        if not validate_station_id(connection, explicit_station_id):
            raise ValueError(f"station_id={explicit_station_id} не найден в таблице stations")

        return {
            "station_id": explicit_station_id,
            "match_method": "explicit_station_id",
            "match_confidence": 1.00,
        }

    matched_by_rzd_table = find_station_by_rzd_mapping(connection, station_code_rzd)
    if matched_by_rzd_table:
        return matched_by_rzd_table

    matched_by_code = find_station_by_exact_code(connection, station_code_rzd)
    if matched_by_code:
        return matched_by_code

    matched_by_name = find_station_by_exact_name(connection, station_name_raw)
    if matched_by_name:
        return matched_by_name

    return {
        "station_id": None,
        "match_method": None,
        "match_confidence": None,
    }


def build_external_route_id(payload: dict[str, Any]) -> str | None:
    external_route_id = payload.get("external_route_id")
    if external_route_id:
        return str(external_route_id)

    train_number = payload.get("train_number")
    snapshot_date = payload.get("snapshot_date")
    route_name = payload.get("route_name")

    if train_number and snapshot_date:
        return f"{train_number}:{snapshot_date}"

    if train_number and route_name:
        return f"{train_number}:{route_name}"

    return None


def prepare_stop_row(stop: dict[str, Any]) -> dict[str, Any] | None:
    station_name_raw = stop.get("station_name_raw") or stop.get("station_name")
    station_code_rzd = stop.get("station_code_rzd") or stop.get("rzd_code")
    stop_sequence = stop.get("stop_sequence")

    if isinstance(station_name_raw, list):
        return None

    if station_name_raw is not None:
        station_name_raw = str(station_name_raw).strip()

    if station_code_rzd is not None:
        station_code_rzd = str(station_code_rzd).strip()

    if not station_name_raw and not station_code_rzd:
        return None

    if stop_sequence is None:
        raise ValueError("У остановки отсутствует stop_sequence")

    distance_km = stop.get("distance_km")
    if distance_km is None:
        distance_km = stop.get("distance")

    return {
        "stop_sequence": int(stop_sequence),
        "station_name_raw": station_name_raw,
        "station_code_rzd": station_code_rzd,
        "station_id": stop.get("station_id"),
        "arrival_time": stop.get("arrival_time"),
        "departure_time": stop.get("departure_time"),
        "stop_duration_minutes": stop.get("stop_duration_minutes"),
        "distance_km": float(distance_km) if distance_km is not None else None,
    }


def prepare_route_fields(payload: dict[str, Any]) -> dict[str, Any]:
    source_stops = payload.get("stops") or []
    prepared_stops: list[dict[str, Any]] = []

    for stop in source_stops:
        prepared = prepare_stop_row(stop)
        if prepared is not None:
            prepared_stops.append(prepared)

    if len(prepared_stops) < 2:
        raise ValueError("Маршрут должен содержать минимум 2 валидные остановки")

    sorted_stops = sorted(prepared_stops, key=lambda item: item["stop_sequence"])

    seen_sequences = set()
    for stop in sorted_stops:
        sequence = stop["stop_sequence"]
        if sequence in seen_sequences:
            raise ValueError(f"Дублирующийся stop_sequence={sequence}")
        seen_sequences.add(sequence)

    origin_stop = sorted_stops[0]
    destination_stop = sorted_stops[-1]

    route_name = payload.get("route_name")
    if not route_name:
        route_name = f"{origin_stop['station_name_raw']} — {destination_stop['station_name_raw']}"

    return {
        "source_system": payload.get("source_system") or "manual",
        "external_route_id": build_external_route_id(payload),
        "train_number": payload.get("train_number"),
        "route_name": route_name,
        "origin_station_name": payload.get("origin_station_name") or origin_stop["station_name_raw"],
        "destination_station_name": payload.get("destination_station_name") or destination_stop["station_name_raw"],
        "origin_station_code": payload.get("origin_station_code") or origin_stop.get("station_code_rzd"),
        "destination_station_code": payload.get("destination_station_code") or destination_stop.get("station_code_rzd"),
        "snapshot_date": payload.get("snapshot_date"),
        "operates_from": payload.get("operates_from"),
        "operates_to": payload.get("operates_to"),
        "is_active": payload.get("is_active", True),
        "notes": payload.get("notes"),
        "stops": sorted_stops,
    }


def replace_raw_route_if_exists(connection, source_system: str, external_route_id: str | None, snapshot_date):
    if not external_route_id or not snapshot_date:
        return

    existing_raw_id = connection.execute(
        text("""
            SELECT id
            FROM rzd_routes_raw
            WHERE source_system = :source_system
              AND external_route_id = :external_route_id
              AND snapshot_date = :snapshot_date
            LIMIT 1;
        """),
        {
            "source_system": source_system,
            "external_route_id": external_route_id,
            "snapshot_date": snapshot_date,
        },
    ).scalar_one_or_none()

    if existing_raw_id is not None:
        connection.execute(
            text("""
                DELETE FROM rzd_routes_raw
                WHERE id = :raw_id;
            """),
            {"raw_id": existing_raw_id},
        )


def replace_core_route_if_exists(connection, source_system: str, external_route_id: str | None, snapshot_date):
    if not external_route_id or not snapshot_date:
        return None

    existing_route_id = connection.execute(
        text("""
            SELECT id
            FROM routes
            WHERE source_system = :source_system
              AND external_route_id = :external_route_id
              AND snapshot_date = :snapshot_date
            LIMIT 1;
        """),
        {
            "source_system": source_system,
            "external_route_id": external_route_id,
            "snapshot_date": snapshot_date,
        },
    ).scalar_one_or_none()

    if existing_route_id is not None:
        connection.execute(
            text("""
                DELETE FROM routes
                WHERE id = :route_id;
            """),
            {"route_id": existing_route_id},
        )

    return existing_route_id


def import_route_payload(
    payload: dict[str, Any],
    *,
    source_name: str = "manual_api",
    requested_scope: str | None = None,
) -> dict[str, Any]:
    prepared = prepare_route_fields(payload)
    raw_payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    with engine.begin() as connection:
        run_id = create_route_sync_run(
            connection,
            source_name=source_name,
            requested_scope=requested_scope,
        )

    try:
        with engine.begin() as connection:
            replace_raw_route_if_exists(
                connection=connection,
                source_system=prepared["source_system"],
                external_route_id=prepared["external_route_id"],
                snapshot_date=prepared["snapshot_date"],
            )

            raw_route_id = connection.execute(
                text("""
                    INSERT INTO rzd_routes_raw (
                        source_system,
                        external_route_id,
                        train_number,
                        route_name,
                        origin_station_name,
                        destination_station_name,
                        snapshot_date,
                        raw_payload
                    )
                    VALUES (
                        :source_system,
                        :external_route_id,
                        :train_number,
                        :route_name,
                        :origin_station_name,
                        :destination_station_name,
                        :snapshot_date,
                        CAST(:raw_payload AS JSONB)
                    )
                    RETURNING id;
                """),
                {
                    "source_system": prepared["source_system"],
                    "external_route_id": prepared["external_route_id"],
                    "train_number": prepared["train_number"],
                    "route_name": prepared["route_name"],
                    "origin_station_name": prepared["origin_station_name"],
                    "destination_station_name": prepared["destination_station_name"],
                    "snapshot_date": prepared["snapshot_date"],
                    "raw_payload": raw_payload_json,
                },
            ).scalar_one()

            for stop in prepared["stops"]:
                connection.execute(
                    text("""
                        INSERT INTO rzd_route_stops_raw (
                            route_raw_id,
                            stop_sequence,
                            station_name_raw,
                            station_code_rzd,
                            arrival_time,
                            departure_time,
                            stop_duration_minutes,
                            distance_km,
                            raw_payload
                        )
                        VALUES (
                            :route_raw_id,
                            :stop_sequence,
                            :station_name_raw,
                            :station_code_rzd,
                            :arrival_time,
                            :departure_time,
                            :stop_duration_minutes,
                            :distance_km,
                            CAST(:raw_payload AS JSONB)
                        );
                    """),
                    {
                        "route_raw_id": raw_route_id,
                        "stop_sequence": stop["stop_sequence"],
                        "station_name_raw": stop["station_name_raw"],
                        "station_code_rzd": stop.get("station_code_rzd"),
                        "arrival_time": stop.get("arrival_time"),
                        "departure_time": stop.get("departure_time"),
                        "stop_duration_minutes": stop.get("stop_duration_minutes"),
                        "distance_km": stop.get("distance_km"),
                        "raw_payload": json.dumps(stop, ensure_ascii=False, default=str),
                    },
                )

            replace_core_route_if_exists(
                connection=connection,
                source_system=prepared["source_system"],
                external_route_id=prepared["external_route_id"],
                snapshot_date=prepared["snapshot_date"],
            )

            route_id = connection.execute(
                text("""
                    INSERT INTO routes (
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
                        updated_at
                    )
                    VALUES (
                        :source_system,
                        :external_route_id,
                        :train_number,
                        :route_name,
                        :origin_station_name,
                        :destination_station_name,
                        :origin_station_code,
                        :destination_station_code,
                        :snapshot_date,
                        :operates_from,
                        :operates_to,
                        :is_active,
                        :notes,
                        NOW()
                    )
                    RETURNING id;
                """),
                {
                    "source_system": prepared["source_system"],
                    "external_route_id": prepared["external_route_id"],
                    "train_number": prepared["train_number"],
                    "route_name": prepared["route_name"],
                    "origin_station_name": prepared["origin_station_name"],
                    "destination_station_name": prepared["destination_station_name"],
                    "origin_station_code": prepared["origin_station_code"],
                    "destination_station_code": prepared["destination_station_code"],
                    "snapshot_date": prepared["snapshot_date"],
                    "operates_from": prepared["operates_from"],
                    "operates_to": prepared["operates_to"],
                    "is_active": prepared["is_active"],
                    "notes": prepared["notes"],
                },
            ).scalar_one()

            matched_stops_count = 0
            unresolved_stops_count = 0

            for index, stop in enumerate(prepared["stops"]):
                match_result = resolve_station_match(
                    connection=connection,
                    station_name_raw=stop.get("station_name_raw"),
                    station_code_rzd=stop.get("station_code_rzd"),
                    explicit_station_id=stop.get("station_id"),
                )

                if match_result["station_id"] is not None:
                    matched_stops_count += 1
                else:
                    unresolved_stops_count += 1

                connection.execute(
                    text("""
                        INSERT INTO route_stops (
                            route_id,
                            stop_sequence,
                            station_name_raw,
                            station_code_rzd,
                            station_id,
                            arrival_time,
                            departure_time,
                            stop_duration_minutes,
                            distance_km,
                            is_origin,
                            is_destination,
                            match_method,
                            match_confidence
                        )
                        VALUES (
                            :route_id,
                            :stop_sequence,
                            :station_name_raw,
                            :station_code_rzd,
                            :station_id,
                            :arrival_time,
                            :departure_time,
                            :stop_duration_minutes,
                            :distance_km,
                            :is_origin,
                            :is_destination,
                            :match_method,
                            :match_confidence
                        );
                    """),
                    {
                        "route_id": route_id,
                        "stop_sequence": stop["stop_sequence"],
                        "station_name_raw": stop["station_name_raw"],
                        "station_code_rzd": stop.get("station_code_rzd"),
                        "station_id": match_result["station_id"],
                        "arrival_time": stop.get("arrival_time"),
                        "departure_time": stop.get("departure_time"),
                        "stop_duration_minutes": stop.get("stop_duration_minutes"),
                        "distance_km": stop.get("distance_km"),
                        "is_origin": index == 0,
                        "is_destination": index == len(prepared["stops"]) - 1,
                        "match_method": match_result["match_method"],
                        "match_confidence": match_result["match_confidence"],
                    },
                )

        with engine.begin() as connection:
            finish_route_sync_run(
                connection=connection,
                run_id=run_id,
                status="finished",
                routes_raw_count=1,
                route_stops_raw_count=len(prepared["stops"]),
                routes_core_count=1,
                route_stops_core_count=len(prepared["stops"]),
                notes=(
                    f"Маршрут импортирован успешно.\n"
                    f"route_id={route_id}\n"
                    f"matched_stops_count={matched_stops_count}\n"
                    f"unresolved_stops_count={unresolved_stops_count}"
                ),
            )

        return {
            "status": "created",
            "route_id": route_id,
            "route_sync_run_id": run_id,
            "matched_stops_count": matched_stops_count,
            "unresolved_stops_count": unresolved_stops_count,
            "message": "Маршрут успешно импортирован",
        }

    except Exception as exc:
        with engine.begin() as connection:
            finish_route_sync_run(
                connection=connection,
                run_id=run_id,
                status="failed",
                notes=f"Ошибка маршрутного импорта: {exc}",
            )
        raise