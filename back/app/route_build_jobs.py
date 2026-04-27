import json
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text

from app.db import engine
from app.matcher_logging import build_exception_payload, log_event
from app.route_graph_matcher import (
    build_candidates_for_stop,
    build_scope_key,
    infer_route_region_codes,
    load_global_station_catalog,
    load_route,
    resolve_route_for_map,
)

StageCallback = Callable[[int, str, dict[str, Any] | None], None]

STAGE_LABELS = {
    "queued": "Задача поставлена в очередь",
    "loading": "Загрузка маршрута и остановок",
    "network": "Подготовка topology graph маршрута",
    "candidates": "Подбор кандидатов для остановок",
    "solving": "Сопоставление остановок по графу и расстоянию",
    "geometry": "Построение геометрии маршрута",
    "saving": "Сохранение результата",
    "done": "Маршрут построен",
    "failed": "Построение завершилось ошибкой",
}

BASE_DIR = Path(__file__).resolve().parent.parent


def _to_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_dict(row) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row._mapping)


def _normalize_uuid_string(value: Any) -> str | None:
    if value is None:
        return None

    try:
        return str(uuid.UUID(str(value)))
    except Exception:
        return None


def _unique_non_empty(values: list[str | None]) -> list[str]:
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


def ensure_route_build_jobs_table() -> None:
    with engine.begin() as connection:
        connection.execute(
            text("""
                CREATE TABLE IF NOT EXISTS route_build_jobs (
                    id UUID PRIMARY KEY,
                    route_id BIGINT NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
                    status TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    stage_code TEXT,
                    stage_label TEXT,
                    detail JSONB,
                    result JSONB,
                    error_text TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    finished_at TIMESTAMPTZ
                );
            """)
        )

        connection.execute(
            text("""
                CREATE INDEX IF NOT EXISTS idx_route_build_jobs_route_id
                ON route_build_jobs(route_id);
            """)
        )

        connection.execute(
            text("""
                CREATE INDEX IF NOT EXISTS idx_route_build_jobs_status
                ON route_build_jobs(status);
            """)
        )


def ensure_route_exists(route_id: int) -> None:
    with engine.connect() as connection:
        exists = connection.execute(
            text("""
                SELECT id
                FROM routes
                WHERE id = :route_id
                LIMIT 1;
            """),
            {"route_id": route_id},
        ).scalar_one_or_none()

    if exists is None:
        raise ValueError("Route not found")


def _topology_tables_exist() -> bool:
    query = text("""
        SELECT
            to_regclass('public.rail_graph_nodes') AS nodes_table,
            to_regclass('public.rail_graph_edges') AS edges_table,
            to_regclass('public.station_graph_links') AS links_table;
    """)

    with engine.connect() as connection:
        row = connection.execute(query).first()

    if row is None:
        return False

    item = dict(row._mapping)
    return bool(item.get("nodes_table")) and bool(item.get("edges_table")) and bool(item.get("links_table"))


def _derive_route_scope(route_id: int) -> dict[str, Any]:
    payload = load_route(route_id)
    stops = payload["stops"]

    stored_region_codes = _unique_non_empty(
        [
            stop.get("stored_station_region_code")
            for stop in stops
            if stop.get("stored_station_visible") and stop.get("stored_station_region_code")
        ]
    )

    if stored_region_codes:
        return {
            "route_id": route_id,
            "region_codes": stored_region_codes,
            "scope_key": build_scope_key(stored_region_codes),
            "source": "stored_visible_station_regions",
        }

    catalog_payload = load_global_station_catalog(
        diagnostics=None,
        logger_context={"route_id": route_id, "scope_probe": True},
    )
    candidates_per_stop = [
        build_candidates_for_stop(stop, catalog_payload)
        for stop in stops
    ]
    inferred_region_codes = infer_route_region_codes(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
        diagnostics=None,
        logger_context={"route_id": route_id, "scope_probe": True},
    )

    return {
        "route_id": route_id,
        "region_codes": inferred_region_codes,
        "scope_key": build_scope_key(inferred_region_codes),
        "source": "inferred_route_regions",
    }


