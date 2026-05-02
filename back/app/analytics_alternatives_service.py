from __future__ import annotations

import heapq
import logging
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.analytics_alternatives_schemas import (
    AlternativeRouteItem,
    AlternativeRoutesParams,
    AlternativeRoutesResponse,
)
from app.route_graph_matcher import (
    Candidate,
    build_network_data,
    get_station_link_options_for_candidate,
    haversine_km,
    merge_coordinate_sequences,
    resolve_route_for_map,
    safe_float,
)

try:
    from app.virtual_route_service import (
        load_nearest_scope_edge_snap_options,
        load_nearest_scope_node_options,
        load_scope_station_link_options,
        merge_link_options,
        filter_link_options_by_distance,
        build_virtual_pair_coordinates,
    )
except Exception:
    load_nearest_scope_edge_snap_options = None
    load_nearest_scope_node_options = None
    load_scope_station_link_options = None
    merge_link_options = None
    filter_link_options_by_distance = None
    build_virtual_pair_coordinates = None


LOGGER = logging.getLogger(__name__)


class AlternativesError(RuntimeError):
    pass


ALTERNATIVE_MAX_STATION_CONNECTOR_AIR_KM = 1.5
ALTERNATIVE_MAX_STATION_CONNECTOR_TOTAL_KM = 25.0
ALTERNATIVE_MAX_LINK_OPTIONS_PER_SIDE = 24

ALTERNATIVE_ABSURD_MIN_GEO_KM = 5.0
ALTERNATIVE_ABSURD_MAX_GEO_RATIO = 5.0
ALTERNATIVE_ABSURD_MAX_GEO_EXTRA_KM = 140.0

CONNECTOR_EDGE_COST_MULTIPLIER = 1.8
RUNTIME_STATION_TRANSFER_COST_MULTIPLIER = 1.05


@dataclass
class PathResult:
    edge_keys: list[str]
    node_hashes: list[str]
    edge_chain: list[dict[str, Any]]
    coordinates: list[list[float]]
    length_km: float
    cost_km: float
    scope_key: str | None
    source_node_hash: str
    target_node_hash: str
    start_link: dict[str, Any]
    end_link: dict[str, Any]


