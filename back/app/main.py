import subprocess
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import engine, test_connection
from app.route_import_service import import_route_payload
from app.route_graph_matcher import resolve_route_for_map
from app.virtual_route_service import build_virtual_route_path
from app.topology_build_jobs import (
    get_topology_build_job,
    get_topology_status,
    start_topology_build_job,
)

try:
    from app.route_build_jobs import (
        create_route_build_job,
        ensure_route_topology_built,
        get_route_build_job,
        get_route_build_job_result,
        start_route_build_job,
    )
    ROUTE_BUILD_JOBS_AVAILABLE = True
except ImportError:
    create_route_build_job = None
    ensure_route_topology_built = None
    get_route_build_job = None
    get_route_build_job_result = None
    start_route_build_job = None
    ROUTE_BUILD_JOBS_AVAILABLE = False

try:
    from app.rzd_route_service import (
        import_selected_rzd_train,
        load_routes_for_station_zone,
        resolve_rzd_code_for_station,
        search_rzd_routes,
        search_rzd_routes_calendar,
        search_rzd_routes_calendar_by_stations,
        search_rzd_station_codes,
    )
    RZD_ROUTE_SERVICE_AVAILABLE = True
except ImportError:
    import_selected_rzd_train = None
    load_routes_for_station_zone = None
    resolve_rzd_code_for_station = None
    search_rzd_routes = None
    search_rzd_routes_calendar = None
    search_rzd_routes_calendar_by_stations = None
    search_rzd_station_codes = None
    RZD_ROUTE_SERVICE_AVAILABLE = False

app = FastAPI(title="Railway GIS Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = BASE_DIR / "scripts"

REGION_META = [
    {"code": "central_fd", "label": "Центральный федеральный округ"},
    {"code": "northwestern_fd", "label": "Северо-Западный федеральный округ"},
    {"code": "south_fd", "label": "Южный федеральный округ"},
    {"code": "north_caucasus_fd", "label": "Северо-Кавказский федеральный округ"},
    {"code": "volga_fd", "label": "Приволжский федеральный округ"},
    {"code": "ural_fd", "label": "Уральский федеральный округ"},
    {"code": "siberian_fd", "label": "Сибирский федеральный округ"},
    {"code": "far_eastern_fd", "label": "Дальневосточный федеральный округ"},
]

REGION_META_BY_CODE = {item["code"]: item for item in REGION_META}

MD5_URL_MAP = {
    "central_fd": "https://download.geofabrik.de/russia/central-fed-district-latest.osm.pbf.md5",
    "northwestern_fd": "https://download.geofabrik.de/russia/northwestern-fed-district-latest.osm.pbf.md5",
    "south_fd": "https://download.geofabrik.de/russia/south-fed-district-latest.osm.pbf.md5",
    "north_caucasus_fd": "https://download.geofabrik.de/russia/north-caucasus-fed-district-latest.osm.pbf.md5",
    "volga_fd": "https://download.geofabrik.de/russia/volga-fed-district-latest.osm.pbf.md5",
    "ural_fd": "https://download.geofabrik.de/russia/ural-fed-district-latest.osm.pbf.md5",
    "siberian_fd": "https://download.geofabrik.de/russia/siberian-fed-district-latest.osm.pbf.md5",
    "far_eastern_fd": "https://download.geofabrik.de/russia/far-eastern-fed-district-latest.osm.pbf.md5",
}


class UpdateRunRequest(BaseModel):
    region_code: str


class ManualRouteStopPayload(BaseModel):
    stop_sequence: int = Field(..., ge=1)
    station_name_raw: str
    station_code_rzd: str | None = None
    station_id: int | None = None
    arrival_time: str | None = None
    departure_time: str | None = None
    stop_duration_minutes: int | None = Field(default=None, ge=0)
    distance_km: float | None = Field(default=None, ge=0)


class ManualRouteCreateRequest(BaseModel):
    source_system: str = "manual"
    external_route_id: str | None = None
    train_number: str | None = None
    route_name: str | None = None
    origin_station_name: str | None = None
    destination_station_name: str | None = None
    origin_station_code: str | None = None
    destination_station_code: str | None = None
    snapshot_date: date | None = None
    operates_from: date | None = None
    operates_to: date | None = None
    is_active: bool = True
    notes: str | None = None
    stops: list[ManualRouteStopPayload]


class RzdRouteSearchRequest(BaseModel):
    origin_code: str
    destination_code: str
    dep_date: date
    check_seats: bool = False
    include_transfers: bool = False


class RzdRouteCalendarSearchRequest(BaseModel):
    origin_code: str
    destination_code: str
    start_date: date | None = None
    days_ahead: int = Field(default=14, ge=1, le=30)
    check_seats: bool = False


class RzdRouteCalendarByStationsRequest(BaseModel):
    origin_station_id: int
    destination_station_id: int
    start_date: date | None = None
    days_ahead: int = Field(default=5, ge=1, le=30)
    check_seats: bool = False
    nearby_radius_km: float = Field(default=5.0, ge=0.5, le=15.0)
    nearby_station_limit: int = Field(default=5, ge=1, le=10)
    max_code_pair_attempts: int = Field(default=10, ge=1, le=30)


class TopologyBuildRequest(BaseModel):
    region_codes: list[str]
    force_rebuild: bool = False


class VirtualRoutePathRequest(BaseModel):
    origin_station_id: int
    destination_station_id: int
    scope_region_codes: list[str] | None = None


class RzdTrainImportRequest(BaseModel):
    train_number: str
    dep_date: date
    origin_code: str | None = None
    destination_code: str | None = None
    origin_station_name: str | None = None
    destination_station_name: str | None = None
    route_name: str | None = None
    notes: str | None = None


def ensure_route_build_jobs_available() -> None:
    if ROUTE_BUILD_JOBS_AVAILABLE:
        return

    raise HTTPException(
        status_code=503,
        detail=(
            "Route build jobs module is not available yet. "
            "Add back/app/route_build_jobs.py and restart backend."
        ),
    )


def ensure_rzd_route_service_available() -> None:
    if RZD_ROUTE_SERVICE_AVAILABLE:
        return

    raise HTTPException(
        status_code=503,
        detail=(
            "RZD route service module is not available yet. "
            "Add back/app/rzd_route_service.py and restart backend."
        ),
    )


def parse_bbox_param(bbox: str) -> dict:
    parts = [part.strip() for part in bbox.split(",")]
    if len(parts) != 4:
        raise HTTPException(
            status_code=400,
            detail="Invalid bbox format. Expected: min_lon,min_lat,max_lon,max_lat",
        )

    try:
        min_lon, min_lat, max_lon, max_lat = map(float, parts)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid bbox values. All bbox coordinates must be numeric",
        ) from exc

    if min_lon >= max_lon or min_lat >= max_lat:
        raise HTTPException(
            status_code=400,
            detail="Invalid bbox bounds. Expected min values to be less than max values",
        )

    if min_lon < -180 or max_lon > 180:
        raise HTTPException(
            status_code=400,
            detail="Invalid bbox longitude range",
        )

    if min_lat < -90 or max_lat > 90:
        raise HTTPException(
            status_code=400,
            detail="Invalid bbox latitude range",
        )

    return {
        "min_lon": min_lon,
        "min_lat": min_lat,
        "max_lon": max_lon,
        "max_lat": max_lat,
    }


