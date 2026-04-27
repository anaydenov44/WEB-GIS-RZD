import threading
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import text

from app.db import engine
from scripts.build_route_scope_topology import (
    build_scope_key,
    build_topology,
    clear_scope,
    ensure_tables,
)


_TOPOLOGY_JOBS: dict[str, dict[str, Any]] = {}
_TOPOLOGY_ACTIVE_SCOPE_JOBS: dict[str, str] = {}
_TOPOLOGY_JOBS_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalize_region_codes(region_codes: list[str] | str | None) -> list[str]:
    if region_codes is None:
        return []

    if isinstance(region_codes, str):
        raw_items = region_codes.split(",")
    else:
        raw_items = region_codes

    result: list[str] = []
    seen = set()

    for item in raw_items:
        code = str(item or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)

    return result


def get_topology_status(region_codes: list[str] | str | None) -> dict[str, Any]:
    normalized_codes = normalize_region_codes(region_codes)
    scope_key = build_scope_key(normalized_codes) if normalized_codes else ""

    if not normalized_codes:
        return {
            "region_codes": [],
            "scope_key": "",
            "nodes_count": 0,
            "edges_count": 0,
            "station_links_count": 0,
            "is_built": False,
            "status": "empty_scope",
        }

    query = text("""
        SELECT
            (SELECT COUNT(*) FROM rail_graph_nodes WHERE scope_key = :scope_key) AS nodes_count,
            (SELECT COUNT(*) FROM rail_graph_edges WHERE scope_key = :scope_key) AS edges_count,
            (SELECT COUNT(*) FROM station_graph_links WHERE scope_key = :scope_key) AS station_links_count;
    """)

    try:
        with engine.connect() as connection:
            row = connection.execute(query, {"scope_key": scope_key}).first()

        if row is None:
            nodes_count = 0
            edges_count = 0
            station_links_count = 0
        else:
            mapping = row._mapping
            nodes_count = int(mapping["nodes_count"] or 0)
            edges_count = int(mapping["edges_count"] or 0)
            station_links_count = int(mapping["station_links_count"] or 0)

    except Exception as exc:
        return {
            "region_codes": normalized_codes,
            "scope_key": scope_key,
            "nodes_count": 0,
            "edges_count": 0,
            "station_links_count": 0,
            "is_built": False,
            "status": "error",
            "error": str(exc),
        }

    is_built = nodes_count > 0 and edges_count > 0 and station_links_count > 0

    return {
        "region_codes": normalized_codes,
        "scope_key": scope_key,
        "nodes_count": nodes_count,
        "edges_count": edges_count,
        "station_links_count": station_links_count,
        "is_built": is_built,
        "status": "built" if is_built else "missing",
    }


def _update_job(job_id: str, **updates: Any) -> None:
    with _TOPOLOGY_JOBS_LOCK:
        job = _TOPOLOGY_JOBS.get(job_id)
        if job is None:
            return
        job.update(updates)


def get_topology_build_job(job_id: str) -> dict[str, Any] | None:
    with _TOPOLOGY_JOBS_LOCK:
        job = _TOPOLOGY_JOBS.get(job_id)
        if job is None:
            return None
        return dict(job)


def _run_topology_build_job(job_id: str) -> None:
    job = get_topology_build_job(job_id)
    if job is None:
        return

    region_codes = job["region_codes"]
    scope_key = job["scope_key"]
    force_rebuild = bool(job.get("force_rebuild"))

    try:
        _update_job(
            job_id,
            status="running",
            progress_percent=8,
            stage_code="checking",
            stage_label="Проверяем состояние topology graph",
            started_at=_now_iso(),
        )

        current_status = get_topology_status(region_codes)

        if current_status.get("is_built") and not force_rebuild:
            _update_job(
                job_id,
                status="done",
                progress_percent=100,
                stage_code="done",
                stage_label="Topology graph уже построен",
                finished_at=_now_iso(),
                result=current_status,
            )
            return

        _update_job(
            job_id,
            progress_percent=15,
            stage_code="ensure_tables",
            stage_label="Проверяем таблицы topology graph",
        )
        ensure_tables()

        _update_job(
            job_id,
            progress_percent=25,
            stage_code="clear_scope",
            stage_label="Очищаем старый scope topology graph",
        )
        clear_scope(scope_key)

        _update_job(
            job_id,
            progress_percent=40,
            stage_code="build_topology",
            stage_label="Строим topology graph для выбранных округов",
        )

        def progress_callback(
            percent: int,
            stage_code: str,
            stage_label: str,
            detail: dict[str, Any] | None = None,
        ) -> None:
            _update_job(
                job_id,
                progress_percent=percent,
                stage_code=stage_code,
                stage_label=stage_label,
                detail=detail or {},
            )

        started = time.perf_counter()
        stats = build_topology(
            scope_key,
            region_codes,
            progress_callback=progress_callback,
        )
        duration_seconds = round(time.perf_counter() - started, 3)

        final_status = get_topology_status(region_codes)

        _update_job(
            job_id,
            status="done",
            progress_percent=100,
            stage_code="done",
            stage_label="Topology graph построен",
            finished_at=_now_iso(),
            result={
                **final_status,
                "build_stats": stats,
                "duration_seconds": duration_seconds,
            },
        )

    except Exception as exc:
        _update_job(
            job_id,
            status="failed",
            progress_percent=100,
            stage_code="failed",
            stage_label="Ошибка построения topology graph",
            error_text=str(exc),
            finished_at=_now_iso(),
        )
    finally:
        with _TOPOLOGY_JOBS_LOCK:
            existing_job_id = _TOPOLOGY_ACTIVE_SCOPE_JOBS.get(scope_key)
            if existing_job_id == job_id:
                _TOPOLOGY_ACTIVE_SCOPE_JOBS.pop(scope_key, None)


def start_topology_build_job(
    *,
    region_codes: list[str] | str,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    normalized_codes = normalize_region_codes(region_codes)
    if not normalized_codes:
        raise ValueError("region_codes is required")

    scope_key = build_scope_key(normalized_codes)

    with _TOPOLOGY_JOBS_LOCK:
        existing_job_id = _TOPOLOGY_ACTIVE_SCOPE_JOBS.get(scope_key)

        if existing_job_id:
            existing_job = _TOPOLOGY_JOBS.get(existing_job_id)
            if existing_job and existing_job.get("status") in {"queued", "running"}:
                return dict(existing_job)

    job_id = str(uuid.uuid4())

    job = {
        "id": job_id,
        "region_codes": normalized_codes,
        "scope_key": scope_key,
        "force_rebuild": bool(force_rebuild),
        "status": "queued",
        "progress_percent": 0,
        "stage_code": "queued",
        "stage_label": "Задача поставлена в очередь",
        "error_text": None,
        "result": None,
        "created_at": _now_iso(),
        "started_at": None,
        "finished_at": None,
    }

    with _TOPOLOGY_JOBS_LOCK:
        existing_job_id = _TOPOLOGY_ACTIVE_SCOPE_JOBS.get(scope_key)

        if existing_job_id:
            existing_job = _TOPOLOGY_JOBS.get(existing_job_id)
            if existing_job and existing_job.get("status") in {"queued", "running"}:
                return dict(existing_job)

        _TOPOLOGY_JOBS[job_id] = job
        _TOPOLOGY_ACTIVE_SCOPE_JOBS[scope_key] = job_id

    thread = threading.Thread(
        target=_run_topology_build_job,
        args=(job_id,),
        daemon=True,
    )
    thread.start()

    return dict(job)
