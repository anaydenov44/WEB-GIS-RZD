from pathlib import Path
import re


path = Path("scripts/build_route_scope_topology.py")
text = path.read_text(encoding="utf-8")


def replace_function(src: str, function_name: str, replacement: str) -> str:
    pattern = re.compile(
        rf"^def {re.escape(function_name)}\([\s\S]*?\n(?=def |\nif __name__|\Z)",
        re.MULTILINE,
    )
    new_src, count = pattern.subn(replacement.rstrip() + "\n\n", src, count=1)
    if count != 1:
        raise RuntimeError(f"Не удалось заменить функцию {function_name}, count={count}")
    return new_src


# ---------------------------------------------------------------------
# 1. load_scope_candidate_nodes_for_absurd_stitch: use same connection
# ---------------------------------------------------------------------

text = replace_function(
    text,
    "load_scope_candidate_nodes_for_absurd_stitch",
    r'''
def load_scope_candidate_nodes_for_absurd_stitch(
    *,
    connection,
    scope_key: str,
    bbox: dict[str, float],
    degree_max: int = ROUTE_LOCAL_ABSURD_STITCH_NODE_DEGREE_MAX,
    limit: int = ROUTE_LOCAL_ABSURD_STITCH_MAX_CANDIDATE_NODES,
) -> list[dict[str, Any]]:
    base_query = """
        WITH deg AS (
            SELECT
                node_hash,
                COUNT(*) AS degree
            FROM (
                SELECT source_node_hash AS node_hash
                FROM rail_graph_edges
                WHERE scope_key = :scope_key

                UNION ALL

                SELECT target_node_hash AS node_hash
                FROM rail_graph_edges
                WHERE scope_key = :scope_key
            ) q
            GROUP BY node_hash
        )
        SELECT
            n.node_hash,
            n.lon,
            n.lat,
            COALESCE(d.degree, 0) AS degree
        FROM rail_graph_nodes n
        LEFT JOIN deg d ON d.node_hash = n.node_hash
        WHERE n.scope_key = :scope_key
          AND n.lon BETWEEN :min_lon AND :max_lon
          AND n.lat BETWEEN :min_lat AND :max_lat
          {degree_clause}
        ORDER BY COALESCE(d.degree, 0), n.node_hash
        LIMIT :limit;
    """

    params = {
        "scope_key": scope_key,
        "min_lon": bbox["min_lon"],
        "min_lat": bbox["min_lat"],
        "max_lon": bbox["max_lon"],
        "max_lat": bbox["max_lat"],
        "degree_max": degree_max,
        "limit": limit,
    }

    count_query = text("""
        SELECT COUNT(*) AS node_count
        FROM rail_graph_nodes n
        WHERE n.scope_key = :scope_key
          AND n.lon BETWEEN :min_lon AND :max_lon
          AND n.lat BETWEEN :min_lat AND :max_lat;
    """)

    bbox_node_count = int(
        connection.execute(count_query, params).scalar_one() or 0
    )

    rows = connection.execute(
        text(base_query.format(degree_clause="AND COALESCE(d.degree, 0) <= :degree_max")),
        params,
    ).fetchall()

    fallback_used = False

    if not rows:
        fallback_used = True
        rows = connection.execute(
            text(base_query.format(degree_clause="")),
            params,
        ).fetchall()

    result = [dict(row._mapping) for row in rows]

    print(
        "[graph-builder] absurd-detour candidate node load:",
        json.dumps(
            {
                "scope_key": scope_key,
                "bbox": bbox,
                "bbox_node_count": bbox_node_count,
                "degree_max": degree_max,
                "candidate_nodes_count": len(result),
                "fallback_used": fallback_used,
            },
            ensure_ascii=False,
            default=str,
        ),
    )

    return result
''',
)


# ---------------------------------------------------------------------
# 2. load_scope_graph_for_absurd_stitch: use same connection
# ---------------------------------------------------------------------

