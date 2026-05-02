from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import heapq
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


LOGGER = logging.getLogger("audit_topology_connectivity")


ENV_CANDIDATES = [
    Path.cwd() / ".env",
    Path.cwd() / "back" / ".env",
    Path(__file__).resolve().parents[1] / ".env",
    Path(__file__).resolve().parents[2] / ".env",
]


@dataclass
class EdgeRow:
    id: int
    scope_key: str
    source_node_hash: str
    target_node_hash: str
    length_km: float
    source_lon: float
    source_lat: float
    target_lon: float
    target_lat: float


@dataclass
class DanglingNode:
    node_hash: str
    scope_key: str
    component_id: int
    lon: float
    lat: float


@dataclass
class GapCandidate:
    source_node_hash: str
    target_node_hash: str
    source_component_id: int
    target_component_id: int
    source_lon: float
    source_lat: float
    target_lon: float
    target_lat: float
    distance_m: float
    confidence: float


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def load_env_files() -> None:
    if load_dotenv is None:
        return

    seen: set[Path] = set()

    for env_path in ENV_CANDIDATES:
        resolved = env_path.resolve()

        if resolved in seen:
            continue

        seen.add(resolved)

        if resolved.exists():
            load_dotenv(resolved, override=False)
            LOGGER.info("Loaded environment variables from %s", resolved)


def build_database_url_from_env() -> str | None:
    direct_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")

    if direct_url:
        return direct_url

    user = (
        os.getenv("POSTGRES_USER")
        or os.getenv("DB_USER")
        or os.getenv("DATABASE_USER")
    )
    password = (
        os.getenv("POSTGRES_PASSWORD")
        or os.getenv("DB_PASSWORD")
        or os.getenv("DATABASE_PASSWORD")
    )
    host = (
        os.getenv("POSTGRES_HOST")
        or os.getenv("DB_HOST")
        or os.getenv("DATABASE_HOST")
        or "localhost"
    )
    port = (
        os.getenv("POSTGRES_PORT")
        or os.getenv("DB_PORT")
        or os.getenv("DATABASE_PORT")
        or "5432"
    )
    database = (
        os.getenv("POSTGRES_DB")
        or os.getenv("DB_NAME")
        or os.getenv("DATABASE_NAME")
    )

    if not user or not password or not database:
        return None

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_m = 6_371_008.8

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(dlambda / 2) ** 2
    )

    return 2 * radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def create_tables(engine: Engine, sql_file: Path) -> None:
    if not sql_file.exists():
        raise FileNotFoundError(f"SQL file does not exist: {sql_file}")

    sql = sql_file.read_text(encoding="utf-8")

    with engine.begin() as conn:
        conn.execute(text(sql))


def clear_previous_for_scope(engine: Engine, scope_key: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM rail_graph_gap_candidates
                WHERE scope_key = :scope_key
                  AND status = 'candidate'
                """
            ),
            {"scope_key": scope_key},
        )

        conn.execute(
            text(
                """
                DELETE FROM rail_graph_dangling_nodes
                WHERE scope_key = :scope_key
                """
            ),
            {"scope_key": scope_key},
        )

        conn.execute(
            text(
                """
                DELETE FROM rail_graph_components
                WHERE scope_key = :scope_key
                """
            ),
            {"scope_key": scope_key},
        )


def load_available_scope_keys(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT scope_key
                FROM rail_graph_edges
                WHERE scope_key IS NOT NULL
                GROUP BY scope_key
                ORDER BY scope_key
                """
            )
        ).mappings().all()

    return [str(row["scope_key"]) for row in rows]


