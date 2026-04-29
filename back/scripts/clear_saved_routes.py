"""
Dev-only cleanup script for imported/saved RZD routes.

Run from the backend root:

    cd back
    python scripts/clear_saved_routes.py

It deletes only saved route data and route stops. OSM stations, rail lines,
topology tables, dataset metadata and other reference data are not touched.
"""

from __future__ import annotations

from sqlalchemy import text

from app.db import engine


ROUTE_TABLES_IN_DELETE_ORDER = [
    "route_stops",
    "routes",
]


def _reset_postgres_sequence(connection, table_name: str) -> None:
    """Reset SERIAL/BIGSERIAL sequence when the table has an id sequence."""
    sequence_name = connection.execute(
        text("SELECT pg_get_serial_sequence(:table_name, 'id');"),
        {"table_name": table_name},
    ).scalar_one_or_none()

    if not sequence_name:
        return

    # sequence_name is returned by PostgreSQL for a known constant table name.
    connection.execute(text(f"ALTER SEQUENCE {sequence_name} RESTART WITH 1;"))


def main() -> None:
    deleted_counts: dict[str, int] = {}

    with engine.begin() as connection:
        for table_name in ROUTE_TABLES_IN_DELETE_ORDER:
            result = connection.execute(text(f"DELETE FROM {table_name};"))
            deleted_counts[table_name] = int(result.rowcount or 0)

        for table_name in ROUTE_TABLES_IN_DELETE_ORDER:
            try:
                _reset_postgres_sequence(connection, table_name)
            except Exception as exc:
                print(f"Sequence reset skipped for {table_name}: {exc}")

    print("Saved route cleanup completed.")
    for table_name in ROUTE_TABLES_IN_DELETE_ORDER:
        print(f"Deleted from {table_name}: {deleted_counts.get(table_name, 0)}")


if __name__ == "__main__":
    main()