def apply_bbox_filter(where_parts: list[str], params: dict, bbox: str | None) -> bool:
    if not bbox:
        return False

    bbox_params = parse_bbox_param(bbox)
    params.update(bbox_params)

    envelope_sql = "ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)"

    where_parts.append(f"geom && {envelope_sql}")
    where_parts.append(f"ST_Intersects(geom, {envelope_sql})")

    return True


def apply_region_filter(
    where_parts: list[str],
    params: dict,
    region_code: str | None,
    region_codes: str | None,
) -> list[str]:
    codes: list[str] = []

    if region_codes:
        codes = [item.strip() for item in region_codes.split(",") if item.strip()]
    elif region_code:
        codes = [region_code.strip()]

    unique_codes: list[str] = []
    seen = set()

    for code in codes:
        if code not in seen:
            unique_codes.append(code)
            seen.add(code)

    if not unique_codes:
        return []

    placeholders = []
    for index, code in enumerate(unique_codes):
        param_name = f"region_code_{index}"
        params[param_name] = code
        placeholders.append(f":{param_name}")

    if len(placeholders) == 1:
        where_parts.append(f"region_code = {placeholders[0]}")
    else:
        where_parts.append(f"region_code IN ({', '.join(placeholders)})")

    return unique_codes


def build_station_rzd_profile(row: dict[str, Any]) -> dict[str, Any]:
    has_esr = bool((row.get("esr_user") or "").strip())
    has_uic = bool((row.get("uic_ref") or "").strip())
    has_any_code = has_esr or has_uic
    is_main = bool(row.get("is_main_rail_station"))
    is_visible = bool(row.get("is_visible_default"))
    has_graph_link = bool(row.get("has_graph_link"))

    score = 0
    if is_main:
        score += 100
    if has_esr:
        score += 50
    if has_uic:
        score += 35
    if has_graph_link:
        score += 25
    if is_visible:
        score += 10

    code_candidates = []

    if has_esr:
        code_candidates.append(
            {
                "source": "esr_user",
                "code": row.get("esr_user"),
                "priority": 1,
            }
        )

    if has_uic:
        code_candidates.append(
            {
                "source": "uic_ref",
                "code": row.get("uic_ref"),
                "priority": 2,
            }
        )

    return {
        "station_id": row["id"],
        "name": row.get("name"),
        "region_code": row.get("region_code"),
        "lon": row.get("lon"),
        "lat": row.get("lat"),
        "is_main_rail_station": is_main,
        "is_visible_default": is_visible,
        "has_graph_link": has_graph_link,
        "has_rzd_code_candidate": has_any_code,
        "rzd_search_priority_score": score,
        "recommended_for_rzd_search": bool(is_visible and is_main and has_any_code),
        "code_candidates": code_candidates,
    }


def get_latest_successful_md5(region_code: str) -> str | None:
    query = text("""
        SELECT source_md5
        FROM dataset_runs
        WHERE region_code = :region_code
          AND status = 'finished'
          AND source_md5 IS NOT NULL
          AND source_md5 <> ''
        ORDER BY finished_at DESC NULLS LAST, id DESC
        LIMIT 1;
    """)

    with engine.connect() as connection:
        row = connection.execute(query, {"region_code": region_code}).first()

    if row is None:
        return None

    return row._mapping["source_md5"]


def has_running_update(region_code: str) -> bool:
    query = text("""
        SELECT COUNT(*)
        FROM dataset_runs
        WHERE region_code = :region_code
          AND status = 'running';
    """)

    with engine.connect() as connection:
        count = connection.execute(query, {"region_code": region_code}).scalar_one()

    return count > 0


def try_fetch_remote_md5(region_code: str) -> tuple[str | None, str | None]:
    if region_code not in MD5_URL_MAP:
        return None, f"Unknown region_code: {region_code}"

    md5_url = MD5_URL_MAP[region_code]

    try:
        response = requests.get(
            md5_url,
            timeout=60,
            headers={
                "User-Agent": "railway-gis-diploma/1.0",
            },
        )
        response.raise_for_status()

        content = response.text.strip()
        if not content:
            return None, "remote md5 response is empty"

        remote_md5 = content.split()[0].strip()
        if not remote_md5:
            return None, "remote md5 is empty after parsing"

        return remote_md5, None
    except Exception as exc:
        return None, str(exc)