def load_edges(engine: Engine, scope_key: str, max_edges: int | None) -> list[EdgeRow]:
    sql = text(
        """
        SELECT
            id,
            scope_key,
            source_node_hash,
            target_node_hash,
            length_km,

            ST_X(ST_StartPoint(geom)) AS source_lon,
            ST_Y(ST_StartPoint(geom)) AS source_lat,
            ST_X(ST_EndPoint(geom)) AS target_lon,
            ST_Y(ST_EndPoint(geom)) AS target_lat

        FROM rail_graph_edges
        WHERE scope_key = :scope_key
          AND source_node_hash IS NOT NULL
          AND target_node_hash IS NOT NULL
          AND length_km IS NOT NULL
          AND length_km > 0
          AND geom IS NOT NULL
        ORDER BY id
        LIMIT :max_edges
        """
    )

    with engine.connect() as conn:
        rows = conn.execute(
            sql,
            {
                "scope_key": scope_key,
                "max_edges": max_edges or 2_000_000_000,
            },
        ).mappings().all()

    edges: list[EdgeRow] = []

    for row in rows:
        if row["source_lon"] is None or row["target_lon"] is None:
            continue

        edges.append(
            EdgeRow(
                id=int(row["id"]),
                scope_key=str(row["scope_key"]),
                source_node_hash=str(row["source_node_hash"]),
                target_node_hash=str(row["target_node_hash"]),
                length_km=float(row["length_km"]),
                source_lon=float(row["source_lon"]),
                source_lat=float(row["source_lat"]),
                target_lon=float(row["target_lon"]),
                target_lat=float(row["target_lat"]),
            )
        )

    return edges


def build_components(
    edges: list[EdgeRow],
) -> tuple[
    dict[str, int],
    dict[int, dict[str, Any]],
    dict[str, int],
    dict[str, tuple[float, float]],
]:
    adjacency: dict[str, list[str]] = defaultdict(list)
    degree: dict[str, int] = defaultdict(int)
    coords: dict[str, tuple[float, float]] = {}

    for edge in edges:
        adjacency[edge.source_node_hash].append(edge.target_node_hash)
        adjacency[edge.target_node_hash].append(edge.source_node_hash)

        degree[edge.source_node_hash] += 1
        degree[edge.target_node_hash] += 1

        coords.setdefault(edge.source_node_hash, (edge.source_lon, edge.source_lat))
        coords.setdefault(edge.target_node_hash, (edge.target_lon, edge.target_lat))

    node_component: dict[str, int] = {}
    component_id = 0

    for node_hash in adjacency.keys():
        if node_hash in node_component:
            continue

        component_id += 1

        queue = deque([node_hash])
        node_component[node_hash] = component_id

        while queue:
            current = queue.popleft()

            for next_node in adjacency[current]:
                if next_node in node_component:
                    continue

                node_component[next_node] = component_id
                queue.append(next_node)

    nodes_by_component: dict[int, int] = defaultdict(int)
    edges_by_component: dict[int, int] = defaultdict(int)
    length_by_component: dict[int, float] = defaultdict(float)

    for node_hash, comp_id in node_component.items():
        nodes_by_component[comp_id] += 1

    for edge in edges:
        comp_id = node_component[edge.source_node_hash]
        edges_by_component[comp_id] += 1
        length_by_component[comp_id] += edge.length_km

    component_info: dict[int, dict[str, Any]] = {}

    for comp_id, nodes_count in nodes_by_component.items():
        component_info[comp_id] = {
            "component_id": comp_id,
            "nodes_count": nodes_count,
            "edges_count": edges_by_component.get(comp_id, 0),
            "total_length_km": length_by_component.get(comp_id, 0.0),
        }

    return node_component, component_info, degree, coords


