from app.route_graph_matcher import build_network_data, dijkstra_topology_path


ORIGIN_STATION_ID = 67231      # Москва Казанская
DESTINATION_STATION_ID = 62794 # Дружинино


def check(scope_key: str) -> None:
    region_codes = scope_key.split("|")

    diagnostics = {"network": {}}

    network = build_network_data(
        region_codes=region_codes,
        diagnostics=diagnostics,
        logger_context={"script": "check_scope_connectivity"},
        progress_callback=None,
    )

    station_links = network.get("station_links") or {}
    start_links = station_links.get(ORIGIN_STATION_ID) or []
    end_links = station_links.get(DESTINATION_STATION_ID) or []

    print("\n=== SCOPE ===")
    print(scope_key)
    print("network_stats:", network.get("stats"))
    print("start_links:", [
        {
            "node_hash": x.get("node_hash"),
            "link_distance_km": x.get("link_distance_km"),
            "is_primary": x.get("is_primary"),
            "source": x.get("source"),
        }
        for x in start_links
    ])
    print("end_links:", [
        {
            "node_hash": x.get("node_hash"),
            "link_distance_km": x.get("link_distance_km"),
            "is_primary": x.get("is_primary"),
            "source": x.get("source"),
        }
        for x in end_links
    ])

    if not start_links or not end_links:
        print("RESULT: no station links")
        return

    path_cache = {}
    best = None

    for s in start_links:
        for e in end_links:
            path = dijkstra_topology_path(
                adjacency=network["adjacency"],
                node_coords=network["node_coords"],
                start_node_hash=str(s["node_hash"]),
                end_node_hash=str(e["node_hash"]),
                path_cache=path_cache,
            )

            if path is None:
                continue

            total_km = (
                float(path["distance_km"])
                + float(s["link_distance_km"])
                + float(e["link_distance_km"])
            )

            candidate = {
                "start_node_hash": s["node_hash"],
                "end_node_hash": e["node_hash"],
                "graph_distance_km": round(float(path["distance_km"]), 3),
                "connector_start_km": round(float(s["link_distance_km"]), 3),
                "connector_end_km": round(float(e["link_distance_km"]), 3),
                "total_km": round(total_km, 3),
                "hop_count": path.get("hop_count"),
                "edge_count": len(path.get("edge_chain") or []),
            }

            if best is None or candidate["total_km"] < best["total_km"]:
                best = candidate

    if best is None:
        print("RESULT: no path")
    else:
        print("RESULT: path found")
        print("best:", best)


if __name__ == "__main__":
    scopes = [
        "central_fd|ural_fd",
        "central_fd|far_eastern_fd|siberian_fd|ural_fd",
        "central_fd|far_eastern_fd|siberian_fd|ural_fd|volga_fd",
        "central_fd|south_fd|ural_fd|volga_fd",
        "central_fd|northwestern_fd|ural_fd|volga_fd",
    ]

    for scope in scopes:
        check(scope)