def _get_scope_topology_stats(scope_key: str) -> dict[str, int]:
    if not _topology_tables_exist():
        return {
            "nodes_count": 0,
            "edges_count": 0,
            "station_links_count": 0,
        }

    query = text("""
        SELECT
            (SELECT COUNT(*) FROM rail_graph_nodes WHERE scope_key = :scope_key) AS nodes_count,
            (SELECT COUNT(*) FROM rail_graph_edges WHERE scope_key = :scope_key) AS edges_count,
            (SELECT COUNT(*) FROM station_graph_links WHERE scope_key = :scope_key) AS station_links_count;
    """)

    with engine.connect() as connection:
        row = connection.execute(query, {"scope_key": scope_key}).first()

    if row is None:
        return {
            "nodes_count": 0,
            "edges_count": 0,
            "station_links_count": 0,
        }

    item = dict(row._mapping)
    return {
        "nodes_count": int(item.get("nodes_count") or 0),
        "edges_count": int(item.get("edges_count") or 0),
        "station_links_count": int(item.get("station_links_count") or 0),
    }


def _scope_topology_ready(scope_key: str) -> bool:
    stats = _get_scope_topology_stats(scope_key)
    return (
        stats["nodes_count"] > 0
        and stats["edges_count"] > 0
        and stats["station_links_count"] > 0
    )


def _run_topology_builder(route_id: int) -> None:
    cmd = [
        sys.executable,
        "-m",
        "scripts.build_route_scope_topology",
        "--route-id",
        str(route_id),
    ]

    completed = subprocess.run(
        cmd,
        cwd=str(BASE_DIR),
        capture_output=True,
        text=True,
    )

    if completed.returncode == 0:
        return

    stdout_text = (completed.stdout or "").strip()
    stderr_text = (completed.stderr or "").strip()

    if len(stdout_text) > 6000:
        stdout_text = stdout_text[-6000:]
    if len(stderr_text) > 6000:
        stderr_text = stderr_text[-6000:]

    raise RuntimeError(
        "Не удалось подготовить topology graph для маршрута. "
        f"route_id={route_id}, returncode={completed.returncode}"
        f"\nstdout:\n{stdout_text}"
        f"\nstderr:\n{stderr_text}"
    )


def ensure_route_topology_built(route_id: int) -> dict[str, Any]:
    ensure_route_exists(route_id)

    scope_payload = _derive_route_scope(route_id)
    scope_key = scope_payload["scope_key"]
    region_codes = scope_payload["region_codes"]

    if not scope_key or not region_codes:
        raise RuntimeError(f"Не удалось определить scope topology для route_id={route_id}")

    before_stats = _get_scope_topology_stats(scope_key)

    if (
        before_stats["nodes_count"] > 0
        and before_stats["edges_count"] > 0
        and before_stats["station_links_count"] > 0
    ):
        log_event(
            "info",
            "route_topology_already_ready",
            route_id=route_id,
            scope_key=scope_key,
            region_codes=region_codes,
            stats=before_stats,
            scope_source=scope_payload["source"],
        )
        return {
            "route_id": route_id,
            "scope_key": scope_key,
            "region_codes": region_codes,
            "rebuilt": False,
            "stats": before_stats,
            "scope_source": scope_payload["source"],
        }

    log_event(
        "info",
        "route_topology_build_required",
        route_id=route_id,
        scope_key=scope_key,
        region_codes=region_codes,
        stats_before=before_stats,
        scope_source=scope_payload["source"],
    )

    _run_topology_builder(route_id)

    after_stats = _get_scope_topology_stats(scope_key)
    if not _scope_topology_ready(scope_key):
        raise RuntimeError(
            "Topology builder завершился, но scope остался неполным. "
            f"route_id={route_id}, scope_key={scope_key}, stats={after_stats}"
        )

    log_event(
        "info",
        "route_topology_ready_after_build",
        route_id=route_id,
        scope_key=scope_key,
        region_codes=region_codes,
        stats=after_stats,
        scope_source=scope_payload["source"],
    )

    return {
        "route_id": route_id,
        "scope_key": scope_key,
        "region_codes": region_codes,
        "rebuilt": True,
        "stats": after_stats,
        "scope_source": scope_payload["source"],
    }