text = replace_function(
    text,
    "load_scope_graph_for_absurd_stitch",
    r'''
def load_scope_graph_for_absurd_stitch(
    *,
    connection,
    scope_key: str,
) -> tuple[dict[str, list[tuple[str, float]]], set[tuple[str, str]]]:
    query = text("""
        SELECT
            source_node_hash,
            target_node_hash,
            length_km
        FROM rail_graph_edges
        WHERE scope_key = :scope_key;
    """)

    adjacency: dict[str, list[tuple[str, float]]] = defaultdict(list)
    existing_pairs: set[tuple[str, str]] = set()

    rows = connection.execute(query, {"scope_key": scope_key}).fetchall()

    for row in rows:
        item = dict(row._mapping)
        source = str(item["source_node_hash"])
        target = str(item["target_node_hash"])
        length_km = float(item["length_km"] or 0.0)

        if not source or not target or source == target or length_km <= 0:
            continue

        adjacency[source].append((target, length_km))
        adjacency[target].append((source, length_km))

        key = tuple(sorted((source, target)))
        existing_pairs.add(key)

    return dict(adjacency), existing_pairs
''',
)


# ---------------------------------------------------------------------
# 3. insert_absurd_detour_stitch_edge: use same connection
# ---------------------------------------------------------------------

text = replace_function(
    text,
    "insert_absurd_detour_stitch_edge",
    r'''
def insert_absurd_detour_stitch_edge(
    *,
    connection,
    scope_key: str,
    source_node_hash: str,
    target_node_hash: str,
    source_lon: float,
    source_lat: float,
    target_lon: float,
    target_lat: float,
    length_km: float,
) -> None:
    query = text("""
        INSERT INTO rail_graph_edges (
            scope_key,
            source_node_hash,
            target_node_hash,
            length_km,
            geom
        )
        VALUES (
            :scope_key,
            :source_node_hash,
            :target_node_hash,
            :length_km,
            ST_SetSRID(
                ST_MakeLine(
                    ST_MakePoint(:source_lon, :source_lat),
                    ST_MakePoint(:target_lon, :target_lat)
                ),
                4326
            )
        );
    """)

    connection.execute(
        query,
        {
            "scope_key": scope_key,
            "source_node_hash": source_node_hash,
            "target_node_hash": target_node_hash,
            "length_km": length_km,
            "source_lon": source_lon,
            "source_lat": source_lat,
            "target_lon": target_lon,
            "target_lat": target_lat,
        },
    )
''',
)


# ---------------------------------------------------------------------
# 4. create_route_local_absurd_detour_stitches: accept/pass connection
# ---------------------------------------------------------------------

