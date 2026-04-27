import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.db import engine
from app.matcher_logging import log_event
from app.route_graph_matcher import (
    build_candidates_for_stop,
    build_scope_key,
    infer_route_region_codes,
    load_global_station_catalog,
    load_route,
)

DEFAULT_MIN_NODES = 100
DEFAULT_MIN_EDGES = 100
DEFAULT_MIN_LINKS = 20


@dataclass
class ScopeTopologyStatus:
    scope_key: str
    region_codes: list[str]
    nodes_count: int
    edges_count: int
    station_links_count: int
    ready: bool


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _build_script_path() -> str:
    return os.path.join(_repo_root(), "scripts", "build_route_scope_topology.py")


def resolve_route_scope_regions(route_id: int) -> dict[str, Any]:
    payload = load_route(route_id)
    stops = payload["stops"]

    catalog_payload = load_global_station_catalog()

    candidates_per_stop = [
        build_candidates_for_stop(stop, catalog_payload)
        for stop in stops
    ]

    inferred_region_codes = infer_route_region_codes(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
        diagnostics={},
        logger_context={"route_id": route_id},
    )

    scope_key = build_scope_key(inferred_region_codes)

    return {
        "route": payload["route"],
        "stops": stops,
        "region_codes": inferred_region_codes,
        "scope_key": scope_key,
    }


def get_scope_topology_status(
    *,
    scope_key: str,
    region_codes: list[str] | None = None,
    min_nodes: int = DEFAULT_MIN_NODES,
    min_edges: int = DEFAULT_MIN_EDGES,
    min_links: int = DEFAULT_MIN_LINKS,
) -> ScopeTopologyStatus:
    region_codes = region_codes or []

    query = text("""
        SELECT
            (SELECT COUNT(*) FROM rail_graph_nodes WHERE scope_key = :scope_key) AS nodes_count,
            (SELECT COUNT(*) FROM rail_graph_edges WHERE scope_key = :scope_key) AS edges_count,
            (SELECT COUNT(*) FROM station_graph_links WHERE scope_key = :scope_key) AS station_links_count
    """)

    with engine.connect() as connection:
        row = connection.execute(query, {"scope_key": scope_key}).first()

    if row is None:
        nodes_count = 0
        edges_count = 0
        station_links_count = 0
    else:
        nodes_count = int(row._mapping["nodes_count"] or 0)
        edges_count = int(row._mapping["edges_count"] or 0)
        station_links_count = int(row._mapping["station_links_count"] or 0)

    ready = (
        nodes_count >= min_nodes
        and edges_count >= min_edges
        and station_links_count >= min_links
    )

    return ScopeTopologyStatus(
        scope_key=scope_key,
        region_codes=region_codes,
        nodes_count=nodes_count,
        edges_count=edges_count,
        station_links_count=station_links_count,
        ready=ready,
    )


def ensure_route_scope_topology(
    route_id: int,
    *,
    force_rebuild: bool = False,
    python_executable: str | None = None,
) -> dict[str, Any]:
    scope_info = resolve_route_scope_regions(route_id)
    route = scope_info["route"]
    region_codes = scope_info["region_codes"]
    scope_key = scope_info["scope_key"]

    before_status = get_scope_topology_status(
        scope_key=scope_key,
        region_codes=region_codes,
    )

    if before_status.ready and not force_rebuild:
        result = {
            "route_id": route_id,
            "train_number": route.get("train_number"),
            "route_name": route.get("route_name"),
            "scope_key": scope_key,
            "region_codes": region_codes,
            "rebuild_triggered": False,
            "status_before": {
                "nodes_count": before_status.nodes_count,
                "edges_count": before_status.edges_count,
                "station_links_count": before_status.station_links_count,
                "ready": before_status.ready,
            },
            "status_after": {
                "nodes_count": before_status.nodes_count,
                "edges_count": before_status.edges_count,
                "station_links_count": before_status.station_links_count,
                "ready": before_status.ready,
            },
        }

        log_event(
            "info",
            "route_scope_topology_already_ready",
            **result,
        )
        return result

    script_path = _build_script_path()
    python_bin = python_executable or sys.executable

    command = [
        python_bin,
        script_path,
        "--route-id",
        str(route_id),
    ]

    log_event(
        "info",
        "route_scope_topology_build_started",
        route_id=route_id,
        train_number=route.get("train_number"),
        route_name=route.get("route_name"),
        scope_key=scope_key,
        region_codes=region_codes,
        command=command,
        force_rebuild=force_rebuild,
        status_before={
            "nodes_count": before_status.nodes_count,
            "edges_count": before_status.edges_count,
            "station_links_count": before_status.station_links_count,
            "ready": before_status.ready,
        },
    )

    completed = subprocess.run(
        command,
        cwd=_repo_root(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    after_status = get_scope_topology_status(
        scope_key=scope_key,
        region_codes=region_codes,
    )

    result = {
        "route_id": route_id,
        "train_number": route.get("train_number"),
        "route_name": route.get("route_name"),
        "scope_key": scope_key,
        "region_codes": region_codes,
        "rebuild_triggered": True,
        "status_before": {
            "nodes_count": before_status.nodes_count,
            "edges_count": before_status.edges_count,
            "station_links_count": before_status.station_links_count,
            "ready": before_status.ready,
        },
        "status_after": {
            "nodes_count": after_status.nodes_count,
            "edges_count": after_status.edges_count,
            "station_links_count": after_status.station_links_count,
            "ready": after_status.ready,
        },
        "process": {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "ok": completed.returncode == 0,
        },
    }

    if completed.returncode != 0:
        log_event(
            "error",
            "route_scope_topology_build_failed",
            **result,
        )
        raise RuntimeError(
            "Не удалось подготовить topology graph для маршрута. "
            f"route_id={route_id}, returncode={completed.returncode}"
        )

    if not after_status.ready:
        log_event(
            "error",
            "route_scope_topology_build_incomplete",
            **result,
        )
        raise RuntimeError(
            "Topology build завершился без ошибки, но сеть по scope_key осталась неполной. "
            f"route_id={route_id}, scope_key={scope_key}"
        )

    log_event(
        "info",
        "route_scope_topology_build_finished",
        **result,
    )

    return result