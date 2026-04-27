import sys

from pipeline_utils import get_db_connection, get_region_meta


REGION_CODES = [
    "central_fd",
    "northwestern_fd",
    "south_fd",
    "north_caucasus_fd",
    "volga_fd",
    "ural_fd",
    "siberian_fd",
    "far_eastern_fd",
]

RZD_PATTERNS = [
    "ржд",
    "российские железные дороги",
    "октябрьская железная дорога",
    "калининградская железная дорога",
    "московская железная дорога",
    "горьковская железная дорога",
    "северная железная дорога",
    "северо-кавказская железная дорога",
    "юго-восточная железная дорога",
    "приволжская железная дорога",
    "куйбышевская железная дорога",
    "свердловская железная дорога",
    "южно-уральская железная дорога",
    "западно-сибирская железная дорога",
    "красноярская железная дорога",
    "восточно-сибирская железная дорога",
    "забайкальская железная дорога",
    "дальневосточная железная дорога",
    "сахалинская железная дорога",
]


def build_like_any_sql(expr: str, patterns: list[str]) -> str:
    quoted = []
    for pattern in patterns:
        escaped = pattern.replace("'", "''")
        quoted.append(f"'%%{escaped}%%'")
    return f"{expr} LIKE ANY (ARRAY[{', '.join(quoted)}])"


RZD_MATCH_SQL = f"""
(
    {build_like_any_sql("LOWER(COALESCE(r.tags_json ->> 'operator', ''))", RZD_PATTERNS)}
    OR {build_like_any_sql("LOWER(COALESCE(r.tags_json ->> 'operator:branch', ''))", RZD_PATTERNS)}
    OR {build_like_any_sql("LOWER(COALESCE(r.tags_json ->> 'network', ''))", RZD_PATTERNS)}
    OR {build_like_any_sql("LOWER(COALESCE(r.tags_json ->> 'owner', ''))", RZD_PATTERNS)}
)
"""

STATION_IS_SUBWAY_SQL = """
(
    LOWER(COALESCE(r.tags_json ->> 'station', '')) = 'subway'
    OR LOWER(COALESCE(r.tags_json ->> 'railway', '')) = 'subway'
    OR LOWER(COALESCE(r.tags_json ->> 'subway', '')) IN ('yes', 'true', '1')
    OR LOWER(COALESCE(r.tags_json ->> 'public_transport', '')) = 'station'
       AND LOWER(COALESCE(r.tags_json ->> 'station', '')) = 'subway'
    OR LOWER(COALESCE(r.tags_json ->> 'operator', '')) LIKE '%%метрополитен%%'
    OR LOWER(COALESCE(r.tags_json ->> 'operator', '')) LIKE '%%metro%%'
    OR LOWER(COALESCE(r.tags_json ->> 'network', '')) LIKE '%%метро%%'
    OR LOWER(COALESCE(r.tags_json ->> 'network', '')) LIKE '%%metro%%'
    OR LOWER(COALESCE(r.name, '')) LIKE 'метро %%'
)
"""

STATION_HAS_MAIN_RAIL_SIGNS_SQL = f"""
(
    {RZD_MATCH_SQL}
    OR NULLIF(r.tags_json ->> 'uic_ref', '') IS NOT NULL
    OR NULLIF(r.tags_json ->> 'esr:user', '') IS NOT NULL
)
"""

LINE_HAS_SERVICE_TAG_SQL = """
NULLIF(BTRIM(COALESCE(r.tags_json ->> 'service', '')), '') IS NOT NULL
"""

LINE_IS_INDUSTRIAL_SQL = """
LOWER(COALESCE(r.tags_json ->> 'usage', '')) = 'industrial'
"""

LINE_IS_NON_PASSENGER_SQL = """
LOWER(COALESCE(r.tags_json ->> 'passenger', '')) = 'no'
"""

LINE_IS_VISIBLE_DEFAULT_SQL = f"""
(
    r.railway = 'rail'
    AND NOT ({LINE_HAS_SERVICE_TAG_SQL})
    AND NOT ({LINE_IS_INDUSTRIAL_SQL})
    AND NOT ({LINE_IS_NON_PASSENGER_SQL})
    AND COALESCE(r.tags_json ->> 'usage', '') IN ('main', 'branch')
)
"""


