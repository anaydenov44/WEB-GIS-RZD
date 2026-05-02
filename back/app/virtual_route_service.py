import json
import math
import time
from typing import Any

from sqlalchemy import text

from app.db import engine
from app.route_graph_matcher import (
    Candidate,
    build_network_data,
    build_simple_linestring,
    dijkstra_topology_path,
    expand_corridor_region_codes,
    get_station_link_options_for_candidate,
    merge_coordinate_sequences,
)


MAX_VIRTUAL_STATION_CONNECTOR_KM = 1.5
SOFT_VIRTUAL_STATION_CONNECTOR_KM = 0.35
MAX_LINK_OPTIONS_PER_SIDE = 32
NEAREST_GRAPH_NODE_SEARCH_RADIUS_M = 1500
NEAREST_GRAPH_NODE_LIMIT = 16
NEAREST_GRAPH_EDGE_LIMIT = 12

EDGE_SNAP_LINK_SOURCES = {
    "edge_snap",
    "nearest_edge_snap",
    "scope_edge_snap",
    "nearest_scope_edge_snap_source",
    "nearest_scope_edge_snap_target",
}


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _haversine_km(
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
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )

    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _as_coord_pair(value: Any) -> list[float] | None:
    if value is None:
        return None

    if isinstance(value, dict):
        lon = value.get("lon")
        lat = value.get("lat")

        if lon is None:
            lon = value.get("x")
        if lat is None:
            lat = value.get("y")

        if lon is None or lat is None:
            return None

        return [float(lon), float(lat)]

    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return [float(value[0]), float(value[1])]

    return None


def _line_coords_from_geojson(value: str | None) -> list[list[float]]:
    if not value:
        return []

    try:
        data = json.loads(value)
    except Exception:
        return []

    geometry_type = data.get("type")

    if geometry_type == "LineString":
        return [
            [float(item[0]), float(item[1])]
            for item in data.get("coordinates") or []
            if isinstance(item, (list, tuple)) and len(item) >= 2
        ]

    if geometry_type == "MultiLineString":
        result: list[list[float]] = []

        for line in data.get("coordinates") or []:
            for item in line:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    result.append([float(item[0]), float(item[1])])

        return result

    return []


def _append_coord_unique(
    result: list[list[float]],
    coord: list[float],
    *,
    epsilon: float = 1e-10,
) -> None:
    if not result:
        result.append(coord)
        return

    last = result[-1]

    if abs(last[0] - coord[0]) <= epsilon and abs(last[1] - coord[1]) <= epsilon:
        return

    result.append(coord)


def _merge_connector_coords(*sequences: list[list[float]]) -> list[list[float]]:
    result: list[list[float]] = []

    for sequence in sequences:
        for coord in sequence or []:
            if coord is None or len(coord) < 2:
                continue

            _append_coord_unique(result, [float(coord[0]), float(coord[1])])

    return result


def _is_edge_snap_link(link: dict[str, Any]) -> bool:
    source = str(link.get("source") or "")
    return source in EDGE_SNAP_LINK_SOURCES or link.get("station_snap_distance_km") is not None


def _link_air_distance_km(link: dict[str, Any]) -> float:
    """
    Для обычного station_graph_link это station -> graph node.
    Для edge-snap это station -> closest point on edge, а не расстояние до endpoint ребра.

    Это важно: у станции точка может лежать прямо на линии, но ближайший endpoint ребра далеко.
    Поэтому edge-snap фильтруем по snap distance, а не по полной link_distance_km.
    """

    if _is_edge_snap_link(link):
        snap_distance = link.get("station_snap_distance_km")
        if snap_distance is None:
            snap_distance = link.get("air_distance_km")
        if snap_distance is not None:
            return float(snap_distance or 0.0)

    return float(link.get("link_distance_km") or 0.0)


def _coord_distance_sq(a: list[float], b: list[float]) -> float:
    return (float(a[0]) - float(b[0])) ** 2 + (float(a[1]) - float(b[1])) ** 2


def _extract_node_lon_lat(
    node_hash: str,
    node_coords: dict[str, Any],
) -> tuple[float, float] | None:
    pair = _as_coord_pair(node_coords.get(str(node_hash)))

    if pair is None:
        return None

    return float(pair[0]), float(pair[1])


def _get_node_coord(
    node_hash: str | None,
    node_coords: dict[str, Any],
) -> list[float] | None:
    if not node_hash:
        return None

    return _as_coord_pair(node_coords.get(str(node_hash)))


def _orient_edge_geometry_coords(
    *,
    edge: dict[str, Any],
    node_coords: dict[str, Any],
) -> list[list[float]]:
    raw_coords = edge.get("geometry_coords") or []
    coords: list[list[float]] = []

    for item in raw_coords:
        pair = _as_coord_pair(item)
        if pair is not None:
            coords.append(pair)

    if len(coords) < 2:
        return []

    from_node_coord = _get_node_coord(edge.get("from_node_hash"), node_coords)
    to_node_coord = _get_node_coord(edge.get("to_node_hash"), node_coords)

    if from_node_coord is None or to_node_coord is None:
        return coords

    normal_score = (
        _coord_distance_sq(coords[0], from_node_coord)
        + _coord_distance_sq(coords[-1], to_node_coord)
    )
    reversed_score = (
        _coord_distance_sq(coords[-1], from_node_coord)
        + _coord_distance_sq(coords[0], to_node_coord)
    )

    if reversed_score < normal_score:
        return list(reversed(coords))

    return coords


def build_graph_coordinates_from_edge_chain(
    *,
    graph_path: dict[str, Any],
    node_coords: dict[str, Any],
) -> list[list[float]]:
    sequences: list[list[list[float]]] = []

    for edge in graph_path.get("edge_chain") or []:
        coords = _orient_edge_geometry_coords(
            edge=edge,
            node_coords=node_coords,
        )

        if len(coords) >= 2:
            sequences.append(coords)

    if sequences:
        return merge_coordinate_sequences(sequences)

    fallback_coords = graph_path.get("coordinates") or []
    result: list[list[float]] = []

    for item in fallback_coords:
        pair = _as_coord_pair(item)
        if pair is not None:
            result.append(pair)

    return result