class AlternativeRoutesService:
    def __init__(self, db: Session):
        self.db = db

    def build_for_route(
        self,
        route_id: int,
        params: AlternativeRoutesParams,
    ) -> AlternativeRoutesResponse:
        context = self._get_route_context(route_id)

        return self.build_between_stations(
            origin_station_id=context["origin_station_id"],
            destination_station_id=context["destination_station_id"],
            params=params,
            route_id=route_id,
            region_codes=context.get("region_codes") or None,
        )

    def build_between_stations(
        self,
        origin_station_id: int,
        destination_station_id: int,
        params: AlternativeRoutesParams,
        route_id: int | None = None,
        region_codes: list[str] | None = None,
    ) -> AlternativeRoutesResponse:
        origin_station = self._load_station(origin_station_id)
        destination_station = self._load_station(destination_station_id)

        if origin_station_id == destination_station_id:
            raise AlternativesError("Начальная и конечная станции совпадают")

        if not region_codes:
            region_codes = self._derive_region_codes_from_stations(
                origin_station,
                destination_station,
            )

        if not region_codes:
            raise AlternativesError("Не удалось определить scope регионов для альтернатив")

        diagnostics: dict[str, Any] = {
            "mode": "analytics_alternatives",
            "origin_station_id": origin_station_id,
            "destination_station_id": destination_station_id,
            "region_codes": region_codes,
        }

        network = build_network_data(
            region_codes=region_codes,
            diagnostics=diagnostics,
            logger_context={
                "mode": "analytics_alternatives",
                "route_id": route_id,
                "origin_station_id": origin_station_id,
                "destination_station_id": destination_station_id,
            },
            progress_callback=None,
        )

        if (network.get("stats") or {}).get("network_mode") != "scope_topology_graph":
            raise AlternativesError(
                "Topology graph для выбранного scope не найден или не построен"
            )

        adjacency = network.get("adjacency") or {}
        node_coords = network.get("node_coords") or {}

        if not adjacency or not node_coords:
            raise AlternativesError("Topology graph пустой")

        origin_candidate = self._station_to_candidate(origin_station)
        destination_candidate = self._station_to_candidate(destination_station)

        source_links = self._get_station_link_options(
            station=origin_station,
            candidate=origin_candidate,
            network=network,
        )
        target_links = self._get_station_link_options(
            station=destination_station,
            candidate=destination_candidate,
            network=network,
        )

        if not source_links or not target_links:
            raise AlternativesError(
                "Не удалось связать одну из станций с topology graph"
            )

        paths = self._build_penalized_alternatives(
            adjacency=adjacency,
            node_coords=node_coords,
            source_links=source_links,
            target_links=target_links,
            origin_candidate=origin_candidate,
            destination_candidate=destination_candidate,
            params=params,
            scope_key=network.get("scope_key"),
        )

        if not paths:
            raise AlternativesError(
                "Не удалось построить базовый путь между выбранными станциями"
            )

        base_path = paths[0]
        alternative_paths = paths[1:]

        base_edges = set(base_path.edge_keys)
        base_length_km = base_path.length_km

        alternatives: list[AlternativeRouteItem] = []

        for index, path in enumerate(alternative_paths, start=1):
            current_edges = set(path.edge_keys)
            overlap_ratio = self._calculate_overlap_ratio(base_edges, current_edges)
            difference_ratio = round(1.0 - overlap_ratio, 4)

            length_ratio = (
                path.length_km / base_length_km
                if base_length_km and base_length_km > 0
                else 1.0
            )

            alternatives.append(
                AlternativeRouteItem(
                    id=f"alt-{index}",
                    rank=index,
                    origin_station_id=origin_station_id,
                    destination_station_id=destination_station_id,
                    source_node_hash=path.source_node_hash,
                    target_node_hash=path.target_node_hash,
                    scope_key=path.scope_key,
                    length_km=round(path.length_km, 3),
                    length_ratio=round(length_ratio, 4),
                    overlap_ratio=round(overlap_ratio, 4),
                    difference_ratio=difference_ratio,
                    edges_count=len(path.edge_keys),
                    geometry=self._build_geometry_from_coordinates(path.coordinates),
                )
            )

        if alternatives:
            message = f"Построено альтернатив: {len(alternatives)}"
        else:
            message = (
                "Базовый путь построен, но достаточно отличающиеся альтернативы "
                "для выбранных параметров не найдены"
            )

        return AlternativeRoutesResponse(
            route_id=route_id,
            origin_station_id=origin_station_id,
            destination_station_id=destination_station_id,
            source_node_hash=base_path.source_node_hash,
            target_node_hash=base_path.target_node_hash,
            base_length_km=round(base_length_km, 3),
            alternatives=alternatives,
            message=message,
        )

    def _get_route_context(self, route_id: int) -> dict[str, Any]:
        try:
            resolved = resolve_route_for_map(route_id, persist=False)
            stops = resolved.get("stops") or []

            matched_stops = [
                stop for stop in stops
                if stop.get("station_id") is not None
            ]

            if len(matched_stops) >= 2:
                origin_station_id = int(matched_stops[0]["station_id"])
                destination_station_id = int(matched_stops[-1]["station_id"])

                diagnostics = resolved.get("diagnostics") or {}
                inferred = diagnostics.get("inferred_route_regions") or {}
                region_codes = inferred.get("inferred_region_codes") or []

                if not region_codes:
                    region_codes = self._get_route_matched_region_codes(route_id)

                return {
                    "origin_station_id": origin_station_id,
                    "destination_station_id": destination_station_id,
                    "region_codes": region_codes,
                }

        except Exception as exc:
            LOGGER.warning(
                "Could not resolve route before alternatives, fallback to DB endpoints: route_id=%s error=%s",
                route_id,
                exc,
            )

        origin_station_id, destination_station_id = self._get_route_endpoint_station_ids_from_db(
            route_id
        )

        return {
            "origin_station_id": origin_station_id,
            "destination_station_id": destination_station_id,
            "region_codes": self._get_route_matched_region_codes(route_id),
        }

    def _get_route_endpoint_station_ids_from_db(self, route_id: int) -> tuple[int, int]:
        row = self.db.execute(
            text(
                """
                WITH matched_stops AS (
                    SELECT station_id, stop_sequence
                    FROM route_stops
                    WHERE route_id = :route_id
                      AND station_id IS NOT NULL
                )
                SELECT
                    (
                        SELECT station_id
                        FROM matched_stops
                        ORDER BY stop_sequence ASC
                        LIMIT 1
                    ) AS origin_station_id,
                    (
                        SELECT station_id
                        FROM matched_stops
                        ORDER BY stop_sequence DESC
                        LIMIT 1
                    ) AS destination_station_id
                """
            ),
            {"route_id": route_id},
        ).mappings().first()

        if (
            not row
            or row["origin_station_id"] is None
            or row["destination_station_id"] is None
        ):
            raise AlternativesError(
                f"Route {route_id} has no matched endpoint stations"
            )

        return int(row["origin_station_id"]), int(row["destination_station_id"])

    def _get_route_matched_region_codes(self, route_id: int) -> list[str]:
        rows = self.db.execute(
            text(
                """
                SELECT DISTINCT s.region_code
                FROM route_stops rs
                JOIN stations s ON s.id = rs.station_id
                WHERE rs.route_id = :route_id
                  AND rs.station_id IS NOT NULL
                  AND s.region_code IS NOT NULL
                ORDER BY s.region_code
                """
            ),
            {"route_id": route_id},
        ).mappings().all()

        return [str(row["region_code"]) for row in rows if row["region_code"]]

    def _load_station(self, station_id: int) -> dict[str, Any]:
        row = self.db.execute(
            text(
                """
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
                LIMIT 1
                """
            ),
            {"station_id": station_id},
        ).mappings().first()

        if not row:
            raise AlternativesError(f"Station not found: {station_id}")

        item = dict(row)

        if item.get("lon") is None or item.get("lat") is None:
            raise AlternativesError(f"Station has no geometry: {station_id}")

        return item

    def _derive_region_codes_from_stations(
        self,
        origin_station: dict[str, Any],
        destination_station: dict[str, Any],
    ) -> list[str]:
        result: list[str] = []
        seen = set()

        for station in [origin_station, destination_station]:
            code = station.get("region_code")
            if not code:
                continue

            code = str(code)

            if code in seen:
                continue

            seen.add(code)
            result.append(code)

        return result

    def _station_to_candidate(self, station: dict[str, Any]) -> Candidate:
        return Candidate(
            station_id=int(station["id"]),
            region_code=station.get("region_code"),
            name=station.get("name") or f"station_id={station['id']}",
            lon=float(station["lon"]),
            lat=float(station["lat"]),
            effective_score=1.0,
            name_score=1.0,
            code_match=False,
            anchor=True,
            is_main_rail_station=bool(station.get("is_main_rail_station")),
            match_method="analytics_selected_station",
            match_reason="selected_for_alternative_route",
            code_value=None,
        )

    def _get_station_link_options(
        self,
        *,
        station: dict[str, Any],
        candidate: Candidate,
        network: dict[str, Any],
    ) -> list[dict[str, Any]]:
        fallback_node_cache: dict[int, list[dict[str, Any]]] = {}

        base_links = get_station_link_options_for_candidate(
            candidate,
            network,
            fallback_node_cache,
        )

        extra_links: list[dict[str, Any]] = []

        if load_scope_station_link_options is not None:
            try:
                extra_links.extend(
                    load_scope_station_link_options(
                        station=station,
                        network=network,
                        limit=ALTERNATIVE_MAX_LINK_OPTIONS_PER_SIDE,
                    )
                )
            except Exception as exc:
                LOGGER.warning("Failed to load scope station links: %s", exc)

        if load_nearest_scope_node_options is not None:
            try:
                extra_links.extend(
                    load_nearest_scope_node_options(
                        station=station,
                        network=network,
                        radius_m=1500,
                        limit=16,
                    )
                )
            except Exception as exc:
                LOGGER.warning("Failed to load nearest node links: %s", exc)

        if load_nearest_scope_edge_snap_options is not None:
            try:
                extra_links.extend(
                    load_nearest_scope_edge_snap_options(
                        station=station,
                        network=network,
                        radius_m=1500,
                        limit=12,
                    )
                )
            except Exception as exc:
                LOGGER.warning("Failed to load edge snap links: %s", exc)

        if merge_link_options is not None:
            merged = merge_link_options(
                base_links=base_links,
                extra_links=extra_links,
                limit=ALTERNATIVE_MAX_LINK_OPTIONS_PER_SIDE,
            )
        else:
            merged = self._merge_link_options_fallback(base_links + extra_links)

        accepted = []

        for link in merged:
            air_distance_km = self._link_air_distance_km(link)
            total_link_km = float(link.get("link_distance_km") or 0.0)

            if air_distance_km > ALTERNATIVE_MAX_STATION_CONNECTOR_AIR_KM:
                continue

            if total_link_km > ALTERNATIVE_MAX_STATION_CONNECTOR_TOTAL_KM:
                continue

            accepted.append(link)

        return accepted[:ALTERNATIVE_MAX_LINK_OPTIONS_PER_SIDE]

    def _merge_link_options_fallback(
        self,
        links: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_node: dict[str, dict[str, Any]] = {}

        for link in links:
            node_hash = str(link.get("node_hash") or "")
            if not node_hash:
                continue

            existing = by_node.get(node_hash)

            if existing is None:
                by_node[node_hash] = dict(link)
                continue

            current_air = self._link_air_distance_km(link)
            existing_air = self._link_air_distance_km(existing)

            current_total = float(link.get("link_distance_km") or 0.0)
            existing_total = float(existing.get("link_distance_km") or 0.0)

            if (current_air, current_total) < (existing_air, existing_total):
                by_node[node_hash] = dict(link)

        result = list(by_node.values())
        result.sort(
            key=lambda item: (
                self._link_air_distance_km(item),
                float(item.get("link_distance_km") or 0.0),
                str(item.get("node_hash") or ""),
            )
        )
        return result

    @staticmethod
    def _link_air_distance_km(link: dict[str, Any]) -> float:
        if link.get("station_snap_distance_km") is not None:
            return float(link.get("station_snap_distance_km") or 0.0)

        return float(link.get("link_distance_km") or 0.0)

    def _build_penalized_alternatives(
        self,
        *,
        adjacency: dict[str, list[dict[str, Any]]],
        node_coords: dict[str, dict[str, float]],
        source_links: list[dict[str, Any]],
        target_links: list[dict[str, Any]],
        origin_candidate: Candidate,
        destination_candidate: Candidate,
        params: AlternativeRoutesParams,
        scope_key: str | None,
    ) -> list[PathResult]:
        accepted: list[PathResult] = []
        penalty_counts: dict[str, int] = {}

        attempts = max(params.max_attempts, params.max_alternatives * 8, 12)

        base_path: PathResult | None = None

        for _attempt in range(attempts):
            path = self._find_best_path_between_link_options(
                adjacency=adjacency,
                node_coords=node_coords,
                source_links=source_links,
                target_links=target_links,
                origin_candidate=origin_candidate,
                destination_candidate=destination_candidate,
                penalty_counts=penalty_counts,
                penalty_factor=params.penalty_factor,
                scope_key=scope_key,
            )

            if path is None:
                break

            if base_path is None:
                base_path = path
                accepted.append(path)

                for edge_key in path.edge_keys:
                    penalty_counts[edge_key] = penalty_counts.get(edge_key, 0) + 1

                continue

            if path.length_km > base_path.length_km * params.max_length_ratio:
                for edge_key in path.edge_keys:
                    penalty_counts[edge_key] = penalty_counts.get(edge_key, 0) + 1

                continue

            candidate_edges = set(path.edge_keys)

            is_duplicate = False
            for accepted_path in accepted:
                accepted_edges = set(accepted_path.edge_keys)
                overlap_ratio = self._calculate_overlap_ratio(
                    accepted_edges,
                    candidate_edges,
                )
                difference_ratio = 1.0 - overlap_ratio

                if difference_ratio < params.min_difference_ratio:
                    is_duplicate = True
                    break

            for edge_key in path.edge_keys:
                penalty_counts[edge_key] = penalty_counts.get(edge_key, 0) + 1

            if is_duplicate:
                continue

            accepted.append(path)

            # +1, потому что accepted[0] — базовый путь, а не альтернатива.
            if len(accepted) >= params.max_alternatives + 1:
                break

        return accepted

    def _find_best_path_between_link_options(
        self,
        *,
        adjacency: dict[str, list[dict[str, Any]]],
        node_coords: dict[str, dict[str, float]],
        source_links: list[dict[str, Any]],
        target_links: list[dict[str, Any]],
        origin_candidate: Candidate,
        destination_candidate: Candidate,
        penalty_counts: dict[str, int],
        penalty_factor: float,
        scope_key: str | None,
    ) -> PathResult | None:
        best_path: PathResult | None = None
        best_cost = math.inf

        geo_distance_km = haversine_km(
            origin_candidate.lon,
            origin_candidate.lat,
            destination_candidate.lon,
            destination_candidate.lat,
        )

        for source_link in source_links:
            for target_link in target_links:
                source_node_hash = str(source_link["node_hash"])
                target_node_hash = str(target_link["node_hash"])

                if source_node_hash == target_node_hash:
                    continue

                graph_path = self._dijkstra(
                    adjacency=adjacency,
                    source=source_node_hash,
                    target=target_node_hash,
                    penalty_counts=penalty_counts,
                    penalty_factor=penalty_factor,
                    scope_key=scope_key,
                )

                if graph_path is None:
                    continue

                total_length_km = (
                    graph_path.length_km
                    + float(source_link.get("link_distance_km") or 0.0)
                    + float(target_link.get("link_distance_km") or 0.0)
                )

                if self._is_absurd_path(
                    total_length_km=total_length_km,
                    geo_distance_km=geo_distance_km,
                ):
                    continue

                connector_cost = (
                    float(source_link.get("link_distance_km") or 0.0)
                    + float(target_link.get("link_distance_km") or 0.0)
                )

                air_connector_cost = (
                    self._link_air_distance_km(source_link)
                    + self._link_air_distance_km(target_link)
                ) * 8.0

                total_cost = graph_path.cost_km + connector_cost + air_connector_cost

                graph_coords = merge_coordinate_sequences(
                    [edge.get("geometry_coords") or [] for edge in graph_path.edge_chain]
                )

                coordinates = self._build_full_path_coordinates(
                    origin_candidate=origin_candidate,
                    destination_candidate=destination_candidate,
                    start_link=source_link,
                    end_link=target_link,
                    graph_coords=graph_coords,
                )

                if len(coordinates) < 2:
                    continue

                candidate_path = PathResult(
                    edge_keys=graph_path.edge_keys,
                    node_hashes=graph_path.node_hashes,
                    edge_chain=graph_path.edge_chain,
                    coordinates=coordinates,
                    length_km=total_length_km,
                    cost_km=total_cost,
                    scope_key=scope_key,
                    source_node_hash=source_node_hash,
                    target_node_hash=target_node_hash,
                    start_link=source_link,
                    end_link=target_link,
                )

                if total_cost < best_cost:
                    best_cost = total_cost
                    best_path = candidate_path

        return best_path

    def _dijkstra(
        self,
        *,
        adjacency: dict[str, list[dict[str, Any]]],
        source: str,
        target: str,
        penalty_counts: dict[str, int],
        penalty_factor: float,
        scope_key: str | None,
    ) -> PathResult | None:
        queue: list[tuple[float, str]] = [(0.0, source)]
        distances: dict[str, float] = {source: 0.0}
        previous: dict[str, tuple[str, dict[str, Any], str, float, float]] = {}
        visited: set[str] = set()

        while queue:
            current_cost, current_node = heapq.heappop(queue)

            if current_node in visited:
                continue

            visited.add(current_node)

            if current_node == target:
                break

            for edge in adjacency.get(current_node, []):
                next_node = str(edge.get("to_node_hash") or "")

                if not next_node or next_node in visited:
                    continue

                edge_length_km = safe_float(edge.get("length_km"))
                if edge_length_km is None or edge_length_km <= 0:
                    continue

                edge_key = self._edge_key(edge)
                base_cost_km = self._edge_base_cost(edge, edge_length_km)

                penalty_power = penalty_counts.get(edge_key, 0)
                effective_cost = base_cost_km * (penalty_factor ** penalty_power)
                next_cost = current_cost + effective_cost

                if next_cost < distances.get(next_node, math.inf):
                    distances[next_node] = next_cost
                    previous[next_node] = (
                        current_node,
                        edge,
                        edge_key,
                        edge_length_km,
                        base_cost_km,
                    )
                    heapq.heappush(queue, (next_cost, next_node))

        if target not in distances:
            return None

        node_hashes = [target]
        edge_keys_reversed: list[str] = []
        edge_chain_reversed: list[dict[str, Any]] = []

        physical_length_km_total = 0.0
        cost_km_total = 0.0

        cursor = target

        while cursor != source:
            if cursor not in previous:
                return None

            (
                prev_node,
                edge,
                edge_key,
                physical_length_km,
                base_cost_km,
            ) = previous[cursor]

            edge_keys_reversed.append(edge_key)
            edge_chain_reversed.append(edge)

            physical_length_km_total += physical_length_km
            cost_km_total += base_cost_km

            node_hashes.append(prev_node)
            cursor = prev_node

        node_hashes.reverse()
        edge_keys = list(reversed(edge_keys_reversed))
        edge_chain = list(reversed(edge_chain_reversed))

        return PathResult(
            edge_keys=edge_keys,
            node_hashes=node_hashes,
            edge_chain=edge_chain,
            coordinates=[],
            length_km=physical_length_km_total,
            cost_km=cost_km_total,
            scope_key=scope_key,
            source_node_hash=source,
            target_node_hash=target,
            start_link={},
            end_link={},
        )

    def _edge_key(self, edge: dict[str, Any]) -> str:
        edge_id = edge.get("edge_id") or edge.get("id")

        if edge_id is not None:
            return f"edge:{edge_id}"

        from_node = str(edge.get("from_node_hash") or "")
        to_node = str(edge.get("to_node_hash") or "")

        left, right = sorted([from_node, to_node])

        return f"runtime:{edge.get('edge_source') or 'unknown'}:{left}:{right}"

    def _edge_base_cost(
        self,
        edge: dict[str, Any],
        length_km: float,
    ) -> float:
        edge_source = str(edge.get("edge_source") or "")

        if edge_source == "runtime_station_transfer_connector":
            return length_km * RUNTIME_STATION_TRANSFER_COST_MULTIPLIER

        if bool(edge.get("is_virtual_connector")):
            return length_km * CONNECTOR_EDGE_COST_MULTIPLIER

        if edge_source.startswith("virtual_connector"):
            return length_km * CONNECTOR_EDGE_COST_MULTIPLIER

        return length_km

    def _is_absurd_path(
        self,
        *,
        total_length_km: float,
        geo_distance_km: float,
    ) -> bool:
        if geo_distance_km < ALTERNATIVE_ABSURD_MIN_GEO_KM:
            return False

        max_allowed = max(
            geo_distance_km * ALTERNATIVE_ABSURD_MAX_GEO_RATIO,
            geo_distance_km + ALTERNATIVE_ABSURD_MAX_GEO_EXTRA_KM,
        )

        return total_length_km > max_allowed

    def _build_full_path_coordinates(
        self,
        *,
        origin_candidate: Candidate,
        destination_candidate: Candidate,
        start_link: dict[str, Any],
        end_link: dict[str, Any],
        graph_coords: list[list[float]],
    ) -> list[list[float]]:
        if build_virtual_pair_coordinates is not None:
            try:
                return build_virtual_pair_coordinates(
                    origin_candidate=origin_candidate,
                    destination_candidate=destination_candidate,
                    start_link=start_link,
                    end_link=end_link,
                    graph_coords=graph_coords,
                )
            except Exception:
                pass

        sequences: list[list[list[float]]] = []

        start_connector = [
            [origin_candidate.lon, origin_candidate.lat],
            [float(start_link["node_lon"]), float(start_link["node_lat"])],
        ]

        if start_connector[0] != start_connector[1]:
            sequences.append(start_connector)

        if graph_coords:
            sequences.append(graph_coords)

        end_connector = [
            [float(end_link["node_lon"]), float(end_link["node_lat"])],
            [destination_candidate.lon, destination_candidate.lat],
        ]

        if end_connector[0] != end_connector[1]:
            sequences.append(end_connector)

        return merge_coordinate_sequences(sequences)

    def _build_geometry_from_coordinates(
        self,
        coordinates: list[list[float]],
    ) -> dict[str, Any]:
        normalized: list[list[float]] = []

        for coord in coordinates:
            if not coord or len(coord) < 2:
                continue

            item = [float(coord[0]), float(coord[1])]

            if normalized and normalized[-1] == item:
                continue

            normalized.append(item)

        if len(normalized) < 2:
            return {
                "type": "MultiLineString",
                "coordinates": [],
            }

        return {
            "type": "MultiLineString",
            "coordinates": [normalized],
        }

    @staticmethod
    def _calculate_overlap_ratio(
        base_edges: set[str],
        other_edges: set[str],
    ) -> float:
        if not base_edges or not other_edges:
            return 0.0

        shared = len(base_edges.intersection(other_edges))
        denominator = min(len(base_edges), len(other_edges))

        if denominator <= 0:
            return 0.0

        return shared / denominator