def normalize_one_region(region_code: str) -> dict:
    meta = get_region_meta(region_code)
    region_label = meta["label"]

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            print(f"[{region_code}] Собираю входную статистику...")

            cur.execute(
                """
                SELECT COUNT(*)
                FROM stations
                WHERE region_code = %s
                  AND is_visible_default = TRUE;
                """,
                (region_code,),
            )
            stations_visible_before = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM rail_lines
                WHERE region_code = %s
                  AND is_visible_default = TRUE;
                """,
                (region_code,),
            )
            lines_visible_before = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM osm_stations_raw
                WHERE region_code = %s;
                """,
                (region_code,),
            )
            stations_raw_count = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM osm_rail_lines_raw
                WHERE region_code = %s;
                """,
                (region_code,),
            )
            lines_raw_count = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_stations_raw r
                WHERE r.region_code = %s
                  AND r.railway IN ('station', 'halt');
                """,
                (region_code,),
            )
            stations_raw_station_halt = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_stations_raw r
                WHERE r.region_code = %s
                  AND r.railway IN ('station', 'halt')
                  AND {STATION_IS_SUBWAY_SQL};
                """,
                (region_code,),
            )
            stations_subway_candidates = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_stations_raw r
                WHERE r.region_code = %s
                  AND r.railway IN ('station', 'halt')
                  AND {RZD_MATCH_SQL};
                """,
                (region_code,),
            )
            stations_rzd_affiliation_candidates = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_stations_raw r
                WHERE r.region_code = %s
                  AND r.railway IN ('station', 'halt')
                  AND {STATION_HAS_MAIN_RAIL_SIGNS_SQL};
                """,
                (region_code,),
            )
            stations_main_rail_candidates = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_rail_lines_raw r
                WHERE r.region_code = %s
                  AND r.railway = 'rail';
                """,
                (region_code,),
            )
            lines_raw_rail = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_rail_lines_raw r
                WHERE r.region_code = %s
                  AND r.railway = 'rail'
                  AND {LINE_HAS_SERVICE_TAG_SQL};
                """,
                (region_code,),
            )
            lines_service_tag_candidates = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_rail_lines_raw r
                WHERE r.region_code = %s
                  AND r.railway = 'rail'
                  AND {LINE_IS_INDUSTRIAL_SQL};
                """,
                (region_code,),
            )
            lines_industrial_candidates = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT COUNT(*)
                FROM osm_rail_lines_raw r
                WHERE r.region_code = %s
                  AND r.railway = 'rail'
                  AND {LINE_IS_NON_PASSENGER_SQL};
                """,
                (region_code,),
            )
            lines_non_passenger_candidates = cur.fetchone()[0]

            print(f"[{region_code}] Очищаю core только для этого округа...")
            cur.execute("DELETE FROM stations WHERE region_code = %s;", (region_code,))
            cur.execute("DELETE FROM rail_lines WHERE region_code = %s;", (region_code,))

            print(f"[{region_code}] Наполняю stations...")
            cur.execute(
                f"""
                INSERT INTO stations (
                    region_code,
                    osm_element_type,
                    osm_id,
                    name,
                    station_type,
                    region,
                    operator_name,
                    operator_branch,
                    uic_ref,
                    esr_user,
                    is_main_rail_station,
                    is_visible_default,
                    exclude_reason,
                    geom
                )
                SELECT
                    r.region_code,
                    r.osm_element_type,
                    r.osm_id,
                    r.name,
                    r.railway AS station_type,
                    %s AS region,
                    r.tags_json ->> 'operator' AS operator_name,
                    r.tags_json ->> 'operator:branch' AS operator_branch,
                    r.tags_json ->> 'uic_ref' AS uic_ref,
                    r.tags_json ->> 'esr:user' AS esr_user,

                    CASE
                        WHEN {STATION_IS_SUBWAY_SQL} THEN FALSE
                        WHEN {STATION_HAS_MAIN_RAIL_SIGNS_SQL} THEN TRUE
                        ELSE FALSE
                    END AS is_main_rail_station,

                    CASE
                        WHEN {STATION_IS_SUBWAY_SQL} THEN FALSE
                        WHEN {STATION_HAS_MAIN_RAIL_SIGNS_SQL} THEN TRUE
                        ELSE FALSE
                    END AS is_visible_default,

                    CASE
                        WHEN {STATION_IS_SUBWAY_SQL} THEN 'subway_or_metro'
                        WHEN NOT ({STATION_HAS_MAIN_RAIL_SIGNS_SQL}) THEN 'missing_main_rail_signs'
                        ELSE NULL
                    END AS exclude_reason,

                    r.geom
                FROM osm_stations_raw r
                WHERE r.region_code = %s
                  AND r.railway IN ('station', 'halt');
                """,
                (region_label, region_code),
            )

            print(f"[{region_code}] Наполняю rail_lines...")
            cur.execute(
                f"""
                INSERT INTO rail_lines (
                    region_code,
                    osm_element_type,
                    osm_id,
                    name,
                    line_type,
                    region,
                    operator_name,
                    operator_branch,
                    usage_type,
                    service_type,
                    is_service_line,
                    is_main_passenger_line,
                    is_visible_default,
                    exclude_reason,
                    geom
                )
                SELECT
                    r.region_code,
                    r.osm_element_type,
                    r.osm_id,
                    r.name,
                    r.railway AS line_type,
                    %s AS region,
                    r.tags_json ->> 'operator' AS operator_name,
                    r.tags_json ->> 'operator:branch' AS operator_branch,
                    r.tags_json ->> 'usage' AS usage_type,
                    r.tags_json ->> 'service' AS service_type,

                    CASE
                        WHEN ({LINE_HAS_SERVICE_TAG_SQL}) OR ({LINE_IS_INDUSTRIAL_SQL}) THEN TRUE
                        ELSE FALSE
                    END AS is_service_line,

                    CASE
                        WHEN {LINE_IS_VISIBLE_DEFAULT_SQL} THEN TRUE
                        ELSE FALSE
                    END AS is_main_passenger_line,

                    CASE
                        WHEN {LINE_IS_VISIBLE_DEFAULT_SQL} THEN TRUE
                        ELSE FALSE
                    END AS is_visible_default,

                    CASE
                        WHEN {LINE_HAS_SERVICE_TAG_SQL} THEN 'service_line'
                        WHEN {LINE_IS_INDUSTRIAL_SQL} THEN 'industrial_line'
                        WHEN {LINE_IS_NON_PASSENGER_SQL} THEN 'non_passenger_line'
                        WHEN COALESCE(r.tags_json ->> 'usage', '') NOT IN ('main', 'branch') THEN 'non_main_or_branch'
                        ELSE NULL
                    END AS exclude_reason,

                    r.geom
                FROM osm_rail_lines_raw r
                WHERE r.region_code = %s
                  AND r.railway = 'rail';
                """,
                (region_label, region_code),
            )

        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM stations WHERE region_code = %s;", (region_code,))
            stations_core_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM rail_lines WHERE region_code = %s;", (region_code,))
            lines_core_count = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM stations
                WHERE region_code = %s
                  AND is_visible_default = TRUE;
                """,
                (region_code,),
            )
            stations_visible_after = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM rail_lines
                WHERE region_code = %s
                  AND is_visible_default = TRUE;
                """,
                (region_code,),
            )
            lines_visible_after = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM stations
                WHERE region_code = %s
                  AND exclude_reason = 'subway_or_metro';
                """,
                (region_code,),
            )
            stations_hidden_subway = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM stations
                WHERE region_code = %s
                  AND exclude_reason = 'missing_main_rail_signs';
                """,
                (region_code,),
            )
            stations_hidden_missing_signs = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM rail_lines
                WHERE region_code = %s
                  AND exclude_reason = 'service_line';
                """,
                (region_code,),
            )
            lines_hidden_service = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM rail_lines
                WHERE region_code = %s
                  AND exclude_reason = 'industrial_line';
                """,
                (region_code,),
            )
            lines_hidden_industrial = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM rail_lines
                WHERE region_code = %s
                  AND exclude_reason = 'non_passenger_line';
                """,
                (region_code,),
            )
            lines_hidden_non_passenger = cur.fetchone()[0]

            cur.execute(
                """
                SELECT COUNT(*)
                FROM rail_lines
                WHERE region_code = %s
                  AND exclude_reason = 'non_main_or_branch';
                """,
                (region_code,),
            )
            lines_hidden_non_main_or_branch = cur.fetchone()[0]

        print(f"[{region_code}] Готово.")
        print(f"[{region_code}] stations_raw_total: {stations_raw_count}")
        print(f"[{region_code}] lines_raw_total: {lines_raw_count}")
        print(f"[{region_code}] stations_raw_station_halt: {stations_raw_station_halt}")
        print(f"[{region_code}] stations_subway_metro_candidates: {stations_subway_candidates}")
        print(f"[{region_code}] stations_rzd_affiliation_candidates: {stations_rzd_affiliation_candidates}")
        print(f"[{region_code}] stations_main_rail_candidates: {stations_main_rail_candidates}")
        print(f"[{region_code}] lines_raw_rail: {lines_raw_rail}")
        print(f"[{region_code}] lines_service_tag_candidates: {lines_service_tag_candidates}")
        print(f"[{region_code}] lines_industrial_candidates: {lines_industrial_candidates}")
        print(f"[{region_code}] lines_non_passenger_candidates: {lines_non_passenger_candidates}")
        print(f"[{region_code}] stations_core: {stations_core_count}")
        print(f"[{region_code}] lines_core: {lines_core_count}")
        print(f"[{region_code}] stations_visible_default_before: {stations_visible_before}")
        print(f"[{region_code}] stations_visible_default_after:  {stations_visible_after}")
        print(f"[{region_code}] lines_visible_default_before: {lines_visible_before}")
        print(f"[{region_code}] lines_visible_default_after:  {lines_visible_after}")
        print(f"[{region_code}] stations_hidden_subway_or_metro: {stations_hidden_subway}")
        print(f"[{region_code}] stations_hidden_missing_main_rail_signs: {stations_hidden_missing_signs}")
        print(f"[{region_code}] lines_hidden_service_line: {lines_hidden_service}")
        print(f"[{region_code}] lines_hidden_industrial_line: {lines_hidden_industrial}")
        print(f"[{region_code}] lines_hidden_non_passenger_line: {lines_hidden_non_passenger}")
        print(f"[{region_code}] lines_hidden_non_main_or_branch: {lines_hidden_non_main_or_branch}")

        return {
            "region_code": region_code,
            "stations_raw_total": stations_raw_count,
            "lines_raw_total": lines_raw_count,
            "stations_subway_metro_candidates": stations_subway_candidates,
            "stations_rzd_affiliation_candidates": stations_rzd_affiliation_candidates,
            "stations_main_rail_candidates": stations_main_rail_candidates,
            "stations_visible_before": stations_visible_before,
            "stations_visible_after": stations_visible_after,
            "lines_visible_before": lines_visible_before,
            "lines_visible_after": lines_visible_after,
            "stations_hidden_subway": stations_hidden_subway,
            "stations_hidden_missing_signs": stations_hidden_missing_signs,
            "lines_hidden_service": lines_hidden_service,
            "lines_hidden_industrial": lines_hidden_industrial,
            "lines_hidden_non_passenger": lines_hidden_non_passenger,
            "lines_hidden_non_main_or_branch": lines_hidden_non_main_or_branch,
        }

    finally:
        conn.close()