text = replace_function(
    text,
    "create_route_local_absurd_detour_stitches",
    r'''
def create_route_local_absurd_detour_stitches(
    *,
    connection,
    route_id: int,
    scope_key: str,
) -> dict[str, Any]:
    bbox = load_route_station_bbox_for_stitch(route_id=route_id)
    if bbox is None:
        result = {
            "route_id": route_id,
            "scope_key": scope_key,
            "candidate_nodes_count": 0,
            "candidate_pairs_count": 0,
            "selected_pairs_count": 0,
            "inserted_count": 0,
            "selected_pairs_preview": [],
            "reason": "no_route_station_bbox",
        }
        print("[graph-builder] absurd-detour stitches created:", json.dumps(result, ensure_ascii=False))
        return result

    total_scope_nodes = connection.execute(
        text("""
            SELECT COUNT(*)
            FROM rail_graph_nodes
            WHERE scope_key = :scope_key
        """),
        {"scope_key": scope_key},
    ).scalar_one()

    bbox_scope_nodes = connection.execute(
        text("""
            SELECT COUNT(*)
            FROM rail_graph_nodes
            WHERE scope_key = :scope_key
              AND lon BETWEEN :min_lon AND :max_lon
              AND lat BETWEEN :min_lat AND :max_lat
        """),
        {
            "scope_key": scope_key,
            "min_lon": bbox["min_lon"],
            "min_lat": bbox["min_lat"],
            "max_lon": bbox["max_lon"],
            "max_lat": bbox["max_lat"],
        },
    ).scalar_one()

    print(
        "[graph-builder] absurd-detour debug:",
        json.dumps(
            {
                "route_id": route_id,
                "scope_key": scope_key,
                "bbox": bbox,
                "total_scope_nodes": int(total_scope_nodes),
                "bbox_scope_nodes": int(bbox_scope_nodes),
            },
            ensure_ascii=False,
            default=str,
        ),
    )

    candidate_nodes = load_scope_candidate_nodes_for_absurd_stitch(
        connection=connection,
        scope_key=scope_key,
        bbox=bbox,
    )

    expanded_bbox_used = False
    if not candidate_nodes:
        expanded_bbox = {
            **bbox,
            "min_lon": float(bbox["min_lon"]) - 0.25,
            "min_lat": float(bbox["min_lat"]) - 0.25,
            "max_lon": float(bbox["max_lon"]) + 0.25,
            "max_lat": float(bbox["max_lat"]) + 0.25,
            "source": str(bbox.get("source") or "unknown") + "_expanded_0_25deg",
        }
        expanded_bbox_used = True
        print("[graph-builder] absurd-detour bbox expanded:", json.dumps(expanded_bbox, ensure_ascii=False))

        expanded_bbox_scope_nodes = connection.execute(
            text("""
                SELECT COUNT(*)
                FROM rail_graph_nodes
                WHERE scope_key = :scope_key
                  AND lon BETWEEN :min_lon AND :max_lon
                  AND lat BETWEEN :min_lat AND :max_lat
            """),
            {
                "scope_key": scope_key,
                "min_lon": expanded_bbox["min_lon"],
                "min_lat": expanded_bbox["min_lat"],
                "max_lon": expanded_bbox["max_lon"],
                "max_lat": expanded_bbox["max_lat"],
            },
        ).scalar_one()

        print(
            "[graph-builder] absurd-detour expanded bbox debug:",
            json.dumps(
                {
                    "route_id": route_id,
                    "scope_key": scope_key,
                    "bbox": expanded_bbox,
                    "total_scope_nodes": int(total_scope_nodes),
                    "bbox_scope_nodes": int(expanded_bbox_scope_nodes),
                },
                ensure_ascii=False,
                default=str,
            ),
        )

        candidate_nodes = load_scope_candidate_nodes_for_absurd_stitch(
            connection=connection,
            scope_key=scope_key,
            bbox=expanded_bbox,
        )
        bbox = expanded_bbox

    adjacency, existing_pairs = load_scope_graph_for_absurd_stitch(
        connection=connection,
        scope_key=scope_key,
    )

    candidate_pairs: list[dict[str, Any]] = []

    for left_index in range(len(candidate_nodes)):
        left = candidate_nodes[left_index]
        left_hash = str(left["node_hash"])
        left_lon = float(left["lon"])
        left_lat = float(left["lat"])

        for right_index in range(left_index + 1, len(candidate_nodes)):
            right = candidate_nodes[right_index]
            right_hash = str(right["node_hash"])
            right_lon = float(right["lon"])
            right_lat = float(right["lat"])

            pair_key = tuple(sorted((left_hash, right_hash)))
            if pair_key in existing_pairs:
                continue

            gap_km = haversine_km(left_lon, left_lat, right_lon, right_lat)
            if gap_km < ROUTE_LOCAL_ABSURD_STITCH_MIN_GAP_KM:
                continue
            if gap_km > ROUTE_LOCAL_ABSURD_STITCH_MAX_GAP_KM:
                continue

            graph_distance_km = dijkstra_distance_limited(
                adjacency=adjacency,
                start_node_hash=left_hash,
                end_node_hash=right_hash,
            )

            threshold_km = max(
                ROUTE_LOCAL_ABSURD_STITCH_MIN_PATH_KM,
                gap_km * ROUTE_LOCAL_ABSURD_STITCH_PATH_FACTOR,
            )

            if graph_distance_km is None:
                ratio = None
                is_absurd = True
            else:
                ratio = graph_distance_km / max(gap_km, 0.001)
                is_absurd = graph_distance_km >= threshold_km

            if not is_absurd:
                continue

            candidate_pairs.append(
                {
                    "source_node_hash": left_hash,
                    "target_node_hash": right_hash,
                    "source_lon": left_lon,
                    "source_lat": left_lat,
                    "target_lon": right_lon,
                    "target_lat": right_lat,
                    "source_degree": int(left["degree"]),
                    "target_degree": int(right["degree"]),
                    "gap_km": gap_km,
                    "graph_distance_km": graph_distance_km,
                    "ratio": ratio,
                }
            )

    candidate_pairs.sort(
        key=lambda item: (
            -(999999.0 if item["ratio"] is None else float(item["ratio"])),
            float(item["gap_km"]),
            int(item["source_degree"]) + int(item["target_degree"]),
            item["source_node_hash"],
            item["target_node_hash"],
        )
    )

    selected_pairs = candidate_pairs[:ROUTE_LOCAL_ABSURD_STITCH_PAIR_LIMIT]
    inserted_count = 0

    for item in selected_pairs:
        insert_absurd_detour_stitch_edge(
            connection=connection,
            scope_key=scope_key,
            source_node_hash=item["source_node_hash"],
            target_node_hash=item["target_node_hash"],
            source_lon=float(item["source_lon"]),
            source_lat=float(item["source_lat"]),
            target_lon=float(item["target_lon"]),
            target_lat=float(item["target_lat"]),
            length_km=float(item["gap_km"]),
        )
        inserted_count += 1

    result = {
        "route_id": route_id,
        "scope_key": scope_key,
        "bbox": bbox,
        "expanded_bbox_used": expanded_bbox_used,
        "candidate_nodes_count": len(candidate_nodes),
        "candidate_pairs_count": len(candidate_pairs),
        "selected_pairs_count": len(selected_pairs),
        "inserted_count": inserted_count,
        "selected_pairs_preview": [
            {
                "source_node_hash": item["source_node_hash"],
                "target_node_hash": item["target_node_hash"],
                "gap_km": round(float(item["gap_km"]), 4),
                "graph_distance_km": round(float(item["graph_distance_km"]), 4)
                if item["graph_distance_km"] is not None else None,
                "ratio": round(float(item["ratio"]), 2)
                if item["ratio"] is not None else None,
                "source_degree": item["source_degree"],
                "target_degree": item["target_degree"],
            }
            for item in selected_pairs[:8]
        ],
    }

    print("[graph-builder] absurd-detour stitches created:", json.dumps(result, ensure_ascii=False))
    return result
''',
)


# ---------------------------------------------------------------------
# 5. Patch call site: add connection=connection
# ---------------------------------------------------------------------

text = re.sub(
    r"absurd_detour_stitches = create_route_local_absurd_detour_stitches\(\n\s*route_id=route_id,\n\s*scope_key=scope_key,\n\s*\)",
    "absurd_detour_stitches = create_route_local_absurd_detour_stitches(\n"
    "            connection=connection,\n"
    "            route_id=route_id,\n"
    "            scope_key=scope_key,\n"
    "        )",
    text,
    count=1,
)

# Defensive: if indentation/call shape differs, patch a call lacking connection.
if "absurd_detour_stitches = create_route_local_absurd_detour_stitches(\n            connection=connection," not in text:
    text = text.replace(
        "absurd_detour_stitches = create_route_local_absurd_detour_stitches(\n"
        "            route_id=route_id,\n"
        "            scope_key=scope_key,\n"
        "        )",
        "absurd_detour_stitches = create_route_local_absurd_detour_stitches(\n"
        "            connection=connection,\n"
        "            route_id=route_id,\n"
        "            scope_key=scope_key,\n"
        "        )",
        1,
    )

path.write_text(text, encoding="utf-8")
print("OK: absurd-detour stitch now uses same transaction connection")