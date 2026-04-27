from pathlib import Path
import re


path = Path("scripts/build_route_scope_topology.py")
text = path.read_text(encoding="utf-8")


def insert_after_anchor(src: str, anchor: str, insertion: str) -> str:
    if insertion.strip() in src:
        return src

    if anchor not in src:
        raise RuntimeError(f"Не найден anchor:\n{anchor}")

    return src.replace(anchor, anchor + insertion, 1)


def insert_before_function(src: str, function_name: str, block: str) -> str:
    if block.strip() in src:
        return src

    marker = f"\ndef {function_name}("
    index = src.find(marker)
    if index == -1:
        raise RuntimeError(f"Не найдена функция {function_name} для вставки блока")

    return src[:index] + "\n\n" + block.rstrip() + "\n" + src[index:]


def ensure_heapq_import(src: str) -> str:
    if "from heapq import heappop, heappush" in src:
        return src

    if "import json\n" in src:
        return src.replace(
            "import json\n",
            "import json\nfrom heapq import heappop, heappush\n",
            1,
        )

    return "from heapq import heappop, heappush\n" + src


def ensure_haversine_import(src: str) -> str:
    import_block_match = re.search(
        r"from app\.route_graph_matcher import \(\n(?P<body>[\s\S]*?)\n\)",
        src,
        flags=re.MULTILINE,
    )
    if not import_block_match:
        raise RuntimeError("Не найден import-блок from app.route_graph_matcher import (...)")

    block = import_block_match.group(0)
    if "haversine_km" in block:
        return src

    body = import_block_match.group("body")
    new_body = body.rstrip() + "\n    haversine_km,"
    return src[:import_block_match.start("body")] + new_body + src[import_block_match.end("body"):]


# ---------------------------------------------------------------------
# 1. Imports
# ---------------------------------------------------------------------

text = ensure_heapq_import(text)
text = ensure_haversine_import(text)


# ---------------------------------------------------------------------
# 2. Constants
# ---------------------------------------------------------------------

constants = """
ROUTE_LOCAL_ABSURD_STITCH_BBOX_PAD_DEG = 0.08
ROUTE_LOCAL_ABSURD_STITCH_NODE_DEGREE_MAX = 3
ROUTE_LOCAL_ABSURD_STITCH_MIN_GAP_KM = 0.03
ROUTE_LOCAL_ABSURD_STITCH_MAX_GAP_KM = 1.50
ROUTE_LOCAL_ABSURD_STITCH_MIN_PATH_KM = 8.0
ROUTE_LOCAL_ABSURD_STITCH_PATH_FACTOR = 12.0
ROUTE_LOCAL_ABSURD_STITCH_PAIR_LIMIT = 12
ROUTE_LOCAL_ABSURD_STITCH_MAX_CANDIDATE_NODES = 400
ROUTE_LOCAL_ABSURD_STITCH_MAX_DIJKSTRA_KM = 350.0
"""

if "ROUTE_LOCAL_ABSURD_STITCH_BBOX_PAD_DEG" not in text:
    anchors = [
        "SAME_COMPONENT_MAX_STITCHES_PER_BUILD = 50\n",
        "SYNTHETIC_GAP_MAX_PAIRS = 100\n",
        "STATION_LINK_MAX_DISTANCE_M = 450.0\n",
    ]

    for anchor in anchors:
        if anchor in text:
            text = text.replace(anchor, anchor + constants, 1)
            break
    else:
        raise RuntimeError("Не найдено место для вставки ROUTE_LOCAL_ABSURD_STITCH_* констант")


# ---------------------------------------------------------------------
# 3. Absurd-detour helper functions
# ---------------------------------------------------------------------