def load_station_for_virtual_route(station_id: int) -> dict[str, Any]:
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

    item = dict(row._mapping)

    if item.get("lon") is None or item.get("lat") is None:
        raise ValueError(f"Station has no geometry: {station_id}")

    return item


def station_to_virtual_candidate(station: dict[str, Any]) -> Candidate:
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
        match_method="virtual_route_selected_station",
        match_reason="selected_by_user_for_virtual_osm_path",
        code_value=None,
    )


def load_scope_station_link_options(
    *,
    station: dict[str, Any],
    network: dict[str, Any],
    limit: int = MAX_LINK_OPTIONS_PER_SIDE,
) -> list[dict[str, Any]]:
    station_id = int(station["id"])
    station_lon = float(station["lon"])
    station_lat = float(station["lat"])
    scope_key = network.get("scope_key")
    node_coords = network.get("node_coords") or {}

    if not scope_key:
        return []

    query = text("""
        WITH station_nodes AS (
            SELECT DISTINCT
                node_hash
            FROM station_graph_links
            WHERE station_id = :station_id
              AND node_hash IS NOT NULL
        )
        SELECT
            sn.node_hash,
            COUNT(e.id) AS degree_in_scope
        FROM station_nodes sn
        JOIN rail_graph_edges e
          ON e.scope_key = :scope_key
         AND (
              e.source_node_hash = sn.node_hash
              OR e.target_node_hash = sn.node_hash
         )
        GROUP BY sn.node_hash
        HAVING COUNT(e.id) > 0
        ORDER BY COUNT(e.id) DESC, sn.node_hash
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "station_id": station_id,
                "scope_key": scope_key,
                "limit": limit,
            },
        ).mappings().all()

    result: list[dict[str, Any]] = []

    for row in rows:
        node_hash = str(row["node_hash"])
        coords = _extract_node_lon_lat(node_hash, node_coords)

        if coords is None:
            continue

        node_lon, node_lat = coords
        link_distance_km = _haversine_km(
            station_lon,
            station_lat,
            node_lon,
            node_lat,
        )

        result.append(
            {
                "station_id": station_id,
                "node_hash": node_hash,
                "node_lon": node_lon,
                "node_lat": node_lat,
                "link_distance_km": link_distance_km,
                "link_distance_m": link_distance_km * 1000.0,
                "degree_in_scope": int(row["degree_in_scope"] or 0),
                "source": "scope_station_graph_links",
            }
        )

    return result


def load_nearest_scope_node_options(
    *,
    station: dict[str, Any],
    network: dict[str, Any],
    radius_m: int = NEAREST_GRAPH_NODE_SEARCH_RADIUS_M,
    limit: int = NEAREST_GRAPH_NODE_LIMIT,
) -> list[dict[str, Any]]:
    station_id = int(station["id"])
    scope_key = network.get("scope_key")

    if not scope_key:
        return []

    query = text("""
        WITH station_point AS (
            SELECT
                CASE
                    WHEN ST_SRID(geom) = 4326 THEN geom
                    ELSE ST_Transform(geom, 4326)
                END AS geom
            FROM stations
            WHERE id = :station_id
            LIMIT 1
        ),
        endpoint_points AS (
            SELECT
                e.source_node_hash AS node_hash,
                ST_StartPoint(e.geom) AS geom
            FROM rail_graph_edges e
            CROSS JOIN station_point sp
            WHERE e.scope_key = :scope_key
              AND e.source_node_hash IS NOT NULL
              AND ST_DWithin(
                    ST_StartPoint(e.geom)::geography,
                    sp.geom::geography,
                    :radius_m
              )

            UNION ALL

            SELECT
                e.target_node_hash AS node_hash,
                ST_EndPoint(e.geom) AS geom
            FROM rail_graph_edges e
            CROSS JOIN station_point sp
            WHERE e.scope_key = :scope_key
              AND e.target_node_hash IS NOT NULL
              AND ST_DWithin(
                    ST_EndPoint(e.geom)::geography,
                    sp.geom::geography,
                    :radius_m
              )
        ),
        nearest_per_node AS (
            SELECT DISTINCT ON (ep.node_hash)
                ep.node_hash,
                ep.geom,
                ST_DistanceSphere(ep.geom, sp.geom) AS distance_m
            FROM endpoint_points ep
            CROSS JOIN station_point sp
            ORDER BY
                ep.node_hash,
                ST_DistanceSphere(ep.geom, sp.geom) ASC
        ),
        node_degrees AS (
            SELECT
                node_hash,
                COUNT(*) AS degree_in_scope
            FROM (
                SELECT
                    source_node_hash AS node_hash
                FROM rail_graph_edges
                WHERE scope_key = :scope_key
                  AND source_node_hash IS NOT NULL

                UNION ALL

                SELECT
                    target_node_hash AS node_hash
                FROM rail_graph_edges
                WHERE scope_key = :scope_key
                  AND target_node_hash IS NOT NULL
            ) x
            GROUP BY node_hash
        )
        SELECT
            n.node_hash,
            ST_X(n.geom) AS node_lon,
            ST_Y(n.geom) AS node_lat,
            n.distance_m,
            COALESCE(d.degree_in_scope, 0) AS degree_in_scope
        FROM nearest_per_node n
        LEFT JOIN node_degrees d
          ON d.node_hash = n.node_hash
        ORDER BY
            n.distance_m ASC,
            COALESCE(d.degree_in_scope, 0) DESC,
            n.node_hash
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "station_id": station_id,
                "scope_key": scope_key,
                "radius_m": radius_m,
                "limit": limit,
            },
        ).mappings().all()

    result: list[dict[str, Any]] = []

    for row in rows:
        distance_m = float(row["distance_m"] or 0.0)

        result.append(
            {
                "station_id": station_id,
                "node_hash": str(row["node_hash"]),
                "node_lon": float(row["node_lon"]),
                "node_lat": float(row["node_lat"]),
                "link_distance_km": distance_m / 1000.0,
                "link_distance_m": distance_m,
                "degree_in_scope": int(row["degree_in_scope"] or 0),
                "source": "nearest_scope_graph_node",
            }
        )

    return result