def create_route_build_job(route_id: int) -> dict[str, Any]:
    ensure_route_build_jobs_table()
    ensure_route_exists(route_id)

    job_id = str(uuid.uuid4())

    with engine.begin() as connection:
        connection.execute(
            text("""
                INSERT INTO route_build_jobs (
                    id,
                    route_id,
                    status,
                    progress_percent,
                    stage_code,
                    stage_label
                )
                VALUES (
                    :id,
                    :route_id,
                    'queued',
                    0,
                    'queued',
                    :stage_label
                );
            """),
            {
                "id": job_id,
                "route_id": route_id,
                "stage_label": STAGE_LABELS["queued"],
            },
        )

    log_event(
        "info",
        "route_build_job_created",
        job_id=job_id,
        route_id=route_id,
    )

    job = get_route_build_job(job_id)
    if job is None:
        raise RuntimeError("Не удалось создать route build job")

    return job


def get_route_build_job(job_id: str) -> dict[str, Any] | None:
    ensure_route_build_jobs_table()

    normalized_job_id = _normalize_uuid_string(job_id)
    if normalized_job_id is None:
        return None

    with engine.connect() as connection:
        row = connection.execute(
            text("""
                SELECT
                    id,
                    route_id,
                    status,
                    progress_percent,
                    stage_code,
                    stage_label,
                    detail,
                    result,
                    error_text,
                    created_at,
                    started_at,
                    finished_at
                FROM route_build_jobs
                WHERE id = :job_id;
            """),
            {"job_id": normalized_job_id},
        ).first()

    return _row_to_dict(row)


def update_route_build_job(
    job_id: str,
    *,
    status: str | None = None,
    progress_percent: int | None = None,
    stage_code: str | None = None,
    stage_label: str | None = None,
    detail: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error_text: str | None = None,
    clear_error: bool = False,
    set_started: bool = False,
    set_finished: bool = False,
) -> None:
    ensure_route_build_jobs_table()

    normalized_job_id = _normalize_uuid_string(job_id)
    if normalized_job_id is None:
        raise ValueError("Invalid route build job id")

    set_parts: list[str] = []
    params: dict[str, Any] = {"job_id": normalized_job_id}

    if status is not None:
        set_parts.append("status = :status")
        params["status"] = status

    if progress_percent is not None:
        set_parts.append("progress_percent = :progress_percent")
        params["progress_percent"] = int(max(0, min(100, progress_percent)))

    if stage_code is not None:
        set_parts.append("stage_code = :stage_code")
        params["stage_code"] = stage_code

    if stage_label is not None:
        set_parts.append("stage_label = :stage_label")
        params["stage_label"] = stage_label

    if detail is not None:
        set_parts.append("detail = CAST(:detail AS JSONB)")
        params["detail"] = _to_json(detail)

    if result is not None:
        set_parts.append("result = CAST(:result AS JSONB)")
        params["result"] = _to_json(result)

    if error_text is not None:
        set_parts.append("error_text = :error_text")
        params["error_text"] = error_text

    if clear_error:
        set_parts.append("error_text = NULL")

    if set_started:
        set_parts.append("started_at = COALESCE(started_at, NOW())")

    if set_finished:
        set_parts.append("finished_at = NOW()")

    if not set_parts:
        return

    sql = f"""
        UPDATE route_build_jobs
        SET {", ".join(set_parts)}
        WHERE id = :job_id;
    """

    with engine.begin() as connection:
        connection.execute(text(sql), params)


def finish_route_build_job_success(
    job_id: str,
    *,
    result: dict[str, Any],
    detail: dict[str, Any] | None = None,
) -> None:
    update_route_build_job(
        job_id,
        status="done",
        progress_percent=100,
        stage_code="done",
        stage_label=STAGE_LABELS["done"],
        detail=detail,
        result=result,
        clear_error=True,
        set_finished=True,
    )


def finish_route_build_job_failed(
    job_id: str,
    *,
    error_text: str,
    detail: dict[str, Any] | None = None,
) -> None:
    update_route_build_job(
        job_id,
        status="failed",
        progress_percent=100,
        stage_code="failed",
        stage_label=STAGE_LABELS["failed"],
        detail=detail,
        error_text=error_text,
        set_finished=True,
    )


