import json
import sys

import osmium
from psycopg2.extras import execute_values
from shapely.geometry import LineString, Polygon

from pipeline_utils import get_db_connection, get_pbf_path


BATCH_SIZE = 1000


class StageRailwayImporter(osmium.SimpleHandler):
    def __init__(self, conn, region_code: str):
        super().__init__()
        self.conn = conn
        self.region_code = region_code
        self.station_rows = []
        self.line_rows = []
        self.station_count = 0
        self.line_count = 0
        self.skipped_station_ways = 0
        self.skipped_line_ways = 0
        self.skipped_relations = 0

    def flush_stations(self):
        if not self.station_rows:
            return

        sql = """
            INSERT INTO osm_stations_raw_stage (
                region_code,
                osm_element_type,
                osm_id,
                name,
                railway,
                tags_json,
                geom
            )
            VALUES %s;
        """

        template = """
            (%s, %s, %s, %s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromText(%s), 4326))
        """

        with self.conn.cursor() as cur:
            execute_values(cur, sql, self.station_rows, template=template)

        self.conn.commit()
        self.station_rows.clear()

    def flush_lines(self):
        if not self.line_rows:
            return

        sql = """
            INSERT INTO osm_rail_lines_raw_stage (
                region_code,
                osm_element_type,
                osm_id,
                name,
                railway,
                tags_json,
                geom
            )
            VALUES %s;
        """

        template = """
            (%s, %s, %s, %s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromText(%s), 4326))
        """

        with self.conn.cursor() as cur:
            execute_values(cur, sql, self.line_rows, template=template)

        self.conn.commit()
        self.line_rows.clear()

    def node(self, n):
        railway = n.tags.get("railway")
        if railway not in ("station", "halt"):
            return
        if not n.location.valid():
            return

        tags_json = json.dumps(dict(n.tags), ensure_ascii=False)
        wkt = f"POINT({n.location.lon} {n.location.lat})"

        self.station_rows.append(
            (
                self.region_code,
                "node",
                str(n.id),
                n.tags.get("name"),
                railway,
                tags_json,
                wkt,
            )
        )
        self.station_count += 1

        if len(self.station_rows) >= BATCH_SIZE:
            self.flush_stations()

    def way(self, w):
        railway = w.tags.get("railway")
        if railway not in ("station", "halt", "rail"):
            return

        coords = []
        for node_ref in w.nodes:
            if node_ref.location.valid():
                coords.append((node_ref.lon, node_ref.lat))

        if railway in ("station", "halt"):
            if len(coords) < 2:
                self.skipped_station_ways += 1
                return

            try:
                if len(coords) >= 4 and coords[0] == coords[-1]:
                    geom = Polygon(coords).centroid
                else:
                    geom = LineString(coords).centroid
            except Exception:
                self.skipped_station_ways += 1
                return

            tags_json = json.dumps(dict(w.tags), ensure_ascii=False)

            self.station_rows.append(
                (
                    self.region_code,
                    "way",
                    str(w.id),
                    w.tags.get("name"),
                    railway,
                    tags_json,
                    geom.wkt,
                )
            )
            self.station_count += 1

            if len(self.station_rows) >= BATCH_SIZE:
                self.flush_stations()

        elif railway == "rail":
            if len(coords) < 2:
                self.skipped_line_ways += 1
                return

            try:
                geom = LineString(coords)
            except Exception:
                self.skipped_line_ways += 1
                return

            tags_json = json.dumps(dict(w.tags), ensure_ascii=False)

            self.line_rows.append(
                (
                    self.region_code,
                    "way",
                    str(w.id),
                    w.tags.get("name"),
                    railway,
                    tags_json,
                    geom.wkt,
                )
            )
            self.line_count += 1

            if len(self.line_rows) >= BATCH_SIZE:
                self.flush_lines()

    def relation(self, r):
        railway = r.tags.get("railway")
        if railway in ("station", "halt", "rail"):
            self.skipped_relations += 1

    def finish(self):
        self.flush_stations()
        self.flush_lines()


def delete_region_stage(conn, region_code: str):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM osm_stations_raw_stage WHERE region_code = %s;", (region_code,))
        cur.execute("DELETE FROM osm_rail_lines_raw_stage WHERE region_code = %s;", (region_code,))
    conn.commit()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python import_region_raw_pbf.py <region_code>")

    region_code = sys.argv[1]
    pbf_path = get_pbf_path(region_code)

    if not pbf_path.exists():
        raise FileNotFoundError(f"Не найден PBF: {pbf_path}")

    conn = get_db_connection()
    try:
        print(f"[{region_code}] Очищаю staging только для этого округа...")
        delete_region_stage(conn, region_code)

        print(f"[{region_code}] Импортирую railway-объекты из PBF в staging...")
        importer = StageRailwayImporter(conn, region_code)
        importer.apply_file(str(pbf_path), locations=True)
        importer.finish()

        print(f"[{region_code}] Stage import завершён.")
        print(f"[{region_code}] stations_stage: {importer.station_count}")
        print(f"[{region_code}] lines_stage: {importer.line_count}")
        print(f"[{region_code}] skipped station/halt ways: {importer.skipped_station_ways}")
        print(f"[{region_code}] skipped rail ways: {importer.skipped_line_ways}")
        print(f"[{region_code}] skipped relations: {importer.skipped_relations}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()