helpers = r'''
def load_route_station_bbox_for_stitch(
    *,
    route_id: int,
    pad_deg: float = ROUTE_LOCAL_ABSURD_STITCH_BBOX_PAD_DEG,
) -> dict[str, float] | None:
    query = text("""
        SELECT
            MIN(ST_X(s.geom)) AS min_lon,
            MIN(ST_Y(s.geom)) AS min_lat,
            MAX(ST_X(s.geom)) AS max_lon,
            MAX(ST_Y(s.geom)) AS max_lat
        FROM route_stops rs
        JOIN stations s ON s.id = rs.station_id
        WHERE rs.route_id = :route_id
          AND s.geom IS NOT NULL;
    """)

    with engine.connect() as connection:
        row = connection.execute(query, {"route_id": route_id}).first()

    if row is None:
        return None

    item = dict(row._mapping)
    if (
        item.get("min_lon") is None
        or item.get("min_lat") is None
        or item.get("max_lon") is None
        or item.get("max_lat") is None
    ):
        return None

    return {
        "min_lon": float(item["min_lon"]) - pad_deg,
        "min_lat": float(item["min_lat"]) - pad_deg,
        "max_lon": float(item["max_lon"]) + pad_deg,
        "max_lat": float(item["max_lat"]) + pad_deg,
    }


def load_scope_candidate_nodes_for_absurd_stitch(
    *,
    scope_key: str,
    bbox: dict[str, float],
    degree_max: int = ROUTE_LOCAL_ABSURD_STITCH_NODE_DEGREE_MAX,
    limit: int = ROUTE_LOCAL_ABSURD_STITCH_MAX_CANDIDATE_NODES,
) -> list[dict[str, Any]]:
    query = text("""
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
          AND COALESCE(d.degree, 0) <= :degree_max
        ORDER BY COALESCE(d.degree, 0), n.node_hash
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "scope_key": scope_key,
                "min_lon": bbox["min_lon"],
                "min_lat": bbox["min_lat"],
                "max_lon": bbox["max_lon"],
                "max_lat": bbox["max_lat"],
                "degree_max": degree_max,
                "limit": limit,
            },
        ).fetchall()

    return [dict(row._mapping) for row in rows]


def load_scope_graph_for_absurd_stitch(
    *,
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

    with engine.connect() as connection:
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


def dijkstra_distance_limited(
    *,
    adjacency: dict[str, list[tuple[str, float]]],
    start_node_hash: str,
    end_node_hash: str,
    max_distance_km: float = ROUTE_LOCAL_ABSURD_STITCH_MAX_DIJKSTRA_KM,
) -> float | None:
    if start_node_hash == end_node_hash:
        return 0.0

    queue: list[tuple[float, str]] = [(0.0, start_node_hash)]
    distances: dict[str, float] = {start_node_hash: 0.0}

    while queue:
        current_distance, current_node_hash = heappop(queue)

        if current_distance > max_distance_km:
            return None

        if current_node_hash == end_node_hash:
            return current_distance

        if current_distance > distances.get(current_node_hash, math.inf):
            continue

        for next_node_hash, edge_length_km in adjacency.get(current_node_hash, []):
            next_distance = current_distance + float(edge_length_km)
            if next_distance < distances.get(next_node_hash, math.inf):
                distances[next_node_hash] = next_distance
                heappush(queue, (next_distance, next_node_hash))

    return None


def insert_absurd_detour_stitch_edge(
    *,
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

    with engine.begin() as connection:
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


def create_route_local_absurd_detour_stitches(
    *,
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

    candidate_nodes = load_scope_candidate_nodes_for_absurd_stitch(
        scope_key=scope_key,
        bbox=bbox,
    )
    adjacency, existing_pairs = load_scope_graph_for_absurd_stitch(scope_key=scope_key)

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
'''

if "def create_route_local_absurd_detour_stitches(" not in text:
    if "def print_section(" in text:
        text = insert_before_function(text, "print_section", helpers)
    elif "def ensure_tables(" in text:
        text = insert_before_function(text, "ensure_tables", helpers)
    else:
        raise RuntimeError("Не найдено место для вставки absurd-detour helper-функций")


# ---------------------------------------------------------------------
# 4. Insert call into build_topology pipeline
# ---------------------------------------------------------------------

if "absurd_detour_stitches = create_route_local_absurd_detour_stitches(" not in text:
    # Prefer inserting after same_component_stitch_diag if previous patch exists.
    same_component_call = "        same_component_stitch_diag = apply_same_component_gap_stitches_for_scope(scope_key)\n"
    if same_component_call in text:
        text = text.replace(
            same_component_call,
            same_component_call
            + "\n"
            + "        absurd_detour_stitches = create_route_local_absurd_detour_stitches(\n"
            + "            route_id=route_id,\n"
            + "            scope_key=scope_key,\n"
            + "        )\n",
            1,
        )
    else:
        # Fallback: insert before persisted_edges_count query.
        marker = """        persisted_edges_count = connection.execute(
            text("SELECT COUNT(*) FROM rail_graph_edges WHERE scope_key = :scope_key;"),
            {"scope_key": scope_key},
        ).scalar_one()
"""
        if marker not in text:
            raise RuntimeError("Не найдено место для вставки absurd_detour_stitches после записи rail_graph_edges")

        text = text.replace(
            marker,
            "        absurd_detour_stitches = create_route_local_absurd_detour_stitches(\n"
            "            route_id=route_id,\n"
            "            scope_key=scope_key,\n"
            "        )\n\n"
            + marker,
            1,
        )


# ---------------------------------------------------------------------
# 5. Make build_topology accept route_id
# ---------------------------------------------------------------------

text = text.replace(
    "def build_topology(scope_key: str, region_codes: list[str]) -> dict:",
    "def build_topology(scope_key: str, region_codes: list[str], route_id: int) -> dict:",
    1,
)

text = text.replace(
    "stats = build_topology(scope_key, region_codes)",
    "stats = build_topology(scope_key, region_codes, route_id)",
    1,
)


# ---------------------------------------------------------------------
# 6. Add result field
# ---------------------------------------------------------------------

if '"absurd_detour_stitches": absurd_detour_stitches,' not in text:
    if '"same_component_gap_stitches": same_component_stitch_diag,' in text:
        text = text.replace(
            '        "same_component_gap_stitches": same_component_stitch_diag,\n',
            '        "same_component_gap_stitches": same_component_stitch_diag,\n'
            '        "absurd_detour_stitches": absurd_detour_stitches,\n',
            1,
        )
    else:
        text = text.replace(
            '        "persisted_edges_count": int(persisted_edges_count),\n',
            '        "persisted_edges_count": int(persisted_edges_count),\n'
            '        "absurd_detour_stitches": absurd_detour_stitches,\n',
            1,
        )


path.write_text(text, encoding="utf-8")
print("OK: scripts/build_route_scope_topology.py patched with route-local absurd-detour stitches")