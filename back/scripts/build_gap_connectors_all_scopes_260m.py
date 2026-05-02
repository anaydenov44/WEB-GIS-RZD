from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app.db import engine


LOGGER = logging.getLogger("build_gap_connectors_all_scopes_260m")


DEFAULT_MAX_GAP_M = 260.0
DEFAULT_COST_MULTIPLIER = 50.0
DEFAULT_SOURCE = "endpoint_snap_260m"


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def get_scope_keys() -> list[str]:
    query = text("""
        SELECT DISTINCT scope_key
        FROM rail_graph_edges
        WHERE scope_key IS NOT NULL
        ORDER BY scope_key;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query).fetchall()

    return [str(row[0]) for row in rows if row[0]]


def table_exists(table_name: str) -> bool:
    query = text("SELECT to_regclass(:table_name);")

    with engine.connect() as connection:
        result = connection.execute(
            query,
            {"table_name": f"public.{table_name}"},
        ).scalar()

    return result is not None


def ensure_required_tables() -> None:
    missing = []

    for table_name in [
        "rail_graph_edges",
        "rail_graph_gap_candidates",
        "rail_graph_connectors",
    ]:
        if not table_exists(table_name):
            missing.append(table_name)

    if missing:
        raise RuntimeError(
            "Не найдены обязательные таблицы: "
            + ", ".join(missing)
            + ". Сначала запусти audit_topology_connectivity.py с --create-tables хотя бы один раз."
        )


def get_table_columns(table_name: str) -> set[str]:
    query = text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = :table_name;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query, {"table_name": table_name}).fetchall()

    return {str(row[0]) for row in rows}


def run_audit_for_scope(
    *,
    scope_key: str,
    max_gap_m: float,
    clear_previous: bool,
    verbose: bool,
) -> None:
    script_path = Path("scripts") / "audit_topology_connectivity.py"

    command = [
        sys.executable,
        str(script_path),
        "--scope-key",
        scope_key,
        "--max-gap-m",
        str(max_gap_m),
    ]

    if clear_previous:
        command.append("--clear-previous")

    if verbose:
        command.append("--verbose")

    LOGGER.info("Running audit for scope_key=%s", scope_key)

    subprocess.run(command, check=True)


def accept_gap_candidates_for_scope(
    *,
    scope_key: str,
    max_gap_m: float,
) -> int:
    query = text("""
        UPDATE rail_graph_gap_candidates
        SET status = 'accepted'
        WHERE scope_key = :scope_key
          AND distance_m <= :max_gap_m
          AND status IN ('candidate', 'accepted')
        RETURNING id;
    """)

    with engine.begin() as connection:
        rows = connection.execute(
            query,
            {
                "scope_key": scope_key,
                "max_gap_m": max_gap_m,
            },
        ).fetchall()

    return len(rows)


def create_connectors_for_scope(
    *,
    scope_key: str,
    max_gap_m: float,
    cost_multiplier: float,
    source: str,
) -> int:
    connector_columns = get_table_columns("rail_graph_connectors")
    gap_columns = get_table_columns("rail_graph_gap_candidates")

    required_gap_columns = {
        "scope_key",
        "source_node_hash",
        "target_node_hash",
        "distance_m",
        "geom",
        "status",
    }

    missing_gap_columns = sorted(required_gap_columns - gap_columns)
    if missing_gap_columns:
        raise RuntimeError(
            "В rail_graph_gap_candidates не хватает колонок: "
            + ", ".join(missing_gap_columns)
        )

    required_connector_columns = {
        "scope_key",
        "source_node_hash",
        "target_node_hash",
        "length_km",
        "geom",
    }

    missing_connector_columns = sorted(required_connector_columns - connector_columns)
    if missing_connector_columns:
        raise RuntimeError(
            "В rail_graph_connectors не хватает колонок: "
            + ", ".join(missing_connector_columns)
        )

    insert_columns = [
        "scope_key",
        "source_node_hash",
        "target_node_hash",
        "length_km",
        "geom",
    ]

    select_values = [
        "g.scope_key",
        "g.source_node_hash",
        "g.target_node_hash",
        "g.distance_m / 1000.0",
        "g.geom",
    ]

    if "enabled" in connector_columns:
        insert_columns.append("enabled")
        select_values.append("TRUE")

    if "cost_multiplier" in connector_columns:
        insert_columns.append("cost_multiplier")
        select_values.append(":cost_multiplier")

    source_column = None
    for candidate in [
        "connector_source",
        "source",
        "edge_source",
        "created_source",
        "reason",
    ]:
        if candidate in connector_columns:
            source_column = candidate
            break

    if source_column:
        insert_columns.append(source_column)
        select_values.append(":source")

    if "created_at" in connector_columns:
        insert_columns.append("created_at")
        select_values.append("NOW()")

    if "updated_at" in connector_columns:
        insert_columns.append("updated_at")
        select_values.append("NOW()")

    insert_columns_sql = ", ".join(insert_columns)
    select_values_sql = ", ".join(select_values)

    query = text(f"""
        INSERT INTO rail_graph_connectors ({insert_columns_sql})
        SELECT
            {select_values_sql}
        FROM rail_graph_gap_candidates g
        WHERE g.scope_key = :scope_key
          AND g.status = 'accepted'
          AND g.distance_m <= :max_gap_m
          AND g.source_node_hash IS NOT NULL
          AND g.target_node_hash IS NOT NULL
          AND g.source_node_hash <> g.target_node_hash
          AND g.geom IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM rail_graph_connectors c
              WHERE c.scope_key = g.scope_key
                AND (
                    (
                        c.source_node_hash = g.source_node_hash
                        AND c.target_node_hash = g.target_node_hash
                    )
                    OR
                    (
                        c.source_node_hash = g.target_node_hash
                        AND c.target_node_hash = g.source_node_hash
                    )
                )
          )
        RETURNING id;
    """)

    with engine.begin() as connection:
        rows = connection.execute(
            query,
            {
                "scope_key": scope_key,
                "max_gap_m": max_gap_m,
                "cost_multiplier": cost_multiplier,
                "source": source,
            },
        ).fetchall()

    return len(rows)


def print_summary() -> None:
    query = text("""
        SELECT
            scope_key,
            COUNT(*) AS connectors_count,
            ROUND(MIN(length_km * 1000.0)::numeric, 2) AS min_m,
            ROUND(AVG(length_km * 1000.0)::numeric, 2) AS avg_m,
            ROUND(MAX(length_km * 1000.0)::numeric, 2) AS max_m
        FROM rail_graph_connectors
        WHERE length_km <= 0.260
        GROUP BY scope_key
        ORDER BY scope_key;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query).fetchall()

    print("\nConnectors <= 260m by scope_key:")
    for row in rows:
        print(
            f"{row.scope_key}\t"
            f"count={row.connectors_count}\t"
            f"min={row.min_m}m\t"
            f"avg={row.avg_m}m\t"
            f"max={row.max_m}m"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit all scope_key values and create graph connectors for all gaps <= 260m."
    )
    parser.add_argument("--max-gap-m", type=float, default=DEFAULT_MAX_GAP_M)
    parser.add_argument("--cost-multiplier", type=float, default=DEFAULT_COST_MULTIPLIER)
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--clear-previous", action="store_true")
    parser.add_argument("--skip-audit", action="store_true")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args()
    setup_logging(args.verbose)

    ensure_required_tables()

    scope_keys = get_scope_keys()
    if not scope_keys:
        raise RuntimeError("Не найдено ни одного scope_key в rail_graph_edges")

    print(f"Found scope_key count: {len(scope_keys)}")
    for scope_key in scope_keys:
        print(f" - {scope_key}")

    total_accepted = 0
    total_created = 0

    for index, scope_key in enumerate(scope_keys, start=1):
        print(f"\n[{index}/{len(scope_keys)}] scope_key={scope_key}")

        if not args.skip_audit:
            run_audit_for_scope(
                scope_key=scope_key,
                max_gap_m=args.max_gap_m,
                clear_previous=args.clear_previous,
                verbose=args.verbose,
            )

        accepted_count = accept_gap_candidates_for_scope(
            scope_key=scope_key,
            max_gap_m=args.max_gap_m,
        )

        created_count = create_connectors_for_scope(
            scope_key=scope_key,
            max_gap_m=args.max_gap_m,
            cost_multiplier=args.cost_multiplier,
            source=args.source,
        )

        total_accepted += accepted_count
        total_created += created_count

        print(
            f"accepted_candidates={accepted_count}, "
            f"created_connectors={created_count}"
        )

    print("\nDone.")
    print(f"Total accepted candidates: {total_accepted}")
    print(f"Total created connectors: {total_created}")

    print_summary()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())