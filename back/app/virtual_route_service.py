import time
from typing import Any

from sqlalchemy import text

from app.db import engine
from app.route_graph_matcher import (
    Candidate,
    build_network_data,
    build_simple_linestring,
    dijkstra_topology_path,
    get_station_link_options_for_candidate,
    merge_coordinate_sequences,
)


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


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


def build_virtual_pair_coordinates(
    *,
    origin_candidate: Candidate,
    destination_candidate: Candidate,
    start_link: dict[str, Any],
    end_link: dict[str, Any],
    graph_coords: list[list[float]] | None,
) -> list[list[float]]:
    sequences: list[list[list[float]]] = []

    connector_start = [
        [origin_candidate.lon, origin_candidate.lat],
        [float(start_link["node_lon"]), float(start_link["node_lat"])],
    ]
    if connector_start[0] != connector_start[1]:
        sequences.append(connector_start)

    if graph_coords:
        sequences.append(graph_coords)

    connector_end = [
        [float(end_link["node_lon"]), float(end_link["node_lat"])],
        [destination_candidate.lon, destination_candidate.lat],
    ]
    if connector_end[0] != connector_end[1]:
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
        "timings_ms": {},
        "network": {},
        "link_options": {},
        "selected_path": None,
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
        start_links = get_station_link_options_for_candidate(
            origin_candidate,
            network,
            fallback_node_cache,
        )
        end_links = get_station_link_options_for_candidate(
            destination_candidate,
            network,
            fallback_node_cache,
        )
        diagnostics["timings_ms"]["load_station_links"] = round(_now_ms() - t0, 2)

        diagnostics["link_options"] = {
            "origin": start_links,
            "destination": end_links,
        }

        if not start_links or not end_links:
            return {
                "status": "no_station_links",
                "message": "Не удалось связать одну из станций с узлами topology graph",
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

        for start_link in start_links:
            for end_link in end_links:
                checked_pairs += 1

                graph_path = dijkstra_topology_path(
                    adjacency=adjacency,
                    node_coords=node_coords,
                    start_node_hash=str(start_link["node_hash"]),
                    end_node_hash=str(end_link["node_hash"]),
                    path_cache=path_cache,
                )

                if graph_path is None:
                    continue

                connector_start_km = float(start_link["link_distance_km"])
                connector_end_km = float(end_link["link_distance_km"])
                graph_distance_km = float(graph_path["distance_km"])

                total_distance_km = (
                    graph_distance_km
                    + connector_start_km
                    + connector_end_km
                )

                connector_penalty = (connector_start_km + connector_end_km) * 4.0
                final_score = total_distance_km + connector_penalty

                coordinates = build_virtual_pair_coordinates(
                    origin_candidate=origin_candidate,
                    destination_candidate=destination_candidate,
                    start_link=start_link,
                    end_link=end_link,
                    graph_coords=graph_path.get("coordinates") or [],
                )

                if len(coordinates) < 2:
                    continue

                if final_score < best_score:
                    best_score = final_score
                    best_result = {
                        "start_link": start_link,
                        "end_link": end_link,
                        "path": graph_path,
                        "coordinates": coordinates,
                        "graph_distance_km": graph_distance_km,
                        "connector_start_km": connector_start_km,
                        "connector_end_km": connector_end_km,
                        "total_distance_km": total_distance_km,
                        "graph_edge_count": len(graph_path.get("edge_chain") or []),
                        "hop_count": graph_path.get("hop_count"),
                        "final_score": final_score,
                    }

        diagnostics["timings_ms"]["dijkstra_link_pairs"] = round(_now_ms() - t0, 2)
        diagnostics["checked_link_pairs_count"] = checked_pairs

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

        for edge in (best_result.get("path") or {}).get("edge_chain") or []:
            edge_geometry = build_simple_linestring(edge.get("geometry_coords") or [])
            if edge_geometry is None:
                continue

            edge_index += 1
            network_segments.append(
                {
                    "segment_index": 1,
                    "edge_index": edge_index,
                    "from_node_hash": edge.get("from_node_hash"),
                    "to_node_hash": edge.get("to_node_hash"),
                    "length_km": edge.get("length_km"),
                    "segment_source": "virtual_osm_path",
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
            "total_distance_km": round(best_result["total_distance_km"], 3),
            "graph_edge_count": best_result["graph_edge_count"],
            "hop_count": best_result.get("hop_count"),
            "start_node_hash": best_result["start_link"].get("node_hash"),
            "end_node_hash": best_result["end_link"].get("node_hash"),
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
