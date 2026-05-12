import hashlib
import json
import os
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "federal_districts.json"

load_dotenv(BASE_DIR / ".env")


def load_regions_registry() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def get_region_meta(region_code: str) -> dict[str, Any]:
    registry = load_regions_registry()
    for region in registry["regions"]:
        if region["code"] == region_code:
            return region
    raise ValueError(f"Неизвестный region_code: {region_code}")


def _normalize_source(region_meta: dict[str, Any], source: dict[str, Any] | None = None) -> dict[str, Any]:
    if source is None:
        if "url" not in region_meta or "md5_url" not in region_meta:
            raise ValueError(
                f"Для region_code={region_meta.get('code')} не найден ни sources, ни legacy url/md5_url"
            )
        return {
            "source_key": "main",
            "label": region_meta.get("label"),
            "url": region_meta["url"],
            "md5_url": region_meta["md5_url"],
        }

    source_key = source.get("source_key")
    if not source_key:
        raise ValueError(f"У источника региона {region_meta.get('code')} отсутствует source_key")

    if not source.get("url") or not source.get("md5_url"):
        raise ValueError(
            f"У источника {source_key} региона {region_meta.get('code')} отсутствует url или md5_url"
        )

    return {
        "source_key": str(source_key),
        "label": source.get("label") or source_key,
        "url": source["url"],
        "md5_url": source["md5_url"],
    }


def get_region_sources(region_code: str) -> list[dict[str, Any]]:
    meta = get_region_meta(region_code)

    sources = meta.get("sources")
    if isinstance(sources, list) and sources:
        return [_normalize_source(meta, source) for source in sources]

    return [_normalize_source(meta, None)]


def get_source_meta(region_code: str, source_key: str) -> dict[str, Any]:
    for source in get_region_sources(region_code):
        if source["source_key"] == source_key:
            return source
    raise ValueError(f"Неизвестный source_key={source_key} для region_code={region_code}")


def get_osm_data_dir() -> Path:
    root = Path(os.getenv("OSM_DATA_DIR", "C:/osm_data"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_region_data_dir(region_code: str) -> Path:
    path = get_osm_data_dir() / region_code
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_source_data_dir(region_code: str, source_key: str) -> Path:
    path = get_region_data_dir(region_code) / source_key
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_source_pbf_path(region_code: str, source_key: str) -> Path:
    source = get_source_meta(region_code, source_key)
    return get_source_data_dir(region_code, source_key) / Path(source["url"]).name


def get_source_md5_path(region_code: str, source_key: str) -> Path:
    source = get_source_meta(region_code, source_key)
    return get_source_data_dir(region_code, source_key) / Path(source["md5_url"]).name


def get_pbf_path(region_code: str) -> Path:
    """
    Legacy helper: возвращает путь первого источника.
    Нужен для обратной совместимости со старыми скриптами.
    """
    first_source = get_region_sources(region_code)[0]
    return get_source_pbf_path(region_code, first_source["source_key"])


def get_md5_path(region_code: str) -> Path:
    """
    Legacy helper: возвращает путь первого источника.
    Нужен для обратной совместимости со старыми скриптами.
    """
    first_source = get_region_sources(region_code)[0]
    return get_source_md5_path(region_code, first_source["source_key"])


def md5sum(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_aggregate_md5(md5_by_source_key: dict[str, str | None]) -> str | None:
    normalized = {
        str(source_key): str(md5_value).strip()
        for source_key, md5_value in sorted(md5_by_source_key.items())
        if md5_value is not None and str(md5_value).strip()
    }
    if not normalized:
        return None

    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


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

    for source in get_region_sources(region_code):
        source_key = source["source_key"]
        pbf_path = get_source_pbf_path(region_code, source_key)
        md5_path = get_source_md5_path(region_code, source_key)

        if delete_pbf_after_success() and pbf_path.exists():
            pbf_path.unlink()
            deleted.append(str(pbf_path))

        if delete_md5_after_success() and md5_path.exists():
            md5_path.unlink()
            deleted.append(str(md5_path))

    return deleted