def print_total_summary(results: list[dict]) -> None:
    if not results:
        return

    total = {
        "stations_raw_total": 0,
        "lines_raw_total": 0,
        "stations_subway_metro_candidates": 0,
        "stations_rzd_affiliation_candidates": 0,
        "stations_main_rail_candidates": 0,
        "stations_visible_before": 0,
        "stations_visible_after": 0,
        "lines_visible_before": 0,
        "lines_visible_after": 0,
        "stations_hidden_subway": 0,
        "stations_hidden_missing_signs": 0,
        "lines_hidden_service": 0,
        "lines_hidden_industrial": 0,
        "lines_hidden_non_passenger": 0,
        "lines_hidden_non_main_or_branch": 0,
    }

    for row in results:
        for key in total:
            total[key] += row[key]

    print("[all] ----------------------------------------")
    print("[all] ИТОГОВАЯ СВОДКА ПО ВСЕМ ОКРУГАМ")
    print(f"[all] stations_raw_total: {total['stations_raw_total']}")
    print(f"[all] lines_raw_total: {total['lines_raw_total']}")
    print(f"[all] stations_subway_metro_candidates: {total['stations_subway_metro_candidates']}")
    print(f"[all] stations_rzd_affiliation_candidates: {total['stations_rzd_affiliation_candidates']}")
    print(f"[all] stations_main_rail_candidates: {total['stations_main_rail_candidates']}")
    print(f"[all] stations_visible_default_before: {total['stations_visible_before']}")
    print(f"[all] stations_visible_default_after:  {total['stations_visible_after']}")
    print(f"[all] lines_visible_default_before: {total['lines_visible_before']}")
    print(f"[all] lines_visible_default_after:  {total['lines_visible_after']}")
    print(f"[all] stations_hidden_subway_or_metro: {total['stations_hidden_subway']}")
    print(f"[all] stations_hidden_missing_main_rail_signs: {total['stations_hidden_missing_signs']}")
    print(f"[all] lines_hidden_service_line: {total['lines_hidden_service']}")
    print(f"[all] lines_hidden_industrial_line: {total['lines_hidden_industrial']}")
    print(f"[all] lines_hidden_non_passenger_line: {total['lines_hidden_non_passenger']}")
    print(f"[all] lines_hidden_non_main_or_branch: {total['lines_hidden_non_main_or_branch']}")


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Использование: python normalize_region_core.py <region_code|all>"
        )

    target = sys.argv[1].strip().lower()

    if target == "all":
        results = []
        for region_code in REGION_CODES:
            results.append(normalize_one_region(region_code))
        print_total_summary(results)
        return

    normalize_one_region(target)


if __name__ == "__main__":
    main()