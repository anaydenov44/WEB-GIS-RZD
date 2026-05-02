from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


LOGGER = logging.getLogger("create_graph_connectors_from_gaps")


ENV_CANDIDATES = [
    Path.cwd() / ".env",
    Path.cwd() / "back" / ".env",
    Path(__file__).resolve().parents[1] / ".env",
    Path(__file__).resolve().parents[2] / ".env",
]


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def load_env_files() -> None:
    if load_dotenv is None:
        return

    seen: set[Path] = set()

    for env_path in ENV_CANDIDATES:
        resolved = env_path.resolve()

        if resolved in seen:
            continue

        seen.add(resolved)

        if resolved.exists():
            load_dotenv(resolved, override=False)
            LOGGER.info("Loaded environment variables from %s", resolved)


def build_database_url_from_env() -> str | None:
    direct_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URL")

    if direct_url:
        return direct_url

    user = (
        os.getenv("POSTGRES_USER")
        or os.getenv("DB_USER")
        or os.getenv("DATABASE_USER")
    )
    password = (
        os.getenv("POSTGRES_PASSWORD")
        or os.getenv("DB_PASSWORD")
        or os.getenv("DATABASE_PASSWORD")
    )
    host = (
        os.getenv("POSTGRES_HOST")
        or os.getenv("DB_HOST")
        or os.getenv("DATABASE_HOST")
        or "localhost"
    )
    port = (
        os.getenv("POSTGRES_PORT")
        or os.getenv("DB_PORT")
        or os.getenv("DATABASE_PORT")
        or "5432"
    )
    database = (
        os.getenv("POSTGRES_DB")
        or os.getenv("DB_NAME")
        or os.getenv("DATABASE_NAME")
    )

    if not user or not password or not database:
        return None

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create virtual topology connectors from accepted gap candidates"
    )

    parser.add_argument("--database-url", default=None)

    parser.add_argument(
        "--scope-key",
        action="append",
        dest="scope_keys",
        help=(
            "Scope key to process. Can be passed multiple times. "
            "If omitted, all scope keys are processed."
        ),
    )

    parser.add_argument(
        "--max-distance-m",
        type=float,
        default=50.0,
        help="Maximum gap distance in meters.",
    )

    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.35,
        help="Minimum candidate confidence.",
    )

    parser.add_argument(
        "--cost-multiplier",
        type=float,
        default=15.0,
        help=(
            "Routing penalty multiplier for connector edges. "
            "Higher value means routing will avoid connectors unless needed."
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=50_000,
        help="Maximum number of connectors to create in one run.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show selected candidates, do not create connectors.",
    )

    parser.add_argument("--verbose", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    configure_logging(args.verbose)
    load_env_files()

    database_url = args.database_url or build_database_url_from_env()

    if not database_url:
        LOGGER.error("Database URL was not provided and could not be built from .env")
        return 2

    if args.cost_multiplier < 1:
        LOGGER.error("--cost-multiplier must be >= 1")
        return 2

    if args.max_distance_m <= 0:
        LOGGER.error("--max-distance-m must be > 0")
        return 2

    if not 0 <= args.min_confidence <= 1:
        LOGGER.error("--min-confidence must be between 0 and 1")
        return 2

    engine = create_engine(database_url, future=True)

    scope_filter = ""
    params: dict[str, object] = {
        "max_distance_m": args.max_distance_m,
        "min_confidence": args.min_confidence,
        "limit": args.limit,
    }

    if args.scope_keys:
        scope_filter = "AND g.scope_key = ANY(:scope_keys)"
        params["scope_keys"] = args.scope_keys

    select_sql = text(
        f"""
        SELECT
            g.id,
            g.scope_key,
            g.source_node_hash,
            g.target_node_hash,
            g.source_component_id,
            g.target_component_id,
            g.distance_m,
            g.confidence,
            g.connector_type,
            ST_AsText(g.geom) AS geom_wkt
        FROM rail_graph_gap_candidates g
        WHERE g.status = 'accepted'
          AND g.distance_m <= :max_distance_m
          AND g.confidence >= :min_confidence
          {scope_filter}
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
        ORDER BY
            g.scope_key ASC,
            g.distance_m ASC,
            g.confidence DESC
        LIMIT :limit
        """
    )

    with engine.connect() as conn:
        rows = conn.execute(select_sql, params).mappings().all()

    LOGGER.info("Accepted gap candidates selected: %s", len(rows))

    if rows:
        by_scope: dict[str, int] = {}
        for row in rows:
            scope_key = str(row["scope_key"])
            by_scope[scope_key] = by_scope.get(scope_key, 0) + 1

        for scope_key, count in sorted(by_scope.items()):
            LOGGER.info("  scope_key=%s selected=%s", scope_key, count)

    if args.dry_run:
        LOGGER.info("Dry run mode: no connectors will be created")

        for row in rows[:50]:
            LOGGER.info(
                (
                    "gap_id=%s scope=%s distance_m=%.2f confidence=%.3f "
                    "components=%s->%s nodes=%s -> %s geom=%s"
                ),
                row["id"],
                row["scope_key"],
                float(row["distance_m"]),
                float(row["confidence"]),
                row["source_component_id"],
                row["target_component_id"],
                row["source_node_hash"],
                row["target_node_hash"],
                row["geom_wkt"],
            )

        if len(rows) > 50:
            LOGGER.info("... and %s more rows", len(rows) - 50)

        return 0

    gap_ids = [int(row["id"]) for row in rows]

    if not gap_ids:
        LOGGER.info("Nothing to create")
        return 0

    insert_sql = text(
        """
        INSERT INTO rail_graph_connectors (
            source_gap_candidate_id,
            scope_key,
            source_node_hash,
            target_node_hash,
            length_km,
            cost_multiplier,
            connector_type,
            confidence,
            enabled,
            geom
        )
        SELECT
            g.id AS source_gap_candidate_id,
            g.scope_key,
            g.source_node_hash,
            g.target_node_hash,
            g.distance_m / 1000.0 AS length_km,
            :cost_multiplier AS cost_multiplier,
            g.connector_type,
            g.confidence,
            TRUE AS enabled,
            g.geom
        FROM rail_graph_gap_candidates g
        WHERE g.id = ANY(:gap_ids)
        RETURNING id, scope_key
        """
    )

    update_sql = text(
        """
        UPDATE rail_graph_gap_candidates
        SET status = 'created'
        WHERE id = ANY(:gap_ids)
        """
    )

    with engine.begin() as conn:
        created_rows = conn.execute(
            insert_sql,
            {
                "gap_ids": gap_ids,
                "cost_multiplier": args.cost_multiplier,
            },
        ).mappings().all()

        conn.execute(update_sql, {"gap_ids": gap_ids})

    LOGGER.info("Created connectors: %s", len(created_rows))

    created_by_scope: dict[str, int] = {}
    for row in created_rows:
        scope_key = str(row["scope_key"])
        created_by_scope[scope_key] = created_by_scope.get(scope_key, 0) + 1

    for scope_key, count in sorted(created_by_scope.items()):
        LOGGER.info("  scope_key=%s created=%s", scope_key, count)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())