def load_nearest_scope_edge_snap_options(
    *,
    station: dict[str, Any],
    network: dict[str, Any],
    radius_m: int = NEAREST_GRAPH_NODE_SEARCH_RADIUS_M,
    limit: int = NEAREST_GRAPH_EDGE_LIMIT,
) -> list[dict[str, Any]]:
    station_id = int(station["id"])
    station_lon = float(station["lon"])
    station_lat = float(station["lat"])
    scope_key = network.get("scope_key")

    if not scope_key:
        return []

    query = text("""
        WITH station_point AS (
            SELECT
                CASE
                    WHEN ST_SRID(geom) = 4326 THEN geom
                    ELSE ST_Transform(geom, 4326)
                END AS geom
            FROM stations
            WHERE id = :station_id
            LIMIT 1
        ),
        nearest_edges AS (
            SELECT
                e.id AS edge_id,
                e.source_node_hash,
                e.target_node_hash,
                e.length_km,
                e.edge_source,
                e.is_virtual_connector,
                e.geom,
                ST_StartPoint(e.geom) AS source_geom,
                ST_EndPoint(e.geom) AS target_geom,
                ST_ClosestPoint(e.geom, sp.geom) AS snap_geom,
                ST_LineLocatePoint(e.geom, ST_ClosestPoint(e.geom, sp.geom)) AS snap_fraction,
                ST_DistanceSphere(e.geom, sp.geom) AS station_snap_distance_m
            FROM rail_graph_edges e
            CROSS JOIN station_point sp
            WHERE e.scope_key = :scope_key
              AND e.source_node_hash IS NOT NULL
              AND e.target_node_hash IS NOT NULL
              AND COALESCE(e.is_virtual_connector, FALSE) = FALSE
              AND ST_DWithin(e.geom::geography, sp.geom::geography, :radius_m)
            ORDER BY e.geom <-> sp.geom
            LIMIT :limit
        ),
        edge_parts AS (
            SELECT
                edge_id,
                source_node_hash,
                target_node_hash,
                length_km,
                edge_source,
                is_virtual_connector,
                source_geom,
                target_geom,
                snap_geom,
                snap_fraction,
                station_snap_distance_m,
                ST_Length(
                    ST_LineSubstring(
                        geom,
                        0,
                        LEAST(GREATEST(snap_fraction, 0), 1)
                    )::geography
                ) AS snap_to_source_m,
                ST_Length(
                    ST_LineSubstring(
                        geom,
                        LEAST(GREATEST(snap_fraction, 0), 1),
                        1
                    )::geography
                ) AS snap_to_target_m,
                ST_AsGeoJSON(
                    ST_Reverse(
                        ST_LineSubstring(
                            geom,
                            0,
                            LEAST(GREATEST(snap_fraction, 0), 1)
                        )
                    )
                ) AS source_rail_geojson,
                ST_AsGeoJSON(
                    ST_LineSubstring(
                        geom,
                        LEAST(GREATEST(snap_fraction, 0), 1),
                        1
                    )
                ) AS target_rail_geojson
            FROM nearest_edges
        )
        SELECT
            edge_id,
            source_node_hash,
            target_node_hash,
            length_km,
            edge_source,
            is_virtual_connector,
            ST_X(source_geom) AS source_lon,
            ST_Y(source_geom) AS source_lat,
            ST_X(target_geom) AS target_lon,
            ST_Y(target_geom) AS target_lat,
            ST_X(snap_geom) AS snap_lon,
            ST_Y(snap_geom) AS snap_lat,
            station_snap_distance_m,
            snap_to_source_m,
            snap_to_target_m,
            source_rail_geojson,
            target_rail_geojson
        FROM edge_parts
        ORDER BY station_snap_distance_m ASC, edge_id;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "station_id": station_id,
                "scope_key": scope_key,
                "radius_m": radius_m,
                "limit": limit,
            },
        ).mappings().all()

    result: list[dict[str, Any]] = []
    station_coord = [station_lon, station_lat]

    for row in rows:
        snap_coord = [float(row["snap_lon"]), float(row["snap_lat"])]
        station_snap_m = float(row["station_snap_distance_m"] or 0.0)

        source_node_hash = str(row["source_node_hash"])
        source_coord = [float(row["source_lon"]), float(row["source_lat"])]
        source_rail_coords = _line_coords_from_geojson(row.get("source_rail_geojson"))

        if len(source_rail_coords) < 2:
            source_rail_coords = [snap_coord, source_coord]

        source_connector_coords = _merge_connector_coords(
            [station_coord, snap_coord],
            source_rail_coords,
        )

        source_link_distance_m = station_snap_m + float(row["snap_to_source_m"] or 0.0)

        result.append(
            {
                "station_id": station_id,
                "node_hash": source_node_hash,
                "node_lon": source_coord[0],
                "node_lat": source_coord[1],
                "link_distance_km": source_link_distance_m / 1000.0,
                "link_distance_m": source_link_distance_m,
                "station_snap_distance_km": station_snap_m / 1000.0,
                "station_snap_distance_m": station_snap_m,
                "degree_in_scope": 0,
                "source": "nearest_scope_edge_snap_source",
                "snap_edge_id": row["edge_id"],
                "edge_source": row.get("edge_source"),
                "connector_geometry_coords": source_connector_coords,
            }
        )

        target_node_hash = str(row["target_node_hash"])
        target_coord = [float(row["target_lon"]), float(row["target_lat"])]
        target_rail_coords = _line_coords_from_geojson(row.get("target_rail_geojson"))

        if len(target_rail_coords) < 2:
            target_rail_coords = [snap_coord, target_coord]

        target_connector_coords = _merge_connector_coords(
            [station_coord, snap_coord],
            target_rail_coords,
        )

        target_link_distance_m = station_snap_m + float(row["snap_to_target_m"] or 0.0)

        result.append(
            {
                "station_id": station_id,
                "node_hash": target_node_hash,
                "node_lon": target_coord[0],
                "node_lat": target_coord[1],
                "link_distance_km": target_link_distance_m / 1000.0,
                "link_distance_m": target_link_distance_m,
                "station_snap_distance_km": station_snap_m / 1000.0,
                "station_snap_distance_m": station_snap_m,
                "degree_in_scope": 0,
                "source": "nearest_scope_edge_snap_target",
                "snap_edge_id": row["edge_id"],
                "edge_source": row.get("edge_source"),
                "connector_geometry_coords": target_connector_coords,
            }
        )

    result.sort(
        key=lambda item: (
            float(item.get("station_snap_distance_km") or 0.0),
            float(item.get("link_distance_km") or 0.0),
            str(item.get("node_hash") or ""),
        )
    )

    return result[: limit * 2]


def merge_link_options(
    *,
    base_links: list[dict[str, Any]],
    extra_links: list[dict[str, Any]],
    limit: int = MAX_LINK_OPTIONS_PER_SIDE,
) -> list[dict[str, Any]]:
    by_node_hash: dict[str, dict[str, Any]] = {}

    for source_priority, links in enumerate([base_links, extra_links]):
        for link in links or []:
            node_hash = link.get("node_hash")

            if not node_hash:
                continue

            node_hash = str(node_hash)

            normalized = dict(link)
            normalized["node_hash"] = node_hash
            normalized["_source_priority"] = source_priority

            if normalized.get("link_distance_km") is None:
                if normalized.get("link_distance_m") is not None:
                    normalized["link_distance_km"] = (
                        float(normalized["link_distance_m"]) / 1000.0
                    )
                else:
                    normalized["link_distance_km"] = 0.0

            if normalized.get("link_distance_m") is None:
                normalized["link_distance_m"] = (
                    float(normalized.get("link_distance_km") or 0.0) * 1000.0
                )

            if normalized.get("degree_in_scope") is None:
                normalized["degree_in_scope"] = 0

            existing = by_node_hash.get(node_hash)

            if existing is None:
                by_node_hash[node_hash] = normalized
                continue

            existing_air_distance = _link_air_distance_km(existing)
            current_air_distance = _link_air_distance_km(normalized)

            existing_distance = float(existing.get("link_distance_km") or 0.0)
            current_distance = float(normalized.get("link_distance_km") or 0.0)

            existing_degree = int(existing.get("degree_in_scope") or 0)
            current_degree = int(normalized.get("degree_in_scope") or 0)

            existing_source_priority = int(existing.get("_source_priority") or 0)
            current_source_priority = int(normalized.get("_source_priority") or 0)

            if (
                current_air_distance < existing_air_distance
                or (
                    current_air_distance == existing_air_distance
                    and current_distance < existing_distance
                )
                or (
                    current_air_distance == existing_air_distance
                    and current_distance == existing_distance
                    and current_degree > existing_degree
                )
                or (
                    current_air_distance == existing_air_distance
                    and current_distance == existing_distance
                    and current_degree == existing_degree
                    and current_source_priority > existing_source_priority
                )
            ):
                by_node_hash[node_hash] = normalized

    merged = list(by_node_hash.values())

    merged.sort(
        key=lambda item: (
            _link_air_distance_km(item),
            float(item.get("link_distance_km") or 0.0),
            -int(item.get("degree_in_scope") or 0),
            str(item.get("node_hash") or ""),
        )
    )

    for item in merged:
        item.pop("_source_priority", None)

    return merged[:limit]


def filter_link_options_by_distance(
    links: list[dict[str, Any]],
    *,
    max_connector_km: float = MAX_VIRTUAL_STATION_CONNECTOR_KM,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for link in links:
        # Для edge-snap проверяем расстояние station -> snap point.
        # Для обычного link проверяем station -> node.
        air_distance_km = _link_air_distance_km(link)

        if air_distance_km <= max_connector_km:
            accepted.append(link)
        else:
            rejected.append(link)

    return accepted, rejected


def compact_link_for_diagnostics(link: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_hash": link.get("node_hash"),
        "node_lon": link.get("node_lon"),
        "node_lat": link.get("node_lat"),
        "link_distance_km": round(float(link.get("link_distance_km") or 0.0), 6),
        "link_distance_m": round(float(link.get("link_distance_km") or 0.0) * 1000, 2),
        "station_snap_distance_km": (
            round(float(link.get("station_snap_distance_km") or 0.0), 6)
            if link.get("station_snap_distance_km") is not None
            else None
        ),
        "station_snap_distance_m": (
            round(float(link.get("station_snap_distance_m") or 0.0), 2)
            if link.get("station_snap_distance_m") is not None
            else None
        ),
        "air_distance_km_for_filter": round(float(_link_air_distance_km(link)), 6),
        "degree_in_scope": link.get("degree_in_scope"),
        "source": link.get("source"),
        "snap_edge_id": link.get("snap_edge_id"),
        "edge_source": link.get("edge_source"),
        "connector_geometry_coords_count": len(link.get("connector_geometry_coords") or []),
    }


def build_virtual_pair_coordinates(
    *,
    origin_candidate: Candidate,
    destination_candidate: Candidate,
    start_link: dict[str, Any],
    end_link: dict[str, Any],
    graph_coords: list[list[float]] | None,
) -> list[list[float]]:
    sequences: list[list[list[float]]] = []

    connector_start_air_km = _link_air_distance_km(start_link)
    custom_start_connector = start_link.get("connector_geometry_coords")

    if custom_start_connector and connector_start_air_km <= MAX_VIRTUAL_STATION_CONNECTOR_KM:
        sequences.append(custom_start_connector)
    else:
        connector_start = [
            [origin_candidate.lon, origin_candidate.lat],
            [float(start_link["node_lon"]), float(start_link["node_lat"])],
        ]

        if (
            connector_start[0] != connector_start[1]
            and connector_start_air_km <= MAX_VIRTUAL_STATION_CONNECTOR_KM
        ):
            sequences.append(connector_start)

    if graph_coords:
        sequences.append(graph_coords)

    connector_end_air_km = _link_air_distance_km(end_link)
    custom_end_connector = end_link.get("connector_geometry_coords")

    if custom_end_connector and connector_end_air_km <= MAX_VIRTUAL_STATION_CONNECTOR_KM:
        sequences.append(list(reversed(custom_end_connector)))
    else:
        connector_end = [
            [float(end_link["node_lon"]), float(end_link["node_lat"])],
            [destination_candidate.lon, destination_candidate.lat],
        ]

        if (
            connector_end[0] != connector_end[1]
            and connector_end_air_km <= MAX_VIRTUAL_STATION_CONNECTOR_KM
        ):
            sequences.append(connector_end)

    coordinates = merge_coordinate_sequences(sequences)

    if len(coordinates) < 2:
        coordinates = [
            [origin_candidate.lon, origin_candidate.lat],
            [destination_candidate.lon, destination_candidate.lat],
        ]

    return coordinates


def build_virtual_geojson(
    *,
    geometry: dict[str, Any] | None,
    route_payload: dict[str, Any],
    network_segments: list[dict[str, Any]],
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []

    if geometry is not None:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_kind": "virtual_route_path",
                    "geometry_source": "virtual_osm_path",
                    "origin_station_id": route_payload["origin_station_id"],
                    "destination_station_id": route_payload["destination_station_id"],
                },
                "geometry": geometry,
            }
        )

    for segment in network_segments:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "feature_kind": "virtual_route_network_segment",
                    "segment_index": segment.get("segment_index"),
                    "edge_index": segment.get("edge_index"),
                    "length_km": segment.get("length_km"),
                    "segment_source": segment.get("segment_source"),
                    "edge_source": segment.get("edge_source"),
                    "is_virtual_connector": segment.get("is_virtual_connector"),
                },
                "geometry": segment.get("geometry"),
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def build_virtual_route_path(
    *,
    origin_station_id: int,
    destination_station_id: int,
    scope_region_codes: list[str] | None = None,
) -> dict[str, Any]:
    started_ms = _now_ms()

    diagnostics: dict[str, Any] = {
        "origin_station_id": origin_station_id,
        "destination_station_id": destination_station_id,
        "scope_region_codes_input": scope_region_codes,
        "max_virtual_station_connector_km": MAX_VIRTUAL_STATION_CONNECTOR_KM,
        "nearest_graph_node_search_radius_m": NEAREST_GRAPH_NODE_SEARCH_RADIUS_M,
        "timings_ms": {},
        "network": {},
        "link_options": {},
        "selected_path": None,
        "checked_link_pairs": [],
        "errors": [],
    }

    try:
        t0 = _now_ms()
        origin_station = load_station_for_virtual_route(origin_station_id)
        destination_station = load_station_for_virtual_route(destination_station_id)
        diagnostics["timings_ms"]["load_stations"] = round(_now_ms() - t0, 2)

        origin_region = origin_station.get("region_code")
        destination_region = destination_station.get("region_code")

        if scope_region_codes:
            region_codes: list[str] = []
            seen = set()

            for code in scope_region_codes:
                normalized = str(code or "").strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                region_codes.append(normalized)

            missing_regions = []
            missing_seen = set()

            for code in [origin_region, destination_region]:
                if code and code not in region_codes and code not in missing_seen:
                    missing_seen.add(code)
                    missing_regions.append(code)

            diagnostics["scope_region_codes"] = region_codes

            if missing_regions:
                return {
                    "status": "region_not_loaded",
                    "message": (
                        "Один из выбранных пунктов находится в округе, который сейчас не загружен. "
                        "Загрузите соответствующий округ, чтобы построить виртуальный путь."
                    ),
                    "missing_region_codes": missing_regions,
                    "origin_station": origin_station,
                    "destination_station": destination_station,
                    "region_codes": region_codes,
                    "geometry": None,
                    "network_segments": [],
                    "diagnostics": diagnostics,
                }
        else:
            region_codes = []

            for code in [origin_region, destination_region]:
                if code and code not in region_codes:
                    region_codes.append(code)

            diagnostics["scope_region_codes"] = region_codes

        if not region_codes:
            return {
                "status": "failed",
                "message": "Не удалось определить region_code для выбранных станций",
                "geometry": None,
                "network_segments": [],
                "diagnostics": diagnostics,
            }

        region_codes = expand_corridor_region_codes(region_codes)
        diagnostics["scope_region_codes"] = region_codes

        origin_candidate = station_to_virtual_candidate(origin_station)
        destination_candidate = station_to_virtual_candidate(destination_station)

        t0 = _now_ms()
        network = build_network_data(
            region_codes=region_codes,
            diagnostics=diagnostics,
            logger_context={
                "mode": "virtual_route",
                "origin_station_id": origin_station_id,
                "destination_station_id": destination_station_id,
            },
            progress_callback=None,
        )
        diagnostics["timings_ms"]["build_network_data"] = round(_now_ms() - t0, 2)

        network_stats = network.get("stats") or {}
        network_mode = network_stats.get("network_mode")

        if network_mode != "scope_topology_graph" or not network.get("adjacency"):
            return {
                "status": "topology_missing",
                "message": (
                    "Для выбранной пары регионов topology graph не найден. "
                    "Нужно построить rail_graph_nodes / rail_graph_edges / station_graph_links "
                    "для соответствующего scope_key."
                ),
                "origin_station": origin_station,
                "destination_station": destination_station,
                "region_codes": region_codes,
                "scope_key": network.get("scope_key"),
                "geometry": None,
                "network_segments": [],
                "diagnostics": diagnostics,
            }

        fallback_node_cache: dict[int, list[dict[str, Any]]] = {}
        path_cache: dict[tuple[str, str], dict[str, Any] | None] = {}

        t0 = _now_ms()

        base_start_links = get_station_link_options_for_candidate(
            origin_candidate,
            network,
            fallback_node_cache,
        )
        base_end_links = get_station_link_options_for_candidate(
            destination_candidate,
            network,
            fallback_node_cache,
        )

        scope_start_links = load_scope_station_link_options(
            station=origin_station,
            network=network,
            limit=MAX_LINK_OPTIONS_PER_SIDE,
        )
        scope_end_links = load_scope_station_link_options(
            station=destination_station,
            network=network,
            limit=MAX_LINK_OPTIONS_PER_SIDE,
        )

        nearest_start_links = load_nearest_scope_node_options(
            station=origin_station,
            network=network,
            radius_m=NEAREST_GRAPH_NODE_SEARCH_RADIUS_M,
            limit=NEAREST_GRAPH_NODE_LIMIT,
        )
        nearest_end_links = load_nearest_scope_node_options(
            station=destination_station,
            network=network,
            radius_m=NEAREST_GRAPH_NODE_SEARCH_RADIUS_M,
            limit=NEAREST_GRAPH_NODE_LIMIT,
        )

        edge_snap_start_links = load_nearest_scope_edge_snap_options(
            station=origin_station,
            network=network,
            radius_m=NEAREST_GRAPH_NODE_SEARCH_RADIUS_M,
            limit=NEAREST_GRAPH_EDGE_LIMIT,
        )
        edge_snap_end_links = load_nearest_scope_edge_snap_options(
            station=destination_station,
            network=network,
            radius_m=NEAREST_GRAPH_NODE_SEARCH_RADIUS_M,
            limit=NEAREST_GRAPH_EDGE_LIMIT,
        )

        raw_start_links = merge_link_options(
            base_links=base_start_links,
            extra_links=scope_start_links + nearest_start_links + edge_snap_start_links,
            limit=MAX_LINK_OPTIONS_PER_SIDE,
        )
        raw_end_links = merge_link_options(
            base_links=base_end_links,
            extra_links=scope_end_links + nearest_end_links + edge_snap_end_links,
            limit=MAX_LINK_OPTIONS_PER_SIDE,
        )

        start_links, rejected_start_links = filter_link_options_by_distance(raw_start_links)
        end_links, rejected_end_links = filter_link_options_by_distance(raw_end_links)

        diagnostics["timings_ms"]["load_station_links"] = round(_now_ms() - t0, 2)

        diagnostics["link_options"] = {
            "origin_base_count": len(base_start_links or []),
            "destination_base_count": len(base_end_links or []),
            "origin_scope_count": len(scope_start_links),
            "destination_scope_count": len(scope_end_links),
            "origin_nearest_count": len(nearest_start_links),
            "destination_nearest_count": len(nearest_end_links),
            "origin_edge_snap_count": len(edge_snap_start_links),
            "destination_edge_snap_count": len(edge_snap_end_links),
            "origin_raw_count": len(raw_start_links),
            "destination_raw_count": len(raw_end_links),
            "origin_accepted_count": len(start_links),
            "destination_accepted_count": len(end_links),
            "origin_rejected_too_long_count": len(rejected_start_links),
            "destination_rejected_too_long_count": len(rejected_end_links),
            "origin": [compact_link_for_diagnostics(item) for item in start_links],
            "destination": [compact_link_for_diagnostics(item) for item in end_links],
            "origin_rejected_too_long": [
                compact_link_for_diagnostics(item) for item in rejected_start_links[:12]
            ],
            "destination_rejected_too_long": [
                compact_link_for_diagnostics(item) for item in rejected_end_links[:12]
            ],
        }

        if not start_links or not end_links:
            return {
                "status": "no_station_links",
                "message": (
                    "Не удалось связать одну из станций с topology graph на допустимом расстоянии. "
                    f"Максимальная длина station-connector: {MAX_VIRTUAL_STATION_CONNECTOR_KM} км."
                ),
                "origin_station": origin_station,
                "destination_station": destination_station,
                "region_codes": region_codes,
                "scope_key": network.get("scope_key"),
                "geometry": None,
                "network_segments": [],
                "diagnostics": diagnostics,
            }

        adjacency = network["adjacency"]
        node_coords = network["node_coords"]

        best_result: dict[str, Any] | None = None
        best_score = float("inf")

        t0 = _now_ms()
        checked_pairs = 0
        successful_pairs = 0
        failed_pairs = 0
        max_pair_diagnostics = 120

        for start_link in start_links:
            for end_link in end_links:
                checked_pairs += 1

                start_node_hash = str(start_link["node_hash"])
                end_node_hash = str(end_link["node_hash"])

                connector_start_km = float(start_link.get("link_distance_km") or 0.0)
                connector_end_km = float(end_link.get("link_distance_km") or 0.0)

                connector_start_air_km = _link_air_distance_km(start_link)
                connector_end_air_km = _link_air_distance_km(end_link)

                if (
                    connector_start_air_km > MAX_VIRTUAL_STATION_CONNECTOR_KM
                    or connector_end_air_km > MAX_VIRTUAL_STATION_CONNECTOR_KM
                ):
                    failed_pairs += 1

                    if len(diagnostics["checked_link_pairs"]) < max_pair_diagnostics:
                        diagnostics["checked_link_pairs"].append(
                            {
                                "start_node_hash": start_node_hash,
                                "end_node_hash": end_node_hash,
                                "status": "connector_too_long",
                                "connector_start_air_km": round(connector_start_air_km, 3),
                                "connector_end_air_km": round(connector_end_air_km, 3),
                                "connector_start_km": round(connector_start_km, 3),
                                "connector_end_km": round(connector_end_km, 3),
                                "start_link_source": start_link.get("source"),
                                "end_link_source": end_link.get("source"),
                                "max_connector_km": MAX_VIRTUAL_STATION_CONNECTOR_KM,
                            }
                        )

                    continue

                graph_path = dijkstra_topology_path(
                    adjacency=adjacency,
                    node_coords=node_coords,
                    start_node_hash=start_node_hash,
                    end_node_hash=end_node_hash,
                    path_cache=path_cache,
                )

                if graph_path is None:
                    failed_pairs += 1

                    if len(diagnostics["checked_link_pairs"]) < max_pair_diagnostics:
                        diagnostics["checked_link_pairs"].append(
                            {
                                "start_node_hash": start_node_hash,
                                "end_node_hash": end_node_hash,
                                "status": "no_path",
                                "connector_start_air_km": round(connector_start_air_km, 3),
                                "connector_end_air_km": round(connector_end_air_km, 3),
                                "connector_start_km": round(connector_start_km, 3),
                                "connector_end_km": round(connector_end_km, 3),
                                "start_link_source": start_link.get("source"),
                                "end_link_source": end_link.get("source"),
                            }
                        )

                    continue

                graph_edge_coords = build_graph_coordinates_from_edge_chain(
                    graph_path=graph_path,
                    node_coords=node_coords,
                )

                graph_distance_km = float(graph_path["distance_km"])

                total_distance_km = (
                    graph_distance_km
                    + connector_start_km
                    + connector_end_km
                )

                connector_overflow_km = max(
                    0.0,
                    connector_start_air_km - SOFT_VIRTUAL_STATION_CONNECTOR_KM,
                )
                connector_overflow_km += max(
                    0.0,
                    connector_end_air_km - SOFT_VIRTUAL_STATION_CONNECTOR_KM,
                )

                connector_penalty = (
                    (connector_start_air_km + connector_end_air_km) * 6.0
                    + connector_overflow_km * 30.0
                )
                final_score = total_distance_km + connector_penalty

                coordinates = build_virtual_pair_coordinates(
                    origin_candidate=origin_candidate,
                    destination_candidate=destination_candidate,
                    start_link=start_link,
                    end_link=end_link,
                    graph_coords=graph_edge_coords,
                )

                if len(coordinates) < 2:
                    failed_pairs += 1

                    if len(diagnostics["checked_link_pairs"]) < max_pair_diagnostics:
                        diagnostics["checked_link_pairs"].append(
                            {
                                "start_node_hash": start_node_hash,
                                "end_node_hash": end_node_hash,
                                "status": "bad_coordinates",
                                "connector_start_air_km": round(connector_start_air_km, 3),
                                "connector_end_air_km": round(connector_end_air_km, 3),
                                "connector_start_km": round(connector_start_km, 3),
                                "connector_end_km": round(connector_end_km, 3),
                                "start_link_source": start_link.get("source"),
                                "end_link_source": end_link.get("source"),
                            }
                        )

                    continue

                successful_pairs += 1

                pair_diagnostics = {
                    "start_node_hash": start_node_hash,
                    "end_node_hash": end_node_hash,
                    "status": "ok",
                    "graph_distance_km": round(graph_distance_km, 3),
                    "connector_start_air_km": round(connector_start_air_km, 6),
                    "connector_end_air_km": round(connector_end_air_km, 6),
                    "connector_start_km": round(connector_start_km, 6),
                    "connector_end_km": round(connector_end_km, 6),
                    "start_link_source": start_link.get("source"),
                    "end_link_source": end_link.get("source"),
                    "total_distance_km": round(total_distance_km, 3),
                    "connector_penalty": round(connector_penalty, 3),
                    "final_score": round(final_score, 3),
                    "edge_count": len(graph_path.get("edge_chain") or []),
                    "graph_edge_coords_count": len(graph_edge_coords),
                }

                if len(diagnostics["checked_link_pairs"]) < max_pair_diagnostics:
                    diagnostics["checked_link_pairs"].append(pair_diagnostics)

                if final_score < best_score:
                    best_score = final_score
                    best_result = {
                        "start_link": start_link,
                        "end_link": end_link,
                        "path": graph_path,
                        "coordinates": coordinates,
                        "graph_edge_coords_count": len(graph_edge_coords),
                        "graph_distance_km": graph_distance_km,
                        "connector_start_km": connector_start_km,
                        "connector_end_km": connector_end_km,
                        "connector_start_air_km": connector_start_air_km,
                        "connector_end_air_km": connector_end_air_km,
                        "total_distance_km": total_distance_km,
                        "graph_edge_count": len(graph_path.get("edge_chain") or []),
                        "hop_count": graph_path.get("hop_count"),
                        "connector_penalty": connector_penalty,
                        "final_score": final_score,
                    }

        diagnostics["timings_ms"]["dijkstra_link_pairs"] = round(_now_ms() - t0, 2)
        diagnostics["checked_link_pairs_count"] = checked_pairs
        diagnostics["successful_link_pairs_count"] = successful_pairs
        diagnostics["failed_link_pairs_count"] = failed_pairs

        if best_result is None:
            return {
                "status": "no_path",
                "message": "Путь по topology graph между выбранными станциями не найден",
                "origin_station": origin_station,
                "destination_station": destination_station,
                "region_codes": region_codes,
                "scope_key": network.get("scope_key"),
                "geometry": None,
                "network_segments": [],
                "diagnostics": diagnostics,
            }

        geometry = {
            "type": "LineString",
            "coordinates": best_result["coordinates"],
        }

        network_segments: list[dict[str, Any]] = []
        edge_index = 0
        used_virtual_connectors_count = 0

        for edge in (best_result.get("path") or {}).get("edge_chain") or []:
            edge_geometry = build_simple_linestring(edge.get("geometry_coords") or [])

            if edge_geometry is None:
                continue

            edge_index += 1

            edge_id = edge.get("edge_id") or edge.get("id")
            edge_source = edge.get("edge_source")
            is_virtual_connector = bool(edge.get("is_virtual_connector"))

            if is_virtual_connector or str(edge_source or "").startswith("virtual_connector"):
                used_virtual_connectors_count += 1

            network_segments.append(
                {
                    "segment_index": 1,
                    "edge_index": edge_index,
                    "edge_id": edge_id,
                    "from_node_hash": edge.get("from_node_hash"),
                    "to_node_hash": edge.get("to_node_hash"),
                    "length_km": edge.get("length_km"),
                    "segment_source": "virtual_osm_path",
                    "edge_source": edge_source,
                    "is_virtual_connector": is_virtual_connector,
                    "geometry": edge_geometry,
                }
            )

        route_payload = {
            "id": f"virtual-{origin_station_id}-{destination_station_id}",
            "origin_station_id": origin_station_id,
            "destination_station_id": destination_station_id,
            "source_system": "virtual_osm",
            "train_number": None,
            "route_name": "Теоретический путь по OSM",
            "origin_station_name": origin_station.get("name"),
            "destination_station_name": destination_station.get("name"),
            "geometry_source": "virtual_osm_path",
            "stops_count": 2,
            "matched_stops_count": 2,
            "unresolved_stops_count": 0,
            "notes": "Построено по topology graph OSM, не является расписанием РЖД",
        }

        stops = [
            {
                "stop_sequence": 1,
                "station_id": origin_station["id"],
                "station_name_raw": origin_station.get("name"),
                "station_name_matched": origin_station.get("name"),
                "matched_station_name": origin_station.get("name"),
                "lon": origin_station.get("lon"),
                "lat": origin_station.get("lat"),
                "is_origin": True,
                "is_destination": False,
                "match_method": "selected_station",
                "match_confidence": 1.0,
            },
            {
                "stop_sequence": 2,
                "station_id": destination_station["id"],
                "station_name_raw": destination_station.get("name"),
                "station_name_matched": destination_station.get("name"),
                "matched_station_name": destination_station.get("name"),
                "lon": destination_station.get("lon"),
                "lat": destination_station.get("lat"),
                "is_origin": False,
                "is_destination": True,
                "match_method": "selected_station",
                "match_confidence": 1.0,
            },
        ]

        diagnostics["selected_path"] = {
            "graph_distance_km": round(best_result["graph_distance_km"], 3),
            "connector_start_km": round(best_result["connector_start_km"], 3),
            "connector_end_km": round(best_result["connector_end_km"], 3),
            "connector_start_air_km": round(best_result["connector_start_air_km"], 3),
            "connector_end_air_km": round(best_result["connector_end_air_km"], 3),
            "total_distance_km": round(best_result["total_distance_km"], 3),
            "connector_penalty": round(best_result["connector_penalty"], 3),
            "graph_edge_count": best_result["graph_edge_count"],
            "graph_edge_coords_count": best_result.get("graph_edge_coords_count"),
            "hop_count": best_result.get("hop_count"),
            "start_node_hash": best_result["start_link"].get("node_hash"),
            "end_node_hash": best_result["end_link"].get("node_hash"),
            "start_link": compact_link_for_diagnostics(best_result["start_link"]),
            "end_link": compact_link_for_diagnostics(best_result["end_link"]),
            "final_score": round(best_result["final_score"], 3),
            "used_virtual_connectors_count": used_virtual_connectors_count,
        }

        diagnostics["timings_ms"]["total"] = round(_now_ms() - started_ms, 2)

        geojson = build_virtual_geojson(
            geometry=geometry,
            route_payload=route_payload,
            network_segments=network_segments,
        )

        return {
            "status": "ok",
            "message": "Виртуальный путь по OSM построен",
            "route": route_payload,
            "item": {
                **route_payload,
                "stops": stops,
                "geometry": geometry,
                "geometry_source": "virtual_osm_path",
                "network_segments": network_segments,
                "diagnostics": diagnostics,
            },
            "stops": stops,
            "geometry": geometry,
            "geometry_source": "virtual_osm_path",
            "network_segments": network_segments,
            "geojson": geojson,
            "summary": {
                "status": "ok",
                "origin_station_id": origin_station_id,
                "destination_station_id": destination_station_id,
                "distance_km": round(best_result["total_distance_km"], 3),
                "graph_distance_km": round(best_result["graph_distance_km"], 3),
                "network_segments_count": len(network_segments),
                "geometry_ready": True,
                "geometry_source": "virtual_osm_path",
                "checked_link_pairs_count": checked_pairs,
                "successful_link_pairs_count": successful_pairs,
                "used_virtual_connectors_count": used_virtual_connectors_count,
                "connector_start_km": round(best_result["connector_start_km"], 3),
                "connector_end_km": round(best_result["connector_end_km"], 3),
                "connector_start_air_km": round(best_result["connector_start_air_km"], 3),
                "connector_end_air_km": round(best_result["connector_end_air_km"], 3),
            },
            "diagnostics": diagnostics,
        }

    except Exception as exc:
        diagnostics["errors"].append(
            {
                "stage": "build_virtual_route_path",
                "error": str(exc),
                "type": exc.__class__.__name__,
            }
        )
        diagnostics["timings_ms"]["total"] = round(_now_ms() - started_ms, 2)
        raise
