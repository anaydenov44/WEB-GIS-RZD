from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.analytics_schemas import (
    AnalyticsSummary,
    CorridorAnalysisParams,
    CorridorAnalysisResponse,
    RouteInfo,
    SettlementCandidate,
    VirtualStationInfo,
)
from app.analytics_scoring import ScoreInput, calculate_candidate_score, get_attention_level


class AnalyticsService:
    def __init__(self, db: Session):
        self.db = db

    def analyze_real_route(
        self,
        route_id: int,
        params: CorridorAnalysisParams,
    ) -> CorridorAnalysisResponse:
        route_geom_data = self._get_route_geometry(route_id)
        route_geojson = route_geom_data["geojson"]

        stations_count = self._get_route_stations_count(route_id)

        settlements = self._analyze_corridor(
            route_geojson=route_geojson,
            params=params,
            route_id=route_id,
            station_ids=None,
        )

        summary = self._build_summary(settlements)

        return CorridorAnalysisResponse(
            route=RouteInfo(
                id=route_id,
                source="real",
                length_km=route_geom_data.get("length_km"),
                stations_count=stations_count,
            ),
            params=params,
            summary=summary,
            settlements=settlements,
        )

    def analyze_virtual_route(
        self,
        route_geojson: dict[str, Any],
        params: CorridorAnalysisParams,
        start_station_id: Optional[int] = None,
        end_station_id: Optional[int] = None,
    ) -> CorridorAnalysisResponse:
        station_ids = [
            station_id
            for station_id in [start_station_id, end_station_id]
            if station_id is not None
        ]

        length_km = self._get_geojson_length_km(route_geojson)

        settlements = self._analyze_corridor(
            route_geojson=route_geojson,
            params=params,
            route_id=None,
            station_ids=station_ids,
        )

        summary = self._build_summary(settlements)

        return CorridorAnalysisResponse(
            route=RouteInfo(
                id=None,
                source="virtual",
                length_km=length_km,
                stations_count=len(station_ids),
            ),
            params=params,
            summary=summary,
            settlements=settlements,
        )

    def _get_route_geometry(self, route_id: int) -> dict[str, Any]:
        geom_column = self._detect_geometry_column("routes")

        sql = text(
            f"""
            SELECT
                ST_AsGeoJSON({geom_column}) AS geojson,
                ST_Length({geom_column}::geography) / 1000.0 AS length_km
            FROM routes
            WHERE id = :route_id
              AND {geom_column} IS NOT NULL
            LIMIT 1
            """
        )

        row = self.db.execute(sql, {"route_id": route_id}).mappings().first()

        if not row:
            raise ValueError(f"Route {route_id} not found or has no geometry")

        return {
            "geojson": json.loads(row["geojson"]),
            "length_km": round(float(row["length_km"]), 2) if row["length_km"] is not None else None,
        }

    def _get_geojson_length_km(self, route_geojson: dict[str, Any]) -> float | None:
        sql = text(
            """
            SELECT
                ST_Length(
                    ST_SetSRID(ST_GeomFromGeoJSON(:route_geojson), 4326)::geography
                ) / 1000.0 AS length_km
            """
        )

        row = self.db.execute(
            sql,
            {"route_geojson": json.dumps(route_geojson)},
        ).mappings().first()

        if not row or row["length_km"] is None:
            return None

        return round(float(row["length_km"]), 2)

    def _get_route_stations_count(self, route_id: int) -> int:
        sql = text(
            """
            SELECT COUNT(*) AS cnt
            FROM route_stops
            WHERE route_id = :route_id
            """
        )

        row = self.db.execute(sql, {"route_id": route_id}).mappings().first()
        return int(row["cnt"]) if row else 0

    def _analyze_corridor(
        self,
        route_geojson: dict[str, Any],
        params: CorridorAnalysisParams,
        route_id: int | None,
        station_ids: list[int] | None,
    ) -> list[SettlementCandidate]:
        station_geom_column = self._detect_geometry_column("stations")

        sql = text(
            f"""
            WITH route AS (
                SELECT ST_SetSRID(ST_GeomFromGeoJSON(:route_geojson), 4326) AS geom
            ),
            route_stations AS (
                SELECT st.id, {station_geom_column} AS geom
                FROM stations st
                WHERE {station_geom_column} IS NOT NULL
                  AND (
                    (:route_id IS NOT NULL AND st.id IN (
                        SELECT rs.station_id
                        FROM route_stops rs
                        WHERE rs.route_id = :route_id
                    ))
                    OR
                    (:route_id IS NULL AND st.id = ANY(:station_ids))
                  )
            ),
            settlement_candidates AS (
                SELECT
                    s.id,
                    s.canonical_name,
                    s.settlement_type,
                    s.region,
                    s.federal_district,
                    s.population,
                    s.geom,

                    ST_Distance(s.geom::geography, route.geom::geography) / 1000.0
                        AS distance_to_route_km,

                    ST_AsGeoJSON(s.geom) AS settlement_geojson,

                    ST_AsGeoJSON(
                        ST_ClosestPoint(route.geom, s.geom)
                    ) AS virtual_station_geojson,

                    nearest.distance_to_station_km
                        AS distance_to_nearest_route_station_km

                FROM settlements s
                CROSS JOIN route
                LEFT JOIN LATERAL (
                    SELECT
                        ST_Distance(s.geom::geography, rs.geom::geography) / 1000.0
                            AS distance_to_station_km
                    FROM route_stations rs
                    ORDER BY s.geom <-> rs.geom
                    LIMIT 1
                ) nearest ON TRUE

                WHERE s.population IS NOT NULL
                  AND s.geom IS NOT NULL
                  AND s.population >= :min_population
                  AND (:max_population IS NULL OR s.population <= :max_population)
                  AND ST_DWithin(
                    s.geom::geography,
                    route.geom::geography,
                    :corridor_meters
                  )
                  AND (
                    :exclude_aggregate_like_names = FALSE
                    OR (
                        s.canonical_name NOT ILIKE '%%прочие сельские населенные пункты%%'
                        AND s.canonical_name NOT ILIKE '%%прочие сельские населённые пункты%%'
                        AND s.canonical_name NOT ILIKE '%%прочие городские населенные пункты%%'
                        AND s.canonical_name NOT ILIKE '%%сельское поселение%%'
                        AND s.canonical_name NOT ILIKE '%%городское поселение%%'
                        AND s.canonical_name NOT ILIKE '%%сельсовет%%'
                    )
                  )
            )
            SELECT *
            FROM settlement_candidates
            ORDER BY population DESC
            LIMIT :max_results
            """
        )

        query_params = {
            "route_geojson": json.dumps(route_geojson),
            "route_id": route_id,
            "station_ids": station_ids or [],
            "corridor_meters": params.corridor_km * 1000.0,
            "min_population": params.min_population,
            "max_population": params.max_population,
            "exclude_aggregate_like_names": params.exclude_aggregate_like_names,
            "max_results": params.max_results,
        }

        rows = self.db.execute(sql, query_params).mappings().all()

        result: list[SettlementCandidate] = []

        for row in rows:
            population = int(row["population"])
            distance_to_route_km = float(row["distance_to_route_km"])

            station_distance_raw = row["distance_to_nearest_route_station_km"]
            distance_to_nearest_station_km = (
                float(station_distance_raw)
                if station_distance_raw is not None
                else None
            )

            estimated_connection_km = distance_to_route_km
            estimated_connection_cost = (
                estimated_connection_km * params.cost_per_km
                + params.station_cost
            )

            score = calculate_candidate_score(
                ScoreInput(
                    population=population,
                    max_population=params.max_population,
                    distance_to_route_km=distance_to_route_km,
                    distance_to_nearest_route_station_km=distance_to_nearest_station_km,
                    corridor_km=params.corridor_km,
                    station_access_km=params.station_access_km,
                    estimated_connection_cost=estimated_connection_cost,
                )
            )

            if score < params.min_score:
                continue

            served = (
                distance_to_nearest_station_km is not None
                and distance_to_nearest_station_km <= params.station_access_km
            )

            cost_per_1000_people = None
            if population > 0:
                cost_per_1000_people = estimated_connection_cost / (population / 1000.0)

            candidate = SettlementCandidate(
                id=int(row["id"]),
                name=row["canonical_name"],
                settlement_type=row["settlement_type"],
                region=row["region"],
                federal_district=row["federal_district"],
                population=population,

                distance_to_route_km=round(distance_to_route_km, 2),
                distance_to_nearest_route_station_km=(
                    round(distance_to_nearest_station_km, 2)
                    if distance_to_nearest_station_km is not None
                    else None
                ),

                served=served,
                score=score,
                attention_level=get_attention_level(score),

                estimated_connection_km=round(estimated_connection_km, 2),
                estimated_connection_cost=round(estimated_connection_cost, 2),
                cost_per_1000_people=(
                    round(cost_per_1000_people, 2)
                    if cost_per_1000_people is not None
                    else None
                ),

                geometry=json.loads(row["settlement_geojson"]),
                virtual_station=VirtualStationInfo(
                    geometry=json.loads(row["virtual_station_geojson"]),
                    distance_from_settlement_km=round(distance_to_route_km, 2),
                ),
            )

            result.append(candidate)

        result.sort(key=lambda item: item.score, reverse=True)
        return result

    def _build_summary(
        self,
        settlements: list[SettlementCandidate],
    ) -> AnalyticsSummary:
        if not settlements:
            return AnalyticsSummary(
                settlements_in_corridor=0,
                candidate_settlements=0,
                served_population=0,
                underserved_population=0,
                max_attention_score=0.0,
                avg_attention_score=0.0,
            )

        served_population = sum(
            item.population
            for item in settlements
            if item.served
        )

        underserved_population = sum(
            item.population
            for item in settlements
            if not item.served
        )

        scores = [item.score for item in settlements]

        return AnalyticsSummary(
            settlements_in_corridor=len(settlements),
            candidate_settlements=len([item for item in settlements if not item.served]),
            served_population=served_population,
            underserved_population=underserved_population,
            max_attention_score=round(max(scores), 2),
            avg_attention_score=round(sum(scores) / len(scores), 2),
        )

    def _detect_geometry_column(self, table_name: str) -> str:
        """
        Ищет geometry-колонку таблицы.

        Сначала через geometry_columns, потом через популярные имена.
        Возвращает безопасно процитированное имя колонки.
        """

        geometry_columns_sql = text(
            """
            SELECT f_geometry_column
            FROM geometry_columns
            WHERE f_table_schema = 'public'
              AND f_table_name = :table_name
            LIMIT 1
            """
        )

        row = self.db.execute(
            geometry_columns_sql,
            {"table_name": table_name},
        ).mappings().first()

        if row and row["f_geometry_column"]:
            return self._quote_identifier(row["f_geometry_column"])

        fallback_columns = [
            "geom",
            "geometry",
            "route_geom",
            "route_geometry",
            "virtual_osm_path",
        ]

        columns_sql = text(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = :table_name
            """
        )

        rows = self.db.execute(
            columns_sql,
            {"table_name": table_name},
        ).mappings().all()

        existing_columns = {row["column_name"] for row in rows}

        for column in fallback_columns:
            if column in existing_columns:
                return self._quote_identifier(column)

        raise ValueError(f"Could not detect geometry column for table '{table_name}'")

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        safe = identifier.replace('"', '""')
        return f'"{safe}"'