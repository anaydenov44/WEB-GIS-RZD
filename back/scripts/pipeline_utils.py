import hashlib
import json
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "federal_districts.json"

load_dotenv(BASE_DIR / ".env")


def load_regions_registry() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_region_meta(region_code: str) -> dict:
    registry = load_regions_registry()
    for region in registry["regions"]:
        if region["code"] == region_code:
            return region
    raise ValueError(f"Неизвестный region_code: {region_code}")


def get_osm_data_dir() -> Path:
    root = Path(os.getenv("OSM_DATA_DIR", "C:/osm_data"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_region_data_dir(region_code: str) -> Path:
    path = get_osm_data_dir() / region_code
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_pbf_path(region_code: str) -> Path:
    meta = get_region_meta(region_code)
    return get_region_data_dir(region_code) / Path(meta["url"]).name


def get_md5_path(region_code: str) -> Path:
    meta = get_region_meta(region_code)
    return get_region_data_dir(region_code) / Path(meta["md5_url"]).name


def md5sum(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "railway_gis"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
    )


def delete_pbf_after_success() -> bool:
    return os.getenv("DELETE_PBF_AFTER_SUCCESS", "true").strip().lower() == "true"


def delete_md5_after_success() -> bool:
    return os.getenv("DELETE_MD5_AFTER_SUCCESS", "false").strip().lower() == "true"


def cleanup_region_files(region_code: str) -> list[str]:
    deleted: list[str] = []

    pbf_path = get_pbf_path(region_code)
    md5_path = get_md5_path(region_code)

    if delete_pbf_after_success() and pbf_path.exists():
        pbf_path.unlink()
        deleted.append(str(pbf_path))

    if delete_md5_after_success() and md5_path.exists():
        md5_path.unlink()
        deleted.append(str(md5_path))

    return deleted