def build_region_update_check(region_code: str) -> dict:
    region = REGION_META_BY_CODE.get(region_code)
    if not region:
        raise HTTPException(status_code=400, detail=f"Unknown region_code: {region_code}")

    latest_successful_md5 = get_latest_successful_md5(region_code)
    running = has_running_update(region_code)
    remote_md5, remote_error = try_fetch_remote_md5(region_code)

    if running:
        return {
            "region_code": region_code,
            "region_label": region["label"],
            "status": "running",
            "message": "Обновление уже выполняется",
            "remote_md5": remote_md5,
            "latest_successful_md5": latest_successful_md5,
            "can_update": False,
        }

    if remote_error:
        return {
            "region_code": region_code,
            "region_label": region["label"],
            "status": "check_failed",
            "message": "Не удалось проверить наличие обновлений",
            "remote_md5": None,
            "latest_successful_md5": latest_successful_md5,
            "error": remote_error,
            "can_update": False,
        }

    if latest_successful_md5 and latest_successful_md5 == remote_md5:
        return {
            "region_code": region_code,
            "region_label": region["label"],
            "status": "up_to_date",
            "message": "Обновление не требуется",
            "remote_md5": remote_md5,
            "latest_successful_md5": latest_successful_md5,
            "can_update": False,
        }

    return {
        "region_code": region_code,
        "region_label": region["label"],
        "status": "update_available",
        "message": "Найдены обновления",
        "remote_md5": remote_md5,
        "latest_successful_md5": latest_successful_md5,
        "can_update": True,
    }