def find_gap_candidates(
    dangling_nodes: list[DanglingNode],
    max_gap_m: float,
    same_component_allowed: bool,
    max_candidates_per_node: int,
) -> list[GapCandidate]:
    if not dangling_nodes:
        return []

    # Rough degree grid. Final distance is still haversine.
    cell_deg = max(max_gap_m / 111_000.0, 0.0001)

    grid: dict[tuple[int, int], list[DanglingNode]] = defaultdict(list)

    for node in dangling_nodes:
        gx = int(math.floor(node.lon / cell_deg))
        gy = int(math.floor(node.lat / cell_deg))
        grid[(gx, gy)].append(node)

    candidates_by_node: dict[str, list[tuple[float, GapCandidate]]] = defaultdict(list)

    for node in dangling_nodes:
        gx = int(math.floor(node.lon / cell_deg))
        gy = int(math.floor(node.lat / cell_deg))

        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                nearby_nodes = grid.get((gx + dx, gy + dy), [])

                for other in nearby_nodes:
                    if node.node_hash >= other.node_hash:
                        continue

                    if not same_component_allowed and node.component_id == other.component_id:
                        continue

                    distance_m = haversine_m(
                        node.lon,
                        node.lat,
                        other.lon,
                        other.lat,
                    )

                    if distance_m > max_gap_m:
                        continue

                    confidence = max(0.0, min(1.0, 1.0 - distance_m / max_gap_m))

                    candidate = GapCandidate(
                        source_node_hash=node.node_hash,
                        target_node_hash=other.node_hash,
                        source_component_id=node.component_id,
                        target_component_id=other.component_id,
                        source_lon=node.lon,
                        source_lat=node.lat,
                        target_lon=other.lon,
                        target_lat=other.lat,
                        distance_m=distance_m,
                        confidence=confidence,
                    )

                    # Store nearest N candidates for each endpoint.
                    heapq.heappush(candidates_by_node[node.node_hash], (-distance_m, candidate))
                    heapq.heappush(candidates_by_node[other.node_hash], (-distance_m, candidate))

                    if len(candidates_by_node[node.node_hash]) > max_candidates_per_node:
                        heapq.heappop(candidates_by_node[node.node_hash])

                    if len(candidates_by_node[other.node_hash]) > max_candidates_per_node:
                        heapq.heappop(candidates_by_node[other.node_hash])

    unique: dict[tuple[str, str], GapCandidate] = {}

    for heap_items in candidates_by_node.values():
        for _, candidate in heap_items:
            key = tuple(
                sorted(
                    [
                        candidate.source_node_hash,
                        candidate.target_node_hash,
                    ]
                )
            )

            existing = unique.get(key)

            if existing is None or candidate.distance_m < existing.distance_m:
                unique[key] = candidate

    return sorted(unique.values(), key=lambda item: item.distance_m)


