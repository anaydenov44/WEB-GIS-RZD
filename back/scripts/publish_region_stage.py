import sys

from pipeline_utils import get_db_connection, get_region_meta

MIN_ACCEPTABLE_RATIO = 0.5


def fetch_scalar(cur, sql: str, params: tuple):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def deduplicate_stage(conn, region_code: str) -> dict:
    with conn.cursor() as cur:
        duplicate_station_groups = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM osm_stations_raw_stage
                WHERE region_code = %s
                GROUP BY region_code, osm_element_type, osm_id
                HAVING COUNT(*) > 1
            ) t;
            """,
            (region_code,),
        )
        duplicate_line_groups = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM osm_rail_lines_raw_stage
                WHERE region_code = %s
                GROUP BY region_code, osm_element_type, osm_id
                HAVING COUNT(*) > 1
            ) t;
            """,
            (region_code,),
        )

        cur.execute(
            """
            DELETE FROM osm_stations_raw_stage s
            USING osm_stations_raw_stage d
            WHERE s.region_code = %s
              AND d.region_code = s.region_code
              AND d.osm_element_type = s.osm_element_type
              AND d.osm_id = s.osm_id
              AND d.ctid > s.ctid;
            """,
            (region_code,),
        )
        deleted_station_rows = cur.rowcount

        cur.execute(
            """
            DELETE FROM osm_rail_lines_raw_stage s
            USING osm_rail_lines_raw_stage d
            WHERE s.region_code = %s
              AND d.region_code = s.region_code
              AND d.osm_element_type = s.osm_element_type
              AND d.osm_id = s.osm_id
              AND d.ctid > s.ctid;
            """,
            (region_code,),
        )
        deleted_line_rows = cur.rowcount

    return {
        "duplicate_station_groups_before": duplicate_station_groups,
        "duplicate_line_groups_before": duplicate_line_groups,
        "deleted_station_rows": deleted_station_rows,
        "deleted_line_rows": deleted_line_rows,
    }


