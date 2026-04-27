from pathlib import Path
import re
import sys


DEFAULT_TARGET = "scripts/build_route_scope_topology.py"

target_path = Path(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET)

if not target_path.exists():
    raise SystemExit(
        f"Файл не найден: {target_path}\n"
        f"Передай путь явно: python patch_scope_topology_synthetic_gaps.py path/to/builder.py"
    )

text = target_path.read_text(encoding="utf-8")


def ensure_import(src: str, import_line: str) -> str:
    if import_line in src:
        return src

    lines = src.splitlines()
    insert_at = 0

    # after shebang / encoding comments
    while insert_at < len(lines) and (
        lines[insert_at].startswith("#!")
        or "coding" in lines[insert_at]
        or lines[insert_at].strip() == ""
    ):
        insert_at += 1

    # after existing import block
    last_import = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            last_import = i
        elif last_import != -1 and stripped and not stripped.startswith(("import ", "from ")):
            break

    if last_import >= 0:
        lines.insert(last_import + 1, import_line)
    else:
        lines.insert(insert_at, import_line)

    return "\n".join(lines) + "\n"


def ensure_from_import_name(src: str, module: str, name: str) -> str:
    # already imported directly
    if re.search(rf"^from {re.escape(module)} import .*?\b{re.escape(name)}\b", src, re.MULTILINE):
        return src

    # append into existing single-line import
    pattern = re.compile(rf"^(from {re.escape(module)} import )(.+)$", re.MULTILINE)
    match = pattern.search(src)
    if match and "(" not in match.group(2):
        existing = match.group(2).strip()
        replacement = f"{match.group(1)}{existing}, {name}"
        return src[:match.start()] + replacement + src[match.end():]

    return ensure_import(src, f"from {module} import {name}")


def insert_before_first_function(src: str, block: str) -> str:
    if "def bridge_synthetic_gaps_for_scope(" in src:
        return src

    match = re.search(r"^def\s+\w+\(", src, re.MULTILINE)
    if not match:
        raise RuntimeError("Не нашёл первую функцию для вставки helper-блока")

    return src[:match.start()] + block.rstrip() + "\n\n" + src[match.start():]


def insert_call_after_edge_build(src: str, call_block: str) -> tuple[str, bool]:
    if "bridge_synthetic_gaps_for_scope(scope_key)" in src:
        return src, True

    lines = src.splitlines()
    candidates: list[int] = []

    edge_call_re = re.compile(
        r"\b("
        r"build_.*edges.*|"
        r".*build.*edges.*|"
        r"insert_.*edges.*|"
        r".*insert.*edges.*|"
        r"write_.*edges.*|"
        r".*write.*edges.*|"
        r"persist_.*edges.*|"
        r".*persist.*edges.*|"
        r"save_.*edges.*|"
        r".*save.*edges.*"
        r")\s*\("
    )

    for i, line in enumerate(lines):
        stripped = line.strip()

        if not stripped:
            continue
        if stripped.startswith("def "):
            continue
        if "rail_graph_edges" in stripped and any(word in stripped.lower() for word in ("insert", "execute")):
            candidates.append(i)
            continue
        if "scope_key" in stripped and edge_call_re.search(stripped.lower()):
            candidates.append(i)

    if not candidates:
        return src, False

    insert_after = candidates[-1]

    indent = re.match(r"^(\s*)", lines[insert_after]).group(1)
    patched_block = "\n".join(indent + line if line else "" for line in call_block.strip("\n").splitlines())

    lines.insert(insert_after + 1, patched_block)

    return "\n".join(lines) + "\n", True


# Imports used by the synthetic-gap helpers.
text = ensure_import(text, "import math")
text = ensure_from_import_name(text, "collections", "defaultdict")
text = ensure_from_import_name(text, "typing", "Any")