def persist_results(
    engine: Engine,
    audit_run_id: uuid.UUID,
    scope_key: str,
    component_info: dict[int, dict[str, Any]],
    dangling_nodes: list[DanglingNode],
    gap_candidates: list[GapCandidate],
    batch_size: int,
) -> None:
    if not component_info:
        return

    largest_component_id = max(
        component_info.values(),
        key=lambda item: item["nodes_count"],
    )["component_id"]

    with engine.begin() as conn:
        component_rows = [
            {
                "audit_run_id": str(audit_run_id),
                "scope_key": scope_key,
                "component_id": item["component_id"],
                "nodes_count": item["nodes_count"],
                "edges_count": item["edges_count"],
                "total_length_km": item["total_length_km"],
                "is_largest": item["component_id"] == largest_component_id,
            }
            for item in component_info.values()
        ]

        conn.execute(
            text(
                """
                INSERT INTO rail_graph_components (
                    audit_run_id,
                    scope_key,
                    component_id,
                    nodes_count,
                    edges_count,
                    total_length_km,
                    is_largest
                )
                VALUES (
                    :audit_run_id,
                    :scope_key,
                    :component_id,
                    :nodes_count,
                    :edges_count,
                    :total_length_km,
                    :is_largest
                )
                """
            ),
            component_rows,
        )

        for i in range(0, len(dangling_nodes), batch_size):
            batch = dangling_nodes[i : i + batch_size]

            conn.execute(
                text(
                    """
                    INSERT INTO rail_graph_dangling_nodes (
                        audit_run_id,
                        scope_key,
                        node_hash,
                        component_id,
                        degree,
                        geom
                    )
                    VALUES (
                        :audit_run_id,
                        :scope_key,
                        :node_hash,
                        :component_id,
                        1,
                        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                    )
                    """
                ),
                [
                    {
                        "audit_run_id": str(audit_run_id),
                        "scope_key": scope_key,
                        "node_hash": node.node_hash,
                        "component_id": node.component_id,
                        "lon": node.lon,
                        "lat": node.lat,
                    }
                    for node in batch
                ],
            )

        for i in range(0, len(gap_candidates), batch_size):
            batch = gap_candidates[i : i + batch_size]

            conn.execute(
                text(
                    """
                    INSERT INTO rail_graph_gap_candidates (
                        audit_run_id,
                        scope_key,
                        source_node_hash,
                        target_node_hash,
                        source_component_id,
                        target_component_id,
                        distance_m,
                        connector_type,
                        confidence,
                        status,
                        geom
                    )
                    VALUES (
                        :audit_run_id,
                        :scope_key,
                        :source_node_hash,
                        :target_node_hash,
                        :source_component_id,
                        :target_component_id,
                        :distance_m,
                        'endpoint_snap',
                        :confidence,
                        'candidate',
                        ST_SetSRID(
                            ST_MakeLine(
                                ST_MakePoint(:source_lon, :source_lat),
                                ST_MakePoint(:target_lon, :target_lat)
                            ),
                            4326
                        )
                    )
                    """
                ),
                [
                    {
                        "audit_run_id": str(audit_run_id),
                        "scope_key": scope_key,
                        "source_node_hash": item.source_node_hash,
                        "target_node_hash": item.target_node_hash,
                        "source_component_id": item.source_component_id,
                        "target_component_id": item.target_component_id,
                        "distance_m": item.distance_m,
                        "confidence": item.confidence,
                        "source_lon": item.source_lon,
                        "source_lat": item.source_lat,
                        "target_lon": item.target_lon,
                        "target_lat": item.target_lat,
                    }
                    for item in batch
                ],
            )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit railway topology graph connectivity"
    )

    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--scope-key",
        action="append",
        dest="scope_keys",
        help="Scope key to audit. Can be passed multiple times.",
    )
    parser.add_argument(
        "--max-gap-m",
        type=float,
        default=80.0,
        help="Max distance between dangling nodes to create gap candidates.",
    )
    parser.add_argument(
        "--max-edges",
        type=int,
        default=None,
        help="Safety limit for loaded edges per scope.",
    )
    parser.add_argument(
        "--max-candidates-per-node",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--same-component-allowed",
        action="store_true",
        help="Also find nearby dangling pairs inside the same component.",
    )
    parser.add_argument("--create-tables", action="store_true")
    parser.add_argument("--clear-previous", action="store_true")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--verbose", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    configure_logging(args.verbose)
    load_env_files()

    database_url = args.database_url or build_database_url_from_env()

    if not database_url:
        LOGGER.error("Database URL was not provided and could not be built from .env")
        return 2

    engine = create_engine(database_url, future=True)

    sql_file = Path(__file__).resolve().parent / "create_topology_qa_tables.sql"

    if args.create_tables:
        create_tables(engine, sql_file)
        LOGGER.info("Topology QA tables are ready")

    available_scope_keys = load_available_scope_keys(engine)
    scope_keys = args.scope_keys or available_scope_keys

    if not scope_keys:
        LOGGER.warning("No scope keys found in rail_graph_edges")
        return 0

    missing_scope_keys = sorted(set(scope_keys) - set(available_scope_keys))
    if missing_scope_keys:
        LOGGER.warning("Requested scope keys not found: %s", missing_scope_keys)

    audit_run_id = uuid.uuid4()
    LOGGER.info("Audit run id: %s", audit_run_id)

    for scope_key in scope_keys:
        LOGGER.info("Auditing scope_key=%s", scope_key)

        if args.clear_previous:
            clear_previous_for_scope(engine, scope_key)

        edges = load_edges(
            engine=engine,
            scope_key=scope_key,
            max_edges=args.max_edges,
        )

        LOGGER.info("Loaded edges: %s", len(edges))

        if not edges:
            LOGGER.warning("No edges found for scope_key=%s", scope_key)
            continue

        node_component, component_info, degree, coords = build_components(edges)

        dangling_nodes = [
            DanglingNode(
                node_hash=node_hash,
                scope_key=scope_key,
                component_id=node_component[node_hash],
                lon=coords[node_hash][0],
                lat=coords[node_hash][1],
            )
            for node_hash, node_degree in degree.items()
            if node_degree == 1
            and node_hash in coords
            and node_hash in node_component
        ]

        gap_candidates = find_gap_candidates(
            dangling_nodes=dangling_nodes,
            max_gap_m=args.max_gap_m,
            same_component_allowed=args.same_component_allowed,
            max_candidates_per_node=args.max_candidates_per_node,
        )

        largest_component = max(
            component_info.values(),
            key=lambda item: item["nodes_count"],
        )

        LOGGER.info(
            "Scope=%s components=%s largest_nodes=%s dangling=%s gap_candidates=%s",
            scope_key,
            len(component_info),
            largest_component["nodes_count"],
            len(dangling_nodes),
            len(gap_candidates),
        )

        persist_results(
            engine=engine,
            audit_run_id=audit_run_id,
            scope_key=scope_key,
            component_info=component_info,
            dangling_nodes=dangling_nodes,
            gap_candidates=gap_candidates,
            batch_size=args.batch_size,
        )

    LOGGER.info("Audit finished: %s", audit_run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())