def start_region_update_process(region_code: str) -> None:
    script_path = SCRIPTS_DIR / "run_region_pipeline.py"
    cmd = [sys.executable, str(script_path), region_code]

    popen_kwargs = {
        "cwd": str(BASE_DIR),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if sys.platform.startswith("win"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    subprocess.Popen(cmd, **popen_kwargs)


def start_selected_regions_update_process(region_codes: list[str]) -> None:
    script_path = SCRIPTS_DIR / "run_selected_regions_pipeline.py"
    cmd = [sys.executable, str(script_path), *region_codes]

    popen_kwargs = {
        "cwd": str(BASE_DIR),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if sys.platform.startswith("win"):
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    subprocess.Popen(cmd, **popen_kwargs)


def build_route_geometry_from_stops(stops: list[dict]) -> dict | None:
    coordinates = []

    for stop in stops:
        lon = stop.get("lon")
        lat = stop.get("lat")
        if lon is None or lat is None:
            continue
        coordinates.append([float(lon), float(lat)])

    if len(coordinates) < 2:
        return None

    return {
        "type": "LineString",
        "coordinates": coordinates,
    }


@app.get("/")
def root():
    return {"message": "Backend is running"}


@app.get("/api/health")
def healthcheck():
    try:
        test_connection()
        return {"status": "ok", "database": "connected"}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Database connection failed: {exc}",
        )


@app.get("/api/regions/summary")
def get_regions_summary():
    station_counts_query = text("""
        SELECT
            region_code,
            COUNT(*) AS stations_count
        FROM stations
        WHERE is_visible_default = TRUE
        GROUP BY region_code;
    """)

    line_counts_query = text("""
        SELECT
            region_code,
            COUNT(*) AS lines_count
        FROM rail_lines
        WHERE is_visible_default = TRUE
        GROUP BY region_code;
    """)

    with engine.connect() as connection:
        station_rows = connection.execute(station_counts_query).fetchall()
        line_rows = connection.execute(line_counts_query).fetchall()

    station_counts = {
        row._mapping["region_code"]: int(row._mapping["stations_count"])
        for row in station_rows
    }
    line_counts = {
        row._mapping["region_code"]: int(row._mapping["lines_count"])
        for row in line_rows
    }

    items = []
    for region in REGION_META:
        code = region["code"]
        items.append(
            {
                "code": code,
                "label": region["label"],
                "stations_count": station_counts.get(code, 0),
                "lines_count": line_counts.get(code, 0),
            }
        )

    return {"items": items, "total": len(items)}


@app.get("/api/dataset-runs")
def get_dataset_runs(
    limit: int = Query(default=50, ge=1, le=500),
    region_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
):
    where_parts = []
    params: dict = {"limit": limit}

    if region_code:
        where_parts.append("region_code = :region_code")
        params["region_code"] = region_code

    if status:
        where_parts.append("status = :status")
        params["status"] = status

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    query = text(f"""
        SELECT
            id,
            region_code,
            region_label,
            source_url,
            source_md5,
            started_at,
            finished_at,
            status,
            stations_raw_count,
            lines_raw_count,
            stations_core_count,
            lines_core_count,
            notes
        FROM dataset_runs
        {where_sql}
        ORDER BY started_at DESC, id DESC
        LIMIT :limit;
    """)

    count_query = text(f"""
        SELECT COUNT(*)
        FROM dataset_runs
        {where_sql};
    """)

    with engine.connect() as connection:
        rows = connection.execute(query, params).fetchall()
        total = connection.execute(count_query, params).scalar_one()

    items = [dict(row._mapping) for row in rows]
    return {"items": items, "total": total}


@app.get("/api/dataset-runs/latest")
def get_latest_dataset_run(
    region_code: str | None = Query(default=None),
):
    where_sql = ""
    params: dict = {}

    if region_code:
        where_sql = "WHERE region_code = :region_code"
        params["region_code"] = region_code

    query = text(f"""
        SELECT
            id,
            region_code,
            region_label,
            source_url,
            source_md5,
            started_at,
            finished_at,
            status,
            stations_raw_count,
            lines_raw_count,
            stations_core_count,
            lines_core_count,
            notes
        FROM dataset_runs
        {where_sql}
        ORDER BY started_at DESC, id DESC
        LIMIT 1;
    """)

    with engine.connect() as connection:
        row = connection.execute(query, params).first()

    if row is None:
        return {"item": None}

    return {"item": dict(row._mapping)}


@app.get("/api/updates/check")
def check_region_updates(
    region_code: str = Query(...),
):
    item = build_region_update_check(region_code)
    return {"item": item}


@app.get("/api/updates/check-all")
def check_all_updates():
    items = []

    for region in REGION_META:
        try:
            items.append(build_region_update_check(region["code"]))
        except Exception as exc:
            items.append(
                {
                    "region_code": region["code"],
                    "region_label": region["label"],
                    "status": "check_failed",
                    "message": "Не удалось проверить наличие обновлений",
                    "remote_md5": None,
                    "latest_successful_md5": None,
                    "error": str(exc),
                    "can_update": False,
                }
            )

    summary = {
        "update_available": sum(1 for item in items if item["status"] == "update_available"),
        "up_to_date": sum(1 for item in items if item["status"] == "up_to_date"),
        "check_failed": sum(1 for item in items if item["status"] == "check_failed"),
        "running": sum(1 for item in items if item["status"] == "running"),
    }

    return {
        "items": items,
        "total": len(items),
        "summary": summary,
    }


@app.post("/api/updates/run")
def run_region_update(payload: UpdateRunRequest):
    region_code = payload.region_code.strip()

    if region_code not in REGION_META_BY_CODE:
        raise HTTPException(status_code=400, detail=f"Unknown region_code: {region_code}")

    check_result = build_region_update_check(region_code)

    if check_result["status"] == "running":
        return {
            "status": "already_running",
            "message": "Обновление уже выполняется",
            "item": check_result,
        }

    if check_result["status"] == "check_failed":
        return {
            "status": "check_failed",
            "message": "Не удалось проверить наличие обновлений",
            "item": check_result,
        }

    if check_result["status"] == "up_to_date":
        return {
            "status": "not_required",
            "message": "Обновление не требуется",
            "item": check_result,
        }

    start_region_update_process(region_code)

    return {
        "status": "started",
        "message": "Обновление запущено",
        "region_code": region_code,
    }


@app.post("/api/updates/run-all-available")
def run_all_available_updates():
    items = [build_region_update_check(region["code"]) for region in REGION_META]

    regions_to_start = []
    already_running = []
    check_failed = []
    up_to_date = []

    for item in items:
        if item["status"] == "update_available":
            regions_to_start.append(item["region_code"])
        elif item["status"] == "running":
            already_running.append(item["region_code"])
        elif item["status"] == "check_failed":
            check_failed.append(item["region_code"])
        elif item["status"] == "up_to_date":
            up_to_date.append(item["region_code"])

    if not regions_to_start:
        return {
            "status": "nothing_to_start",
            "message": "Округов с доступными обновлениями не найдено",
            "regions_started": [],
            "already_running": already_running,
            "check_failed": check_failed,
            "up_to_date": up_to_date,
        }

    start_selected_regions_update_process(regions_to_start)

    return {
        "status": "started",
        "message": "Обновление доступных округов запущено",
        "regions_started": regions_to_start,
        "already_running": already_running,
        "check_failed": check_failed,
        "up_to_date": up_to_date,
    }


@app.get("/api/routes")
def get_routes(
    limit: int = Query(default=100, ge=1, le=1000),
    q: str | None = Query(default=None),
    active_only: bool = Query(default=True),
):
    where_parts = []
    params: dict = {"limit": limit}

    if active_only:
        where_parts.append("r.is_active = TRUE")

    if q:
        where_parts.append("""
            (
                r.route_name ILIKE :pattern
                OR COALESCE(r.train_number, '') ILIKE :pattern
                OR COALESCE(r.origin_station_name, '') ILIKE :pattern
                OR COALESCE(r.destination_station_name, '') ILIKE :pattern
            )
        """)
        params["pattern"] = f"%{q}%"

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    query = text(f"""
        SELECT
            r.id,
            r.source_system,
            r.external_route_id,
            r.train_number,
            r.route_name,
            r.origin_station_name,
            r.destination_station_name,
            r.origin_station_code,
            r.destination_station_code,
            r.snapshot_date,
            r.operates_from,
            r.operates_to,
            r.is_active,
            r.notes,
            r.created_at,
            r.updated_at,
            COUNT(rs.id) AS stops_count,
            COUNT(
                CASE
                    WHEN rs.station_id IS NOT NULL THEN 1
                    ELSE NULL
                END
            ) AS matched_stops_count
        FROM routes r
        LEFT JOIN route_stops rs ON rs.route_id = r.id
        {where_sql}
        GROUP BY r.id
        ORDER BY
            COALESCE(r.train_number, ''),
            r.route_name,
            r.id
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(query, params).fetchall()

    items = []
    for row in rows:
        item = dict(row._mapping)
        item["unresolved_stops_count"] = max(
            0,
            int(item.get("stops_count") or 0) - int(item.get("matched_stops_count") or 0),
        )
        items.append(item)

    return {"items": items, "total": len(items)}


@app.post("/api/routes/{route_id}/build")
def build_route_job(route_id: int):
    ensure_route_build_jobs_available()

    try:
        job = create_route_build_job(route_id)
        start_route_build_job(job["id"])

        return {
            "job_id": job["id"],
            "route_id": job["route_id"],
            "status": job["status"],
            "progress_percent": job["progress_percent"],
            "stage_code": job["stage_code"],
            "stage_label": job["stage_label"],
        }
    except ValueError:
        raise HTTPException(status_code=404, detail="Route not found")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось запустить построение маршрута: {exc}",
        )


@app.get("/api/route-jobs/{job_id}")
def get_route_job(job_id: str):
    ensure_route_build_jobs_available()

    job = get_route_build_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Route build job not found")

    return {
        "job_id": job["id"],
        "route_id": job["route_id"],
        "status": job["status"],
        "progress_percent": job["progress_percent"],
        "stage_code": job["stage_code"],
        "stage_label": job["stage_label"],
        "detail": job.get("detail"),
        "error_text": job.get("error_text"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


@app.get("/api/route-jobs/{job_id}/result")
def get_route_job_result(job_id: str):
    ensure_route_build_jobs_available()

    payload = get_route_build_job_result(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Route build job not found")

    if payload["status"] == "failed":
        raise HTTPException(
            status_code=500,
            detail=payload.get("error_text") or "Route build failed",
        )

    if payload["status"] != "done":
        raise HTTPException(
            status_code=409,
            detail="Route build is not finished yet",
        )

    return payload


@app.get("/api/topology/status")
def get_topology_status_endpoint(
    region_codes: str = Query(...),
):
    item = get_topology_status(region_codes)

    return {
        "item": item,
    }


@app.post("/api/topology/build")
def build_topology_endpoint(payload: TopologyBuildRequest):
    region_codes = [code.strip() for code in payload.region_codes if code.strip()]

    if not region_codes:
        raise HTTPException(status_code=400, detail="region_codes is required")

    try:
        job = start_topology_build_job(
            region_codes=region_codes,
            force_rebuild=payload.force_rebuild,
        )

        return {
            "job_id": job["id"],
            "status": job["status"],
            "progress_percent": job["progress_percent"],
            "stage_code": job["stage_code"],
            "stage_label": job["stage_label"],
            "region_codes": job["region_codes"],
            "scope_key": job["scope_key"],
        }

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось запустить построение topology graph: {exc}",
        ) from exc


@app.get("/api/topology/jobs/{job_id}")
def get_topology_job_endpoint(job_id: str):
    job = get_topology_build_job(job_id)

    if job is None:
        raise HTTPException(status_code=404, detail="Topology build job not found")

    return {
        "job_id": job["id"],
        "status": job["status"],
        "progress_percent": job["progress_percent"],
        "stage_code": job["stage_code"],
        "stage_label": job["stage_label"],
        "error_text": job.get("error_text"),
        "region_codes": job.get("region_codes"),
        "scope_key": job.get("scope_key"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "result": job.get("result"),
    }


@app.post("/api/virtual-routes/path")
def build_virtual_route_path_endpoint(payload: VirtualRoutePathRequest):
    started = time.perf_counter()

    try:
        print(
            "Virtual route request:",
            {
                "origin_station_id": payload.origin_station_id,
                "destination_station_id": payload.destination_station_id,
                "scope_region_codes": payload.scope_region_codes,
            },
        )

        result = build_virtual_route_path(
            origin_station_id=payload.origin_station_id,
            destination_station_id=payload.destination_station_id,
            scope_region_codes=payload.scope_region_codes,
        )

        print(
            "Virtual route result:",
            {
                "status": result.get("status"),
                "geometry_ready": result.get("geometry") is not None,
                "network_segments_count": len(result.get("network_segments") or []),
                "duration_seconds": round(time.perf_counter() - started, 3),
                "summary": result.get("summary"),
            },
        )

        return result

    except ValueError as exc:
        print(
            "Virtual route failed:",
            {
                "error": str(exc),
                "duration_seconds": round(time.perf_counter() - started, 3),
            },
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    except Exception as exc:
        print(
            "Virtual route failed:",
            {
                "error": repr(exc),
                "duration_seconds": round(time.perf_counter() - started, 3),
            },
        )
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось построить виртуальный OSM-маршрут: {exc}",
        ) from exc


@app.get("/api/routes/{route_id}")
def get_route_by_id(route_id: int):
    started = time.perf_counter()

    try:
        print(
            "Route map request:",
            {
                "route_id": route_id,
            },
        )

        topology_started = time.perf_counter()

        if ensure_route_topology_built is not None:
            ensure_route_topology_built(route_id)

        topology_duration = round(time.perf_counter() - topology_started, 3)

        matcher_started = time.perf_counter()
        result = resolve_route_for_map(route_id)
        matcher_duration = round(time.perf_counter() - matcher_started, 3)

        summary = result.get("summary") or {}

        print(
            "Route map result:",
            {
                "route_id": route_id,
                "duration_seconds": round(time.perf_counter() - started, 3),
                "topology_duration_seconds": topology_duration,
                "matcher_duration_seconds": matcher_duration,
                "geometry_source": result.get("geometry_source"),
                "geometry_ready": result.get("geometry") is not None,
                "network_segments_count": len(result.get("network_segments") or []),
                "fallback_mode_used": summary.get("fallback_mode_used"),
                "fallback_mode_reason": summary.get("fallback_mode_reason"),
                "locked_segments_with_graph_path": summary.get("locked_segments_with_graph_path"),
                "locked_segments_with_fallback": summary.get("locked_segments_with_fallback"),
            },
        )

        return result

    except ValueError:
        print(
            "Route map failed:",
            {
                "route_id": route_id,
                "status": 404,
                "duration_seconds": round(time.perf_counter() - started, 3),
            },
        )
        raise HTTPException(status_code=404, detail="Route not found")

    except Exception as exc:
        print(
            "Route map failed:",
            {
                "route_id": route_id,
                "error": repr(exc),
                "duration_seconds": round(time.perf_counter() - started, 3),
            },
        )
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось построить маршрут на карте: {exc}",
        )

@app.get("/api/routes/{route_id}/geometry")
def get_route_geometry(route_id: int):
    stops_query = text("""
        SELECT
            rs.stop_sequence,
            ST_X(s.geom) AS lon,
            ST_Y(s.geom) AS lat
        FROM route_stops rs
        JOIN stations s ON s.id = rs.station_id
        WHERE rs.route_id = :route_id
        ORDER BY rs.stop_sequence;
    """)

    route_exists_query = text("""
        SELECT id
        FROM routes
        WHERE id = :route_id
        LIMIT 1;
    """)

    with engine.connect() as connection:
        route_exists = connection.execute(
            route_exists_query,
            {"route_id": route_id},
        ).scalar_one_or_none()

        if route_exists is None:
            raise HTTPException(status_code=404, detail="Route not found")

        stop_rows = connection.execute(stops_query, {"route_id": route_id}).fetchall()

    stops = [dict(row._mapping) for row in stop_rows]
    geometry = build_route_geometry_from_stops(stops)

    return {
        "route_id": route_id,
        "geometry": geometry,
        "matched_points_count": len(stops),
    }


@app.post("/api/routes/manual")
def create_manual_route(payload: ManualRouteCreateRequest):
    try:
        result = import_route_payload(
            payload.model_dump(),
            source_name="manual_api",
            requested_scope="single_manual_route",
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/rzd/stations/{station_id}/resolve-code")
def resolve_rzd_station_code_endpoint(station_id: int):
    ensure_rzd_route_service_available()

    try:
        result = resolve_rzd_code_for_station(station_id)

        return {
            "item": {
                "station_id": result["station"]["id"],
                "station_name": result["station"].get("name"),
                "region_code": result["station"].get("region_code"),
                "recommended_code": result.get("recommended_code"),
                "recommended_source": result.get("recommended_source"),
                "recommended_label": result.get("recommended_label"),
                "confidence": result.get("confidence"),
                "candidates": result.get("candidates") or [],
                "diagnostics": result.get("diagnostics") or [],
            }
        }

    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        print("RZD station code resolve failed:", repr(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось подобрать код РЖД для станции: {exc}",
        ) from exc


@app.get("/api/rzd/stations/{station_id}/profile")
def get_rzd_station_profile(station_id: int):
    query = text("""
        SELECT
            s.id,
            s.region_code,
            s.name,
            s.uic_ref,
            s.esr_user,
            s.is_main_rail_station,
            s.is_visible_default,
            ST_X(s.geom) AS lon,
            ST_Y(s.geom) AS lat,
            EXISTS (
                SELECT 1
                FROM station_graph_links l
                WHERE l.station_id = s.id
                LIMIT 1
            ) AS has_graph_link
        FROM stations s
        WHERE s.id = :station_id
        LIMIT 1;
    """)

    with engine.connect() as connection:
        row = connection.execute(query, {"station_id": station_id}).first()

    if row is None:
        raise HTTPException(status_code=404, detail="Station not found")

    item = build_station_rzd_profile(dict(row._mapping))
    return {"item": item}


@app.get("/api/rzd/stations/nearby")
def get_nearby_rzd_candidate_stations(
    lon: float = Query(..., ge=-180, le=180),
    lat: float = Query(..., ge=-90, le=90),
    radius_km: float = Query(default=25.0, ge=0.1, le=200.0),
    limit: int = Query(default=10, ge=1, le=50),
):
    query = text("""
        WITH point_input AS (
            SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) AS geom
        ),
        station_base AS (
            SELECT
                s.id,
                s.region_code,
                s.name,
                s.uic_ref,
                s.esr_user,
                s.is_main_rail_station,
                s.is_visible_default,
                ST_X(s.geom) AS lon,
                ST_Y(s.geom) AS lat,
                ST_Distance(s.geom::geography, p.geom::geography) AS distance_m,
                EXISTS (
                    SELECT 1
                    FROM station_graph_links l
                    WHERE l.station_id = s.id
                    LIMIT 1
                ) AS has_graph_link
            FROM stations s
            CROSS JOIN point_input p
            WHERE
                s.geom IS NOT NULL
                AND s.is_visible_default = TRUE
                AND ST_DWithin(
                    s.geom::geography,
                    p.geom::geography,
                    :radius_m
                )
        )
        SELECT
            *,
            (
                CASE WHEN is_main_rail_station THEN 100 ELSE 0 END
                + CASE WHEN esr_user IS NOT NULL AND esr_user <> '' THEN 50 ELSE 0 END
                + CASE WHEN uic_ref IS NOT NULL AND uic_ref <> '' THEN 35 ELSE 0 END
                + CASE WHEN has_graph_link THEN 25 ELSE 0 END
                + CASE WHEN is_visible_default THEN 10 ELSE 0 END
                - LEAST(distance_m / 1000.0, 50.0)
            ) AS priority_score
        FROM station_base
        ORDER BY
            priority_score DESC,
            is_main_rail_station DESC,
            distance_m ASC,
            name NULLS LAST,
            id
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        rows = connection.execute(
            query,
            {
                "lon": lon,
                "lat": lat,
                "radius_m": radius_km * 1000.0,
                "limit": limit,
            },
        ).fetchall()

    items = []

    for row in rows:
        raw = dict(row._mapping)
        item = build_station_rzd_profile(raw)
        item["distance_m"] = round(float(raw["distance_m"]), 1)
        item["distance_km"] = round(float(raw["distance_m"]) / 1000.0, 3)
        item["priority_score"] = round(float(raw["priority_score"]), 3)
        items.append(item)

    return {"items": items, "total": len(items)}


@app.get("/api/rzd/station-codes/search")
def search_rzd_station_codes_endpoint(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
):
    ensure_rzd_route_service_available()

    try:
        items = search_rzd_station_codes(q, limit=limit)
        return {"items": items, "total": len(items)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось выполнить поиск станции через РЖД API: {exc}",
        ) from exc


@app.post("/api/rzd/routes/search")
def search_rzd_routes_endpoint(payload: RzdRouteSearchRequest):
    ensure_rzd_route_service_available()

    origin_code = payload.origin_code.strip()
    destination_code = payload.destination_code.strip()

    if not origin_code:
        raise HTTPException(status_code=400, detail="origin_code is required")

    if not destination_code:
        raise HTTPException(status_code=400, detail="destination_code is required")

    try:
        result = search_rzd_routes(
            origin_code=origin_code,
            destination_code=destination_code,
            dep_date=payload.dep_date,
            check_seats=payload.check_seats,
            include_transfers=payload.include_transfers,
        )

        return result

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        print("RZD route search failed:", repr(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось найти маршруты через РЖД API: {exc}",
        ) from exc


@app.post("/api/rzd/routes/search-calendar")
def search_rzd_routes_calendar_endpoint(payload: RzdRouteCalendarSearchRequest):
    ensure_rzd_route_service_available()

    origin_code = payload.origin_code.strip()
    destination_code = payload.destination_code.strip()

    if not origin_code:
        raise HTTPException(status_code=400, detail="origin_code is required")

    if not destination_code:
        raise HTTPException(status_code=400, detail="destination_code is required")

    try:
        print(
            "RZD calendar search request:",
            {
                "origin_code": origin_code,
                "destination_code": destination_code,
                "start_date": payload.start_date,
                "days_ahead": payload.days_ahead,
                "check_seats": payload.check_seats,
            },
        )

        result = search_rzd_routes_calendar(
            origin_code=origin_code,
            destination_code=destination_code,
            start_date=payload.start_date,
            days_ahead=payload.days_ahead,
            check_seats=payload.check_seats,
            include_transfers=False,
        )

        print(
            "RZD calendar search result:",
            {
                "origin_code": origin_code,
                "destination_code": destination_code,
                "total": result.get("total"),
                "dates_checked": result.get("dates_checked"),
                "dates_with_trains": result.get("dates_with_trains"),
                "errors_count": len(result.get("errors") or []),
                "first_errors": (result.get("errors") or [])[:3],
            },
        )

        return result

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        print("RZD calendar route search failed:", repr(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось выполнить календарный поиск через РЖД API: {exc}",
        ) from exc


@app.post("/api/rzd/routes/search-calendar-by-stations")
def search_rzd_routes_calendar_by_stations_endpoint(
    payload: RzdRouteCalendarByStationsRequest,
):
    ensure_rzd_route_service_available()

    try:
        print(
            "RZD calendar by stations request:",
            {
                "origin_station_id": payload.origin_station_id,
                "destination_station_id": payload.destination_station_id,
                "start_date": payload.start_date,
                "days_ahead": payload.days_ahead,
                "check_seats": payload.check_seats,
                "nearby_radius_km": payload.nearby_radius_km,
                "nearby_station_limit": payload.nearby_station_limit,
                "max_code_pair_attempts": payload.max_code_pair_attempts,
            },
        )

        result = search_rzd_routes_calendar_by_stations(
            origin_station_id=payload.origin_station_id,
            destination_station_id=payload.destination_station_id,
            start_date=payload.start_date,
            days_ahead=payload.days_ahead,
            check_seats=payload.check_seats,
            nearby_radius_km=payload.nearby_radius_km,
            nearby_station_limit=payload.nearby_station_limit,
            max_code_pair_attempts=payload.max_code_pair_attempts,
        )

        print(
            "RZD calendar by stations result:",
            {
                "status": result.get("status"),
                "total": result.get("total"),
                "dates_checked": result.get("dates_checked"),
                "dates_with_trains": result.get("dates_with_trains"),
                "code_attempts_count": len(result.get("code_attempts") or []),
                "used_origin_code": result.get("used_origin_code"),
                "used_destination_code": result.get("used_destination_code"),
            },
        )

        return result

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        print("RZD calendar by stations failed:", repr(exc))
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось выполнить поиск поездов по выбранным станциям: {exc}",
        ) from exc


@app.post("/api/rzd/trains/import")
def import_rzd_train_endpoint(payload: RzdTrainImportRequest):
    ensure_rzd_route_service_available()

    train_number = payload.train_number.strip()

    if not train_number:
        raise HTTPException(status_code=400, detail="train_number is required")

    try:
        return import_selected_rzd_train(
            train_number=train_number,
            dep_date=payload.dep_date,
            origin_code=payload.origin_code,
            destination_code=payload.destination_code,
            origin_station_name=payload.origin_station_name,
            destination_station_name=payload.destination_station_name,
            route_name=payload.route_name,
            notes=payload.notes,
        )

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось импортировать поезд из РЖД API: {exc}",
        ) from exc


@app.get("/api/stations/{station_id}/routes")
def get_routes_for_station(
    station_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
):
    station_query = text("""
        SELECT
            id,
            uic_ref
        FROM stations
        WHERE id = :station_id;
    """)

    routes_query = text("""
        SELECT
            r.id,
            r.source_system,
            r.external_route_id,
            r.train_number,
            r.route_name,
            r.origin_station_name,
            r.destination_station_name,
            r.snapshot_date,
            r.operates_from,
            r.operates_to,
            r.is_active,
            r.notes,
            rs.stop_sequence,
            rs.arrival_time,
            rs.departure_time,
            rs.distance_km,
            rs.match_method,
            rs.match_confidence
        FROM route_stops rs
        JOIN routes r ON r.id = rs.route_id
        WHERE
            rs.station_id = :station_id
            OR (
                :station_uic_ref IS NOT NULL
                AND rs.station_code_rzd = :station_uic_ref
            )
        ORDER BY
            COALESCE(r.train_number, ''),
            r.route_name,
            rs.stop_sequence
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        station_row = connection.execute(
            station_query,
            {"station_id": station_id},
        ).first()

        if station_row is None:
            raise HTTPException(status_code=404, detail="Station not found")

        station_uic_ref = station_row._mapping["uic_ref"]

        rows = connection.execute(
            routes_query,
            {
                "station_id": station_id,
                "station_uic_ref": station_uic_ref,
                "limit": limit,
            },
        ).fetchall()

    items = [dict(row._mapping) for row in rows]
    return {"items": items, "total": len(items)}


@app.get("/api/stations/{station_id}/nearby-routes")
def get_nearby_routes_for_station(
    station_id: int,
    radius_km: float = Query(default=5.0, ge=0.5, le=15.0),
    limit: int = Query(default=40, ge=1, le=200),
):
    ensure_rzd_route_service_available()

    try:
        items = load_routes_for_station_zone(
            station_id,
            radius_km=radius_km,
            limit=limit,
        )

        return {
            "station_id": station_id,
            "radius_km": radius_km,
            "items": items,
            "total": len(items),
        }

    except ValueError:
        raise HTTPException(status_code=404, detail="Station not found")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось загрузить маршруты из зоны станции: {exc}",
        )


@app.get("/api/stations")
def get_stations(
    limit: int = Query(default=20000, ge=1, le=100000),
    include_hidden: bool = Query(default=False),
    region_code: str | None = Query(default=None),
    region_codes: str | None = Query(default=None),
    bbox: str | None = Query(default=None),
):
    where_parts = []
    params: dict = {"limit": limit}

    if not include_hidden:
        where_parts.append("is_visible_default = TRUE")

    apply_region_filter(
        where_parts=where_parts,
        params=params,
        region_code=region_code,
        region_codes=region_codes,
    )

    bbox_applied = apply_bbox_filter(where_parts, params, bbox)

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    query = text(f"""
        SELECT
            id,
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
            ST_X(geom) AS lon,
            ST_Y(geom) AS lat
        FROM stations
        {where_sql}
        ORDER BY name NULLS LAST, id
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        result = connection.execute(query, params)
        items = [dict(row._mapping) for row in result]

        if bbox_applied:
            total = len(items)
        else:
            count_query = text(f"""
                SELECT COUNT(*)
                FROM stations
                {where_sql};
            """)
            total = connection.execute(count_query, params).scalar_one()

    return {"items": items, "total": total}


@app.get("/api/stations/{station_id}")
def get_station_by_id(
    station_id: int,
    include_hidden: bool = Query(default=False),
):
    where_parts = ["id = :station_id"]
    params = {"station_id": station_id}

    if not include_hidden:
        where_parts.append("is_visible_default = TRUE")

    where_sql = "WHERE " + " AND ".join(where_parts)

    query = text(f"""
        SELECT
            id,
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
            ST_X(geom) AS lon,
            ST_Y(geom) AS lat
        FROM stations
        {where_sql};
    """)

    with engine.connect() as connection:
        row = connection.execute(query, params).first()

    if row is None:
        raise HTTPException(status_code=404, detail="Station not found")

    return dict(row._mapping)


@app.get("/api/search/stations")
def search_stations(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=1000, ge=1, le=5000),
    include_hidden: bool = Query(default=False),
    region_code: str | None = Query(default=None),
    region_codes: str | None = Query(default=None),
):
    where_parts = [
        "name IS NOT NULL",
        "name ILIKE :pattern",
    ]
    params = {
        "pattern": f"%{q}%",
        "limit": limit,
    }

    if not include_hidden:
        where_parts.append("is_visible_default = TRUE")

    apply_region_filter(
        where_parts=where_parts,
        params=params,
        region_code=region_code,
        region_codes=region_codes,
    )

    where_sql = "WHERE " + " AND ".join(where_parts)

    query = text(f"""
        SELECT
            id,
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
            ST_X(geom) AS lon,
            ST_Y(geom) AS lat
        FROM stations
        {where_sql}
        ORDER BY name, id
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        result = connection.execute(query, params)
        items = [dict(row._mapping) for row in result]

    return {"items": items, "total": len(items)}


@app.get("/api/lines")
def get_lines(
    limit: int = Query(default=100000, ge=1, le=400000),
    include_hidden: bool = Query(default=False),
    include_service: bool = Query(default=False),
    region_code: str | None = Query(default=None),
    region_codes: str | None = Query(default=None),
    bbox: str | None = Query(default=None),
):
    where_parts = []
    params: dict = {"limit": limit}

    if not include_hidden:
        if include_service:
            where_parts.append("(is_visible_default = TRUE OR is_service_line = TRUE)")
        else:
            where_parts.append("is_visible_default = TRUE")

    apply_region_filter(
        where_parts=where_parts,
        params=params,
        region_code=region_code,
        region_codes=region_codes,
    )

    bbox_applied = apply_bbox_filter(where_parts, params, bbox)

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    query = text(f"""
        SELECT
            id,
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
            ST_AsGeoJSON(geom) AS geometry
        FROM rail_lines
        {where_sql}
        ORDER BY id
        LIMIT :limit;
    """)

    with engine.connect() as connection:
        result = connection.execute(query, params)
        items = [dict(row._mapping) for row in result]

        if bbox_applied:
            total = len(items)
        else:
            count_query = text(f"""
                SELECT COUNT(*)
                FROM rail_lines
                {where_sql};
            """)
            total = connection.execute(count_query, params).scalar_one()

    return {"items": items, "total": total}
