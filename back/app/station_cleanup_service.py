from sqlalchemy import text

from app.db import engine


def apply_city_station_cleanup(region_code: str | None = None) -> list[dict]:
    """
    Удаляет городские halt/platform/stop после обновления OSM-данных.

    Важно:
    - удаляются только станции внутри city_halt_cleanup_zones;
    - удаляются только halt/platform/stop;
    - станции, которые уже используются в route_stops, не удаляются;
    - перед удалением станции архивируются в deleted_station_archive.
    """

    query = text("""
        select *
        from apply_station_cleanup_city_halts(
          p_region_code => :region_code
        );
    """)

    with engine.begin() as connection:
        rows = connection.execute(
            query,
            {"region_code": region_code},
        ).mappings().all()

    return [dict(row) for row in rows]