helper_block = r'''
SYNTHETIC_GAP_MAX_DISTANCE_M = 120.0
SYNTHETIC_GAP_MAX_ANGLE_DIFF_DEG = 35.0
SYNTHETIC_GAP_MAX_PAIRS = 100


def load_node_degrees(scope_key: str) -> dict[str, int]:
    query = text("""
        WITH degree_src AS (
            SELECT source_node_hash AS node_hash
            FROM rail_graph_edges
            WHERE scope_key = :scope_key

            UNION ALL

            SELECT target_node_hash AS node_hash
            FROM rail_graph_edges
            WHERE scope_key = :scope_key
        )
        SELECT
            node_hash,
            COUNT(*) AS degree
        FROM degree_src
        GROUP BY node_hash;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query, {"scope_key": scope_key}).fetchall()

    return {
        str(row._mapping["node_hash"]): int(row._mapping["degree"])
        for row in rows
    }


def load_endpoint_nodes(scope_key: str) -> list[dict[str, Any]]:
    degrees = load_node_degrees(scope_key)

    query = text("""
        SELECT
            node_hash,
            lon,
            lat
        FROM rail_graph_nodes
        WHERE scope_key = :scope_key;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query, {"scope_key": scope_key}).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row._mapping)
        node_hash = str(item["node_hash"])
        degree = int(degrees.get(node_hash, 0))
        if degree != 1:
            continue

        result.append(
            {
                "node_hash": node_hash,
                "lon": float(item["lon"]),
                "lat": float(item["lat"]),
                "degree": degree,
            }
        )

    return result


def load_single_neighbor(scope_key: str, node_hash: str) -> dict[str, Any] | None:
    query = text("""
        SELECT
            other.node_hash,
            other.lon,
            other.lat
        FROM (
            SELECT
                e.target_node_hash AS other_node_hash
            FROM rail_graph_edges e
            WHERE e.scope_key = :scope_key
              AND e.source_node_hash = :node_hash

            UNION ALL

            SELECT
                e.source_node_hash AS other_node_hash
            FROM rail_graph_edges e
            WHERE e.scope_key = :scope_key
              AND e.target_node_hash = :node_hash
        ) x
        JOIN rail_graph_nodes other
          ON other.scope_key = :scope_key
         AND other.node_hash = x.other_node_hash
        LIMIT 1;
    """)

    with engine.connect() as connection:
        row = connection.execute(
            query,
            {
                "scope_key": scope_key,
                "node_hash": node_hash,
            },
        ).first()

    if row is None:
        return None

    item = dict(row._mapping)
    return {
        "node_hash": str(item["node_hash"]),
        "lon": float(item["lon"]),
        "lat": float(item["lat"]),
    }


def bearing_degrees(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)

    x = math.sin(dlambda) * math.cos(phi2)
    y = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    )
    angle = math.degrees(math.atan2(x, y))
    return (angle + 360.0) % 360.0


def angle_diff_degrees(a: float, b: float) -> float:
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def load_scope_components(scope_key: str) -> dict[str, int]:
    query = text("""
        SELECT
            source_node_hash,
            target_node_hash
        FROM rail_graph_edges
        WHERE scope_key = :scope_key;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query, {"scope_key": scope_key}).fetchall()

    adjacency: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        src = str(row._mapping["source_node_hash"])
        dst = str(row._mapping["target_node_hash"])
        adjacency[src].append(dst)
        adjacency[dst].append(src)

    component_by_node: dict[str, int] = {}
    component_id = 0

    for node_hash in list(adjacency.keys()):
        if node_hash in component_by_node:
            continue

        component_id += 1
        stack = [node_hash]

        while stack:
            current = stack.pop()
            if current in component_by_node:
                continue

            component_by_node[current] = component_id
            for nxt in adjacency.get(current, []):
                if nxt not in component_by_node:
                    stack.append(nxt)

    return component_by_node


def find_synthetic_gap_pairs(scope_key: str) -> list[dict[str, Any]]:
    endpoints = load_endpoint_nodes(scope_key)
    component_by_node = load_scope_components(scope_key)

    neighbors: dict[str, dict[str, Any] | None] = {}
    for endpoint in endpoints:
        neighbors[endpoint["node_hash"]] = load_single_neighbor(scope_key, endpoint["node_hash"])

    candidates: list[dict[str, Any]] = []

    for i in range(len(endpoints)):
        left = endpoints[i]
        left_neighbor = neighbors.get(left["node_hash"])
        if left_neighbor is None:
            continue

        left_component = component_by_node.get(left["node_hash"])
        if left_component is None:
            continue

        left_bearing = bearing_degrees(
            left_neighbor["lon"],
            left_neighbor["lat"],
            left["lon"],
            left["lat"],
        )

        for j in range(i + 1, len(endpoints)):
            right = endpoints[j]
            right_neighbor = neighbors.get(right["node_hash"])
            if right_neighbor is None:
                continue

            right_component = component_by_node.get(right["node_hash"])
            if right_component is None:
                continue

            if left_component == right_component:
                continue

            gap_km = haversine_km(
                left["lon"],
                left["lat"],
                right["lon"],
                right["lat"],
            )
            gap_m = gap_km * 1000.0
            if gap_m > SYNTHETIC_GAP_MAX_DISTANCE_M:
                continue

            left_to_right = bearing_degrees(
                left["lon"],
                left["lat"],
                right["lon"],
                right["lat"],
            )
            right_bearing = bearing_degrees(
                right_neighbor["lon"],
                right_neighbor["lat"],
                right["lon"],
                right["lat"],
            )
            right_to_left = bearing_degrees(
                right["lon"],
                right["lat"],
                left["lon"],
                left["lat"],
            )

            left_angle_error = angle_diff_degrees(left_bearing, left_to_right)
            right_angle_error = angle_diff_degrees(right_bearing, right_to_left)

            if left_angle_error > SYNTHETIC_GAP_MAX_ANGLE_DIFF_DEG:
                continue
            if right_angle_error > SYNTHETIC_GAP_MAX_ANGLE_DIFF_DEG:
                continue

            score = gap_m + left_angle_error * 2.0 + right_angle_error * 2.0

            candidates.append(
                {
                    "from_node_hash": left["node_hash"],
                    "to_node_hash": right["node_hash"],
                    "from_lon": left["lon"],
                    "from_lat": left["lat"],
                    "to_lon": right["lon"],
                    "to_lat": right["lat"],
                    "gap_km": gap_km,
                    "gap_m": gap_m,
                    "from_component_id": left_component,
                    "to_component_id": right_component,
                    "left_angle_error": left_angle_error,
                    "right_angle_error": right_angle_error,
                    "score": score,
                }
            )

    candidates.sort(
        key=lambda item: (
            float(item["score"]),
            float(item["gap_m"]),
            item["from_node_hash"],
            item["to_node_hash"],
        )
    )

    selected: list[dict[str, Any]] = []
    used_nodes: set[str] = set()

    for item in candidates:
        if item["from_node_hash"] in used_nodes:
            continue
        if item["to_node_hash"] in used_nodes:
            continue

        selected.append(item)
        used_nodes.add(item["from_node_hash"])
        used_nodes.add(item["to_node_hash"])

        if len(selected) >= SYNTHETIC_GAP_MAX_PAIRS:
            break

    return selected


def insert_synthetic_gap_edges(scope_key: str, pairs: list[dict[str, Any]]) -> int:
    if not pairs:
        return 0

    insert_query = text("""
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
            ST_GeomFromText(:wkt, 4326)
        );
    """)

    with engine.begin() as connection:
        for item in pairs:
            wkt = (
                f"LINESTRING("
                f"{item['from_lon']} {item['from_lat']},"
                f"{item['to_lon']} {item['to_lat']}"
                f")"
            )

            connection.execute(
                insert_query,
                {
                    "scope_key": scope_key,
                    "source_node_hash": item["from_node_hash"],
                    "target_node_hash": item["to_node_hash"],
                    "length_km": float(item["gap_km"]),
                    "wkt": wkt,
                },
            )

    return len(pairs)


def bridge_synthetic_gaps_for_scope(scope_key: str) -> dict[str, Any]:
    pairs = find_synthetic_gap_pairs(scope_key)
    inserted_count = insert_synthetic_gap_edges(scope_key, pairs)

    diagnostics = {
        "scope_key": scope_key,
        "pairs_found": len(pairs),
        "pairs_inserted": inserted_count,
        "pairs": pairs,
    }

    logger = globals().get("log_event")
    if callable(logger):
        logger(
            "info",
            "synthetic_gap_bridges_created",
            scope_key=scope_key,
            pairs_inserted=inserted_count,
            pairs=pairs[:20],
        )
    else:
        print(
            "[synthetic-gap] synthetic_gap_bridges_created: "
            f"scope_key={scope_key}, pairs_inserted={inserted_count}"
        )

    return diagnostics
'''

text = insert_before_first_function(text, helper_block)


call_block = '''
synthetic_diag = bridge_synthetic_gaps_for_scope(scope_key)
'''

text, inserted_call = insert_call_after_edge_build(text, call_block)

target_path.write_text(text, encoding="utf-8")

print(f"OK: patched {target_path}")

if inserted_call:
    print("OK: вызов bridge_synthetic_gaps_for_scope(scope_key) был вставлен автоматически.")
else:
    print(
        "\nВНИМАНИЕ: helper-функции добавлены, но место вызова не найдено автоматически.\n"
        "Вставь вручную в builder pipeline сразу после записи rail_graph_edges:\n\n"
        "    synthetic_diag = bridge_synthetic_gaps_for_scope(scope_key)\n\n"
        "То есть логически:\n\n"
        "    build_nodes_for_scope(...)\n"
        "    build_edges_for_scope(...)\n"
        "    synthetic_diag = bridge_synthetic_gaps_for_scope(scope_key)\n"
        "    build_station_links_for_scope(...)\n"
    )