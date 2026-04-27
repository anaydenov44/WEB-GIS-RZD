import argparse
import json
from typing import Any, Callable

from sqlalchemy import text

from app.db import engine
from app.route_graph_matcher import (
    build_candidates_for_stop,
    infer_route_region_codes,
    load_global_station_catalog,
    load_route,
)


STATION_LINK_LIMIT = 6
STATION_LINK_MAX_DISTANCE_M = 450.0
ProgressCallback = Callable[[int, str, str, dict[str, Any]], None]


def print_section(title: str) -> None:
    print("\n" + "=" * 80)
    print(title)
    print("=" * 80)


def ensure_tables() -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rail_graph_nodes (
                    scope_key TEXT NOT NULL,
                    node_hash TEXT NOT NULL,
                    lon DOUBLE PRECISION NOT NULL,
                    lat DOUBLE PRECISION NOT NULL,
                    geom geometry(Point, 4326) NOT NULL,
                    PRIMARY KEY (scope_key, node_hash)
                );
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_rail_graph_nodes_scope_key
                ON rail_graph_nodes(scope_key);
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_rail_graph_nodes_geom
                ON rail_graph_nodes USING GIST(geom);
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS rail_graph_edges (
                    id BIGSERIAL PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    source_node_hash TEXT NOT NULL,
                    target_node_hash TEXT NOT NULL,
                    length_km DOUBLE PRECISION NOT NULL,
                    geom geometry(LineString, 4326) NOT NULL
                );
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_rail_graph_edges_scope_key
                ON rail_graph_edges(scope_key);
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_rail_graph_edges_source
                ON rail_graph_edges(scope_key, source_node_hash);
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_rail_graph_edges_target
                ON rail_graph_edges(scope_key, target_node_hash);
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_rail_graph_edges_geom
                ON rail_graph_edges USING GIST(geom);
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS station_graph_links (
                    scope_key TEXT NOT NULL,
                    station_id BIGINT NOT NULL,
                    node_hash TEXT NOT NULL,
                    link_distance_m DOUBLE PRECISION NOT NULL,
                    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
                    PRIMARY KEY (scope_key, station_id, node_hash)
                );
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_station_graph_links_scope_station
                ON station_graph_links(scope_key, station_id);
                """
            )
        )

        connection.execute(
            text(
                """
                CREATE INDEX IF NOT EXISTS idx_station_graph_links_scope_node
                ON station_graph_links(scope_key, node_hash);
                """
            )
        )


def build_scope_key(region_codes: list[str]) -> str:
    return "|".join(sorted(dict.fromkeys(region_codes)))


def infer_regions_for_route(route_id: int) -> list[str]:
    payload = load_route(route_id)
    stops = payload["stops"]

    catalog_payload = load_global_station_catalog()
    candidates_per_stop = [
        build_candidates_for_stop(stop, catalog_payload)
        for stop in stops
    ]

    diagnostics: dict = {}
    region_codes = infer_route_region_codes(
        stops=stops,
        candidates_per_stop=candidates_per_stop,
        diagnostics=diagnostics,
        logger_context={"route_id": route_id, "script": "build_route_scope_topology"},
    )

    print_section("INFERRED REGIONS")
    print(json.dumps(diagnostics.get("inferred_route_regions") or {}, ensure_ascii=False, indent=2))
    return region_codes


def clear_scope(scope_key: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM station_graph_links WHERE scope_key = :scope_key;"),
            {"scope_key": scope_key},
        )
        connection.execute(
            text("DELETE FROM rail_graph_edges WHERE scope_key = :scope_key;"),
            {"scope_key": scope_key},
        )
        connection.execute(
            text("DELETE FROM rail_graph_nodes WHERE scope_key = :scope_key;"),
            {"scope_key": scope_key},
        )


def build_topology(
    scope_key: str,
    region_codes: list[str],
    progress_callback: ProgressCallback | None = None,
) -> dict:
    def emit_progress(
        percent: int,
        stage_code: str,
        stage_label: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        if progress_callback is None:
            return

        progress_callback(
            percent,
            stage_code,
            stage_label,
            detail or {},
        )

    params: dict[str, object] = {
        "scope_key": scope_key,
        "station_link_limit": STATION_LINK_LIMIT,
        "station_link_max_distance_m": STATION_LINK_MAX_DISTANCE_M,
    }

    region_placeholders: list[str] = []
    for index, code in enumerate(region_codes):
        key = f"region_{index}"
        params[key] = code
        region_placeholders.append(f":{key}")

    region_sql = ", ".join(region_placeholders)

    point_hash_start_sql = """
        md5(
            concat(
                round(ST_X(ST_StartPoint(geom))::numeric, 6)::text,
                ',',
                round(ST_Y(ST_StartPoint(geom))::numeric, 6)::text
            )
        )
    """

    point_hash_end_sql = """
        md5(
            concat(
                round(ST_X(ST_EndPoint(geom))::numeric, 6)::text,
                ',',
                round(ST_Y(ST_EndPoint(geom))::numeric, 6)::text
            )
        )
    """

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS tmp_scope_raw_parts;"))
        connection.execute(text("DROP TABLE IF EXISTS tmp_scope_noded_parts;"))
        connection.execute(text("DROP TABLE IF EXISTS tmp_scope_segments;"))
        connection.execute(text("DROP TABLE IF EXISTS tmp_scope_nodes;"))
        connection.execute(text("DROP TABLE IF EXISTS tmp_scope_station_links;"))

        connection.execute(
            text(
                f"""
                CREATE TEMP TABLE tmp_scope_raw_parts AS
                SELECT
                    l.id AS line_id,
                    dump.path[1] AS part_index,
                    dump.geom AS geom
                FROM rail_lines l
                CROSS JOIN LATERAL ST_Dump(ST_LineMerge(l.geom)) AS dump
                WHERE
                    COALESCE(l.is_service_line, FALSE) = FALSE
                    AND (
                        COALESCE(l.is_visible_default, FALSE) = TRUE
                        OR COALESCE(l.is_main_passenger_line, FALSE) = TRUE
                    )
                    AND l.region_code IN ({region_sql})
                    AND dump.geom IS NOT NULL
                    AND GeometryType(dump.geom) = 'LINESTRING'
                    AND ST_NPoints(dump.geom) >= 2;
                """
            ),
            params,
        )

        raw_parts_count = connection.execute(
            text("SELECT COUNT(*) FROM tmp_scope_raw_parts;")
        ).scalar_one()

        emit_progress(
            45,
            "raw_parts",
            "Выбраны исходные линии для topology graph",
            {"raw_parts_count": int(raw_parts_count)},
        )

        connection.execute(
            text(
                """
                CREATE TEMP TABLE tmp_scope_noded_parts AS
                WITH network_union AS (
                    SELECT ST_Node(ST_UnaryUnion(ST_Collect(geom))) AS geom
                    FROM tmp_scope_raw_parts
                )
                SELECT
                    dump.path[1] AS part_index,
                    dump.geom AS geom
                FROM network_union u
                CROSS JOIN LATERAL ST_Dump(u.geom) AS dump
                WHERE
                    dump.geom IS NOT NULL
                    AND GeometryType(dump.geom) = 'LINESTRING'
                    AND ST_NPoints(dump.geom) >= 2;
                """
            )
        )

        noded_parts_count = connection.execute(
            text("SELECT COUNT(*) FROM tmp_scope_noded_parts;")
        ).scalar_one()

        emit_progress(
            58,
            "noded_parts",
            "Линии разбиты на топологические сегменты",
            {"noded_parts_count": int(noded_parts_count)},
        )

        connection.execute(
            text(
                f"""
                CREATE TEMP TABLE tmp_scope_segments AS
                SELECT DISTINCT ON (segment_hash)
                    segment_hash,
                    geom,
                    {point_hash_start_sql} AS source_node_hash,
                    {point_hash_end_sql} AS target_node_hash,
                    ST_StartPoint(geom) AS source_geom,
                    ST_EndPoint(geom) AS target_geom,
                    ST_Length(geom::geography) / 1000.0 AS length_km
                FROM (
                    SELECT
                        md5(ST_AsBinary(geom)) AS segment_hash,
                        geom
                    FROM tmp_scope_noded_parts
                ) s
                WHERE ST_Length(geom::geography) > 0.001
                ORDER BY segment_hash;
                """
            )
        )

        segments_count = connection.execute(
            text("SELECT COUNT(*) FROM tmp_scope_segments;")
        ).scalar_one()

        emit_progress(
            68,
            "segments",
            "Подготовлены рёбра topology graph",
            {"segments_count": int(segments_count)},
        )

        connection.execute(
            text(
                """
                CREATE TEMP TABLE tmp_scope_nodes AS
                SELECT DISTINCT ON (node_hash)
                    node_hash,
                    ST_X(node_geom) AS lon,
                    ST_Y(node_geom) AS lat,
                    node_geom AS geom
                FROM (
                    SELECT
                        source_node_hash AS node_hash,
                        source_geom AS node_geom
                    FROM tmp_scope_segments
                    UNION ALL
                    SELECT
                        target_node_hash AS node_hash,
                        target_geom AS node_geom
                    FROM tmp_scope_segments
                ) q
                ORDER BY node_hash;
                """
            )
        )

        nodes_count = connection.execute(
            text("SELECT COUNT(*) FROM tmp_scope_nodes;")
        ).scalar_one()

        emit_progress(
            76,
            "nodes",
            "Подготовлены узлы topology graph",
            {"nodes_count": int(nodes_count)},
        )

        connection.execute(
            text(
                """
                INSERT INTO rail_graph_nodes (
                    scope_key,
                    node_hash,
                    lon,
                    lat,
                    geom
                )
                SELECT
                    :scope_key,
                    node_hash,
                    lon,
                    lat,
                    geom
                FROM tmp_scope_nodes;
                """
            ),
            {"scope_key": scope_key},
        )

        persisted_nodes_count = connection.execute(
            text("SELECT COUNT(*) FROM rail_graph_nodes WHERE scope_key = :scope_key;"),
            {"scope_key": scope_key},
        ).scalar_one()

        connection.execute(
            text(
                """
                INSERT INTO rail_graph_edges (
                    scope_key,
                    source_node_hash,
                    target_node_hash,
                    length_km,
                    geom
                )
                SELECT
                    :scope_key,
                    source_node_hash,
                    target_node_hash,
                    length_km,
                    geom
                FROM tmp_scope_segments
                WHERE source_node_hash <> target_node_hash;
                """
            ),
            {"scope_key": scope_key},
        )

        persisted_edges_count = connection.execute(
            text("SELECT COUNT(*) FROM rail_graph_edges WHERE scope_key = :scope_key;"),
            {"scope_key": scope_key},
        ).scalar_one()

        emit_progress(
            84,
            "persist_edges",
            "Рёбра topology graph сохранены",
            {"persisted_edges_count": int(persisted_edges_count)},
        )

        connection.execute(
            text(
                f"""
                CREATE TEMP TABLE tmp_scope_station_links AS
                WITH region_stations AS (
                    SELECT
                        s.id AS station_id,
                        s.geom
                    FROM stations s
                    WHERE
                        s.is_visible_default = TRUE
                        AND s.geom IS NOT NULL
                        AND s.region_code IN ({region_sql})
                ),
                nearest_nodes AS (
                    SELECT
                        rs.station_id,
                        nn.node_hash,
                        nn.link_distance_m,
                        ROW_NUMBER() OVER (
                            PARTITION BY rs.station_id
                            ORDER BY nn.link_distance_m, nn.node_hash
                        ) AS rn
                    FROM region_stations rs
                    CROSS JOIN LATERAL (
                        SELECT
                            n.node_hash,
                            ST_Distance(rs.geom::geography, n.geom::geography) AS link_distance_m
                        FROM rail_graph_nodes n
                        WHERE n.scope_key = :scope_key
                        ORDER BY rs.geom <-> n.geom, n.node_hash
                        LIMIT :station_link_limit
                    ) nn
                )
                SELECT
                    station_id,
                    node_hash,
                    link_distance_m,
                    (rn = 1) AS is_primary
                FROM nearest_nodes
                WHERE
                    rn = 1
                    OR link_distance_m <= :station_link_max_distance_m;
                """
            ),
            params,
        )

        station_hits_count = connection.execute(
            text("SELECT COUNT(*) FROM tmp_scope_station_links;")
        ).scalar_one()

        connection.execute(
            text(
                """
                INSERT INTO station_graph_links (
                    scope_key,
                    station_id,
                    node_hash,
                    link_distance_m,
                    is_primary
                )
                SELECT
                    :scope_key,
                    station_id,
                    node_hash,
                    link_distance_m,
                    is_primary
                FROM tmp_scope_station_links;
                """
            ),
            {"scope_key": scope_key},
        )

        persisted_station_links_count = connection.execute(
            text("SELECT COUNT(*) FROM station_graph_links WHERE scope_key = :scope_key;"),
            {"scope_key": scope_key},
        ).scalar_one()

        emit_progress(
            94,
            "station_links",
            "Станции связаны с topology graph",
            {"persisted_station_links_count": int(persisted_station_links_count)},
        )

    return {
        "raw_parts_count": int(raw_parts_count),
        "noded_parts_count": int(noded_parts_count),
        "segments_count": int(segments_count),
        "nodes_count": int(nodes_count),
        "persisted_nodes_count": int(persisted_nodes_count),
        "persisted_edges_count": int(persisted_edges_count),
        "station_hits_count": int(station_hits_count),
        "persisted_station_links_count": int(persisted_station_links_count),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route-id", type=int, required=True)
    args = parser.parse_args()

    route_id = int(args.route_id)

    region_codes = infer_regions_for_route(route_id)
    scope_key = build_scope_key(region_codes)

    print_section("INPUT")
    print(f"region_codes = {region_codes}")
    print(f"scope_key = {scope_key}")
    print(f"station_link_limit = {STATION_LINK_LIMIT}")
    print(f"station_link_max_distance_m = {STATION_LINK_MAX_DISTANCE_M}")

    print_section("ENSURE TABLES")
    ensure_tables()
    print("OK")

    print_section("CLEAR OLD SCOPE")
    clear_scope(scope_key)
    print("OK")

    print_section("BUILD TOPOLOGY")
    stats = build_topology(scope_key, region_codes)
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    print_section("DONE")
    print("Topology build finished successfully.")


if __name__ == "__main__":
    main()