def _progress_callback_factory(job_id: str, route_id: int) -> StageCallback:
    last_stage = {"code": None, "percent": None}

    def callback(
        progress_percent: int,
        stage_code: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        update_route_build_job(
            job_id,
            status="running",
            progress_percent=progress_percent,
            stage_code=stage_code,
            stage_label=STAGE_LABELS.get(stage_code, stage_code),
            detail=detail,
        )

        should_log = (
            stage_code != last_stage["code"]
            or last_stage["percent"] is None
            or abs(int(progress_percent) - int(last_stage["percent"])) >= 5
        )

        if should_log:
            log_event(
                "info",
                "route_build_job_progress",
                job_id=job_id,
                route_id=route_id,
                stage_code=stage_code,
                progress_percent=int(progress_percent),
                detail=detail,
            )
            last_stage["code"] = stage_code
            last_stage["percent"] = progress_percent

    return callback


def run_route_build_job(job_id: str) -> None:
    ensure_route_build_jobs_table()

    job = get_route_build_job(job_id)
    if job is None:
        return

    route_id = int(job["route_id"])
    progress_callback = _progress_callback_factory(job_id, route_id)

    log_event(
        "info",
        "route_build_job_started",
        job_id=job_id,
        route_id=route_id,
    )

    try:
        update_route_build_job(
            job_id,
            status="running",
            progress_percent=5,
            stage_code="loading",
            stage_label=STAGE_LABELS["loading"],
            detail={"route_id": route_id},
            clear_error=True,
            set_started=True,
        )

        update_route_build_job(
            job_id,
            status="running",
            progress_percent=18,
            stage_code="network",
            stage_label=STAGE_LABELS["network"],
            detail={"route_id": route_id, "message": "Проверяем topology graph маршрута"},
        )

        topology_info = ensure_route_topology_built(route_id)

        update_route_build_job(
            job_id,
            status="running",
            progress_percent=28,
            stage_code="network",
            stage_label=STAGE_LABELS["network"],
            detail={
                "route_id": route_id,
                "message": (
                    "Topology graph готов"
                    if not topology_info.get("rebuilt")
                    else "Topology graph построен"
                ),
                "scope_key": topology_info.get("scope_key"),
                "region_codes": topology_info.get("region_codes"),
                "stats": topology_info.get("stats"),
            },
        )

        result = resolve_route_for_map(
            route_id,
            persist=True,
            progress_callback=progress_callback,
        )

        summary = result.get("summary") or {
            "route_id": route_id,
            "stops_count": len(result.get("stops") or []),
            "matched_stops_count": sum(
                1 for stop in (result.get("stops") or []) if stop.get("station_id") is not None
            ),
            "unresolved_stops_count": sum(
                1 for stop in (result.get("stops") or []) if stop.get("station_id") is None
            ),
            "geometry_ready": result.get("geometry") is not None,
        }

        update_route_build_job(
            job_id,
            status="running",
            progress_percent=95,
            stage_code="saving",
            stage_label=STAGE_LABELS["saving"],
            detail=summary,
        )

        finish_route_build_job_success(
            job_id,
            result=result,
            detail=summary,
        )

        log_event(
            "info",
            "route_build_job_finished",
            job_id=job_id,
            route_id=route_id,
            summary=summary,
        )

    except Exception as exc:
        exception_payload = build_exception_payload(exc)
        detail = {
            "route_id": route_id,
            "exception": exception_payload,
        }

        finish_route_build_job_failed(
            job_id,
            error_text=str(exc),
            detail=detail,
        )

        log_event(
            "error",
            "route_build_job_failed",
            job_id=job_id,
            route_id=route_id,
            exception=exception_payload,
        )


def start_route_build_job(job_id: str) -> None:
    worker = threading.Thread(
        target=run_route_build_job,
        args=(job_id,),
        daemon=True,
    )
    worker.start()


def get_route_build_job_result(job_id: str) -> dict[str, Any] | None:
    ensure_route_build_jobs_table()

    job = get_route_build_job(job_id)
    if job is None:
        return None

    return {
        "job_id": job["id"],
        "route_id": job["route_id"],
        "status": job["status"],
        "result": job.get("result"),
        "error_text": job.get("error_text"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }