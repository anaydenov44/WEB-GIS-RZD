from __future__ import annotations

import heapq
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.analytics_alternatives_schemas import (
    AlternativeRouteItem,
    AlternativeRoutesParams,
    AlternativeRoutesResponse,
)


class AlternativesError(RuntimeError):
    pass


@dataclass
class GraphEdge:
    id: int
    source: str
    target: str
    length_km: float
    scope_key: str | None


@dataclass
class PathResult:
    edge_ids: list[int]
    node_hashes: list[str]
    length_km: float
    scope_key: str | None


class AlternativeRoutesService:
    def __init__(self, db: Session):
        self.db = db

    def build_for_route(
        self,
        route_id: int,
        params: AlternativeRoutesParams,
    ) -> AlternativeRoutesResponse:
        origin_station_id, destination_station_id = self._get_route_endpoint_station_ids(route_id)
        return self.build_between_stations(
            origin_station_id=origin_station_id,
            destination_station_id=destination_station_id,
            params=params,
            route_id=route_id,
        )

    def build_between_stations(
        self,
        origin_station_id: int,
        destination_station_id: int,
        params: AlternativeRoutesParams,
        route_id: int | None = None,
    ) -> AlternativeRoutesResponse:
        source_node_hash = self._get_station_node_hash(origin_station_id)
        target_node_hash = self._get_station_node_hash(destination_station_id)

        if source_node_hash == target_node_hash:
            raise AlternativesError("Начальная и конечная станции связаны с одним и тем же graph node")

        scope_key = None
        if params.prefer_common_scope:
            scope_key = self._find_common_scope(source_node_hash, target_node_hash)

        edges = self._load_graph_edges(scope_key=scope_key, limit=params.max_edges_to_load)
        if not edges:
            raise AlternativesError("Topology graph edges were not found")

        adjacency = self._build_adjacency(edges)

        paths = self._build_penalized_alternatives(
            adjacency=adjacency,
            source_node_hash=source_node_hash,
            target_node_hash=target_node_hash,
            params=params,
            scope_key=scope_key,
        )

        if not paths:
            raise AlternativesError("Не удалось построить альтернативные пути между станциями")

        base_length_km = paths[0].length_km
        alternatives: list[AlternativeRouteItem] = []
        base_edges = set(paths[0].edge_ids)

        for index, path in enumerate(paths, start=1):
            geometry = self._build_multiline_geometry(path.edge_ids)

            current_edges = set(path.edge_ids)
            overlap_ratio = self._calculate_overlap_ratio(base_edges, current_edges)
            difference_ratio = round(1.0 - overlap_ratio, 4)
            length_ratio = path.length_km / base_length_km if base_length_km > 0 else 1.0

            alternatives.append(
                AlternativeRouteItem(
                    id=f"alt-{index}",
                    rank=index,
                    origin_station_id=origin_station_id,
                    destination_station_id=destination_station_id,
                    source_node_hash=source_node_hash,
                    target_node_hash=target_node_hash,
                    scope_key=path.scope_key,
                    length_km=round(path.length_km, 3),
                    length_ratio=round(length_ratio, 4),
                    overlap_ratio=round(overlap_ratio, 4),
                    difference_ratio=difference_ratio,
                    edges_count=len(path.edge_ids),
                    geometry=geometry,
                )
            )

        return AlternativeRoutesResponse(
            route_id=route_id,
            origin_station_id=origin_station_id,
            destination_station_id=destination_station_id,
            source_node_hash=source_node_hash,
            target_node_hash=target_node_hash,
            base_length_km=round(base_length_km, 3),
            alternatives=alternatives,
            message=f"Построено альтернатив: {len(alternatives)}",
        )

    def _get_route_endpoint_station_ids(self, route_id: int) -> tuple[int, int]:
        sql = text(
            """
            WITH matched_stops AS (
                SELECT station_id, stop_sequence
                FROM route_stops
                WHERE route_id = :route_id
                  AND station_id IS NOT NULL
            )
            SELECT
                (SELECT station_id FROM matched_stops ORDER BY stop_sequence ASC LIMIT 1) AS origin_station_id,
                (SELECT station_id FROM matched_stops ORDER BY stop_sequence DESC LIMIT 1) AS destination_station_id
            """
        )
        row = self.db.execute(sql, {"route_id": route_id}).mappings().first()

        if not row or row["origin_station_id"] is None or row["destination_station_id"] is None:
            raise AlternativesError(f"Route {route_id} has no matched endpoint stations")

        return int(row["origin_station_id"]), int(row["destination_station_id"])

    def _get_station_node_hash(self, station_id: int) -> str:
        columns = self._get_table_columns("station_graph_links")

        if "graph_node_hash" in columns:
            distance_column = self._get_station_link_distance_order_column(columns)
            sql = text(
                f"""
                SELECT graph_node_hash AS node_hash
                FROM station_graph_links
                WHERE station_id = :station_id
                  AND graph_node_hash IS NOT NULL
                ORDER BY {distance_column} ASC NULLS LAST
                LIMIT 1
                """
            )
            row = self.db.execute(sql, {"station_id": station_id}).mappings().first()
            if row and row["node_hash"]:
                return str(row["node_hash"])

        if "node_hash" in columns:
            distance_column = self._get_station_link_distance_order_column(columns)
            sql = text(
                f"""
                SELECT node_hash
                FROM station_graph_links
                WHERE station_id = :station_id
                  AND node_hash IS NOT NULL
                ORDER BY {distance_column} ASC NULLS LAST
                LIMIT 1
                """
            )
            row = self.db.execute(sql, {"station_id": station_id}).mappings().first()
            if row and row["node_hash"]:
                return str(row["node_hash"])

        if "graph_node_id" in columns:
            node_hash_column = self._detect_node_hash_column()
            distance_column = self._get_station_link_distance_order_column(columns, table_alias="l")
            sql = text(
                f"""
                SELECT n.{node_hash_column} AS node_hash
                FROM station_graph_links l
                JOIN rail_graph_nodes n ON n.id = l.graph_node_id
                WHERE l.station_id = :station_id
                  AND n.{node_hash_column} IS NOT NULL
                ORDER BY {distance_column} ASC NULLS LAST
                LIMIT 1
                """
            )
            row = self.db.execute(sql, {"station_id": station_id}).mappings().first()
            if row and row["node_hash"]:
                return str(row["node_hash"])

        raise AlternativesError(f"No graph node hash found for station_id={station_id}")

    @staticmethod
    def _get_station_link_distance_order_column(
        columns: set[str],
        *,
        table_alias: str | None = None,
    ) -> str:
        prefix = f"{table_alias}." if table_alias else ""
        if "distance_m" in columns:
            return f"{prefix}distance_m"
        if "link_distance_m" in columns:
            return f"{prefix}link_distance_m"
        return "0"

    def _detect_node_hash_column(self) -> str:
        columns = self._get_table_columns("rail_graph_nodes")
        for candidate in ["node_hash", "hash", "graph_node_hash", "osm_node_hash"]:
            if candidate in columns:
                return self._quote_ident(candidate)
        raise AlternativesError("Cannot detect node hash column in rail_graph_nodes")

    def _find_common_scope(self, source_node_hash: str, target_node_hash: str) -> str | None:
        sql = text(
            """
            WITH endpoint_scopes AS (
                SELECT scope_key, 'source' AS endpoint
                FROM rail_graph_edges
                WHERE source_node_hash = :source_node_hash
                   OR target_node_hash = :source_node_hash

                UNION ALL

                SELECT scope_key, 'target' AS endpoint
                FROM rail_graph_edges
                WHERE source_node_hash = :target_node_hash
                   OR target_node_hash = :target_node_hash
            )
            SELECT scope_key
            FROM endpoint_scopes
            WHERE scope_key IS NOT NULL
            GROUP BY scope_key
            HAVING COUNT(DISTINCT endpoint) = 2
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """
        )
        row = self.db.execute(
            sql,
            {
                "source_node_hash": source_node_hash,
                "target_node_hash": target_node_hash,
            },
        ).mappings().first()

        return str(row["scope_key"]) if row and row["scope_key"] else None

    def _load_graph_edges(self, scope_key: str | None, limit: int) -> list[GraphEdge]:
        if scope_key:
            sql = text(
                """
                SELECT id, scope_key, source_node_hash, target_node_hash, length_km
                FROM rail_graph_edges
                WHERE scope_key = :scope_key
                  AND source_node_hash IS NOT NULL
                  AND target_node_hash IS NOT NULL
                  AND length_km IS NOT NULL
                  AND length_km > 0
                ORDER BY id
                LIMIT :limit
                """
            )
            rows = self.db.execute(sql, {"scope_key": scope_key, "limit": limit}).mappings().all()
        else:
            sql = text(
                """
                SELECT id, scope_key, source_node_hash, target_node_hash, length_km
                FROM rail_graph_edges
                WHERE source_node_hash IS NOT NULL
                  AND target_node_hash IS NOT NULL
                  AND length_km IS NOT NULL
                  AND length_km > 0
                ORDER BY id
                LIMIT :limit
                """
            )
            rows = self.db.execute(sql, {"limit": limit}).mappings().all()

        return [
            GraphEdge(
                id=int(row["id"]),
                source=str(row["source_node_hash"]),
                target=str(row["target_node_hash"]),
                length_km=float(row["length_km"]),
                scope_key=str(row["scope_key"]) if row["scope_key"] is not None else None,
            )
            for row in rows
        ]

    @staticmethod
    def _build_adjacency(edges: Iterable[GraphEdge]) -> dict[str, list[tuple[str, int, float]]]:
        adjacency: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
        for edge in edges:
            adjacency[edge.source].append((edge.target, edge.id, edge.length_km))
            adjacency[edge.target].append((edge.source, edge.id, edge.length_km))
        return adjacency

    def _build_penalized_alternatives(
        self,
        adjacency: dict[str, list[tuple[str, int, float]]],
        source_node_hash: str,
        target_node_hash: str,
        params: AlternativeRoutesParams,
        scope_key: str | None,
    ) -> list[PathResult]:
        accepted: list[PathResult] = []
        penalty_counts: dict[int, int] = defaultdict(int)
        base_path: PathResult | None = None

        # max_alternatives теперь означает количество альтернатив,
        # которые пользователь хочет увидеть, а базовый путь строится отдельно.
        target_paths_count = params.max_alternatives + 1
        attempts = max(params.max_attempts, target_paths_count * 6)

        for _attempt_index in range(attempts):
            path = self._dijkstra(
                adjacency=adjacency,
                source=source_node_hash,
                target=target_node_hash,
                penalty_counts=penalty_counts,
                penalty_factor=params.penalty_factor,
                scope_key=scope_key,
            )

            if not path:
                break

            if base_path is None:
                base_path = path
                accepted.append(path)
                for edge_id in path.edge_ids:
                    penalty_counts[edge_id] += 1
                continue

            if path.length_km > base_path.length_km * params.max_length_ratio:
                for edge_id in path.edge_ids:
                    penalty_counts[edge_id] += 1
                continue

            candidate_edges = set(path.edge_ids)
            is_duplicate = False

            for accepted_path in accepted:
                accepted_edges = set(accepted_path.edge_ids)
                overlap_ratio = self._calculate_overlap_ratio(accepted_edges, candidate_edges)
                difference_ratio = 1.0 - overlap_ratio

                if difference_ratio < params.min_difference_ratio:
                    is_duplicate = True
                    break

            for edge_id in path.edge_ids:
                penalty_counts[edge_id] += 1

            if is_duplicate:
                continue

            accepted.append(path)

            if len(accepted) >= target_paths_count:
                break

        return accepted

    @staticmethod
    def _dijkstra(
        adjacency: dict[str, list[tuple[str, int, float]]],
        source: str,
        target: str,
        penalty_counts: dict[int, int],
        penalty_factor: float,
        scope_key: str | None,
    ) -> PathResult | None:
        queue: list[tuple[float, str]] = [(0.0, source)]
        distances: dict[str, float] = {source: 0.0}
        previous: dict[str, tuple[str, int, float]] = {}
        visited: set[str] = set()

        while queue:
            current_distance, current_node = heapq.heappop(queue)

            if current_node in visited:
                continue
            visited.add(current_node)

            if current_node == target:
                break

            for next_node, edge_id, base_cost in adjacency.get(current_node, []):
                if next_node in visited:
                    continue

                penalty_power = penalty_counts.get(edge_id, 0)
                cost = base_cost * (penalty_factor ** penalty_power)
                next_distance = current_distance + cost

                if next_distance < distances.get(next_node, math.inf):
                    distances[next_node] = next_distance
                    previous[next_node] = (current_node, edge_id, base_cost)
                    heapq.heappush(queue, (next_distance, next_node))

        if target not in previous and source != target:
            return None

        node_hashes = [target]
        edge_ids_reversed: list[int] = []
        real_length_km = 0.0
        cursor = target

        while cursor != source:
            prev_node, edge_id, base_cost = previous[cursor]
            edge_ids_reversed.append(edge_id)
            real_length_km += base_cost
            node_hashes.append(prev_node)
            cursor = prev_node

        node_hashes.reverse()
        edge_ids = list(reversed(edge_ids_reversed))

        return PathResult(
            edge_ids=edge_ids,
            node_hashes=node_hashes,
            length_km=real_length_km,
            scope_key=scope_key,
        )

    def _build_multiline_geometry(self, edge_ids: list[int]) -> dict[str, Any]:
        if not edge_ids:
            return {"type": "MultiLineString", "coordinates": []}

        rows = self.db.execute(
            text(
                """
                SELECT id, ST_AsGeoJSON(geom) AS geojson
                FROM rail_graph_edges
                WHERE id = ANY(:edge_ids)
                """
            ),
            {"edge_ids": edge_ids},
        ).mappings().all()

        geometry_by_id = {
            int(row["id"]): json.loads(row["geojson"])
            for row in rows
            if row["geojson"]
        }

        multilines: list[Any] = []
        for edge_id in edge_ids:
            geometry = geometry_by_id.get(edge_id)
            if not geometry:
                continue

            if geometry["type"] == "LineString":
                multilines.append(geometry["coordinates"])
            elif geometry["type"] == "MultiLineString":
                multilines.extend(geometry["coordinates"])

        return {
            "type": "MultiLineString",
            "coordinates": multilines,
        }

    @staticmethod
    def _calculate_overlap_ratio(base_edges: set[int], other_edges: set[int]) -> float:
        if not base_edges or not other_edges:
            return 0.0
        shared = len(base_edges.intersection(other_edges))
        denominator = min(len(base_edges), len(other_edges))
        if denominator <= 0:
            return 0.0
        return shared / denominator

    def _get_table_columns(self, table_name: str) -> set[str]:
        rows = self.db.execute(
            text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = :table_name
                """
            ),
            {"table_name": table_name},
        ).mappings().all()
        return {str(row["column_name"]) for row in rows}

    @staticmethod
    def _quote_ident(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'