def validate_stage(conn, region_code: str) -> dict:
    with conn.cursor() as cur:
        stage_stations = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM osm_stations_raw_stage
            WHERE region_code = %s;
            """,
            (region_code,),
        )
        stage_lines = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM osm_rail_lines_raw_stage
            WHERE region_code = %s;
            """,
            (region_code,),
        )
        invalid_station_geoms = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM osm_stations_raw_stage
            WHERE region_code = %s
              AND (geom IS NULL OR NOT ST_IsValid(geom));
            """,
            (region_code,),
        )
        invalid_line_geoms = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM osm_rail_lines_raw_stage
            WHERE region_code = %s
              AND (geom IS NULL OR NOT ST_IsValid(geom));
            """,
            (region_code,),
        )
        duplicate_station_keys = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM osm_stations_raw_stage
                WHERE region_code = %s
                GROUP BY region_code, osm_element_type, osm_id
                HAVING COUNT(*) > 1
            ) t;
            """,
            (region_code,),
        )
        duplicate_line_keys = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM (
                SELECT 1
                FROM osm_rail_lines_raw_stage
                WHERE region_code = %s
                GROUP BY region_code, osm_element_type, osm_id
                HAVING COUNT(*) > 1
            ) t;
            """,
            (region_code,),
        )
        prev_raw_stations = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM osm_stations_raw
            WHERE region_code = %s;
            """,
            (region_code,),
        )
        prev_raw_lines = fetch_scalar(
            cur,
            """
            SELECT COUNT(*)
            FROM osm_rail_lines_raw
            WHERE region_code = %s;
            """,
            (region_code,),
        )

    errors = []

    if stage_stations == 0:
        errors.append("staging stations count = 0")
    if stage_lines == 0:
        errors.append("staging lines count = 0")
    if invalid_station_geoms > 0:
        errors.append(f"invalid station geometries = {invalid_station_geoms}")
    if invalid_line_geoms > 0:
        errors.append(f"invalid line geometries = {invalid_line_geoms}")
    if duplicate_station_keys > 0:
        errors.append(f"duplicate station keys = {duplicate_station_keys}")
    if duplicate_line_keys > 0:
        errors.append(f"duplicate line keys = {duplicate_line_keys}")

    if prev_raw_stations > 0 and stage_stations < max(1, int(prev_raw_stations * MIN_ACCEPTABLE_RATIO)):
        errors.append(
            f"stations count too small versus previous raw: stage={stage_stations}, previous={prev_raw_stations}"
        )

    if prev_raw_lines > 0 and stage_lines < max(1, int(prev_raw_lines * MIN_ACCEPTABLE_RATIO)):
        errors.append(
            f"lines count too small versus previous raw: stage={stage_lines}, previous={prev_raw_lines}"
        )

    return {
        "stage_stations": stage_stations,
        "stage_lines": stage_lines,
        "invalid_station_geoms": invalid_station_geoms,
        "invalid_line_geoms": invalid_line_geoms,
        "duplicate_station_keys": duplicate_station_keys,
        "duplicate_line_keys": duplicate_line_keys,
        "prev_raw_stations": prev_raw_stations,
        "prev_raw_lines": prev_raw_lines,
        "errors": errors,
    }


def publish_stage_to_raw(conn, region_code: str):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM osm_stations_raw WHERE region_code = %s;", (region_code,))
        cur.execute(
            """
            INSERT INTO osm_stations_raw (
                region_code,
                osm_element_type,
                osm_id,
                name,
                railway,
                tags_json,
                geom
            )
            SELECT DISTINCT ON (region_code, osm_element_type, osm_id)
                region_code,
                osm_element_type,
                osm_id,
                name,
                railway,
                tags_json,
                geom
            FROM osm_stations_raw_stage
            WHERE region_code = %s
            ORDER BY region_code, osm_element_type, osm_id, ctid DESC;
            """,
            (region_code,),
        )

        cur.execute("DELETE FROM osm_rail_lines_raw WHERE region_code = %s;", (region_code,))
        cur.execute(
            """
            INSERT INTO osm_rail_lines_raw (
                region_code,
                osm_element_type,
                osm_id,
                name,
                railway,
                tags_json,
                geom
            )
            SELECT DISTINCT ON (region_code, osm_element_type, osm_id)
                region_code,
                osm_element_type,
                osm_id,
                name,
                railway,
                tags_json,
                geom
            FROM osm_rail_lines_raw_stage
            WHERE region_code = %s
            ORDER BY region_code, osm_element_type, osm_id, ctid DESC;
            """,
            (region_code,),
        )

        cur.execute("DELETE FROM osm_stations_raw_stage WHERE region_code = %s;", (region_code,))
        cur.execute("DELETE FROM osm_rail_lines_raw_stage WHERE region_code = %s;", (region_code,))


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python publish_region_stage.py <region_code>")

    region_code = sys.argv[1]
    meta = get_region_meta(region_code)

    conn = get_db_connection()
    try:
        print(f"[{region_code}] Дедупликация staging перед validation...")
        dedup_stats = deduplicate_stage(conn, region_code)
        conn.commit()

        print(f"[{region_code}] duplicate station groups before: {dedup_stats['duplicate_station_groups_before']}")
        print(f"[{region_code}] duplicate line groups before: {dedup_stats['duplicate_line_groups_before']}")
        print(f"[{region_code}] deleted duplicate station rows: {dedup_stats['deleted_station_rows']}")
        print(f"[{region_code}] deleted duplicate line rows: {dedup_stats['deleted_line_rows']}")

        print(f"[{region_code}] Проверяю staging перед публикацией...")
        stats = validate_stage(conn, region_code)

        print(f"[{region_code}] stage stations: {stats['stage_stations']}")
        print(f"[{region_code}] stage lines: {stats['stage_lines']}")
        print(f"[{region_code}] invalid station geometries: {stats['invalid_station_geoms']}")
        print(f"[{region_code}] invalid line geometries: {stats['invalid_line_geoms']}")
        print(f"[{region_code}] duplicate station keys: {stats['duplicate_station_keys']}")
        print(f"[{region_code}] duplicate line keys: {stats['duplicate_line_keys']}")
        print(f"[{region_code}] previous raw stations: {stats['prev_raw_stations']}")
        print(f"[{region_code}] previous raw lines: {stats['prev_raw_lines']}")

        if stats["errors"]:
            raise RuntimeError(
                f"[{region_code}] Публикация отменена. Ошибки validation: " + "; ".join(stats["errors"])
            )

        print(f"[{region_code}] Публикую staging -> raw main для {meta['label']}...")
        publish_stage_to_raw(conn, region_code)
        conn.commit()
        print(f"[{region_code}] Публикация в raw main завершена.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()