import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pipeline_utils import (
    get_region_meta,
    get_region_sources,
    get_source_md5_path,
    get_source_pbf_path,
    md5sum,
)


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.headers.update({"User-Agent": "railway-gis-diploma/1.0"})
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def download_file(session: requests.Session, url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)

    with session.get(url, stream=True, timeout=300) as response:
        response.raise_for_status()
        with target.open("wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def read_cached_md5(md5_path: Path) -> str | None:
    if not md5_path.exists():
        return None

    content = md5_path.read_text(encoding="utf-8").strip()
    if not content:
        return None

    return content.split()[0].strip() or None


def download_one_source(
    session: requests.Session,
    *,
    region_code: str,
    source: dict,
) -> dict:
    source_key = source["source_key"]
    pbf_path = get_source_pbf_path(region_code, source_key)
    md5_path = get_source_md5_path(region_code, source_key)

    expected_md5 = None
    md5_download_error = None

    print(f"[{region_code}:{source_key}] Скачиваю md5...")
    try:
        download_file(session, source["md5_url"], md5_path)
        expected_md5 = read_cached_md5(md5_path)
        if not expected_md5:
            raise RuntimeError("Не удалось прочитать скачанный md5 файл")
    except Exception as exc:
        md5_download_error = str(exc)
        expected_md5 = read_cached_md5(md5_path)
        if expected_md5 is None:
            raise RuntimeError(
                f"[{region_code}:{source_key}] Не удалось получить md5 ни удалённо, ни из локального кэша. "
                f"Причина: {md5_download_error}"
            ) from exc

    if md5_download_error:
        print(f"[{region_code}:{source_key}] Remote md5 недоступен, использую локальный cached md5.")
        print(f"[{region_code}:{source_key}] Причина remote md5 error: {md5_download_error}")

    if pbf_path.exists():
        actual_md5 = md5sum(pbf_path)
        if actual_md5 == expected_md5:
            print(f"[{region_code}:{source_key}] PBF уже актуален: {pbf_path}")
            return {
                "source_key": source_key,
                "pbf_path": str(pbf_path),
                "md5_path": str(md5_path),
                "expected_md5": expected_md5,
                "actual_md5": actual_md5,
                "downloaded": False,
            }

    if md5_download_error and not pbf_path.exists():
        raise RuntimeError(
            f"[{region_code}:{source_key}] Remote md5 недоступен и локальный PBF отсутствует. "
            f"Безопасное обновление невозможно. Причина: {md5_download_error}"
        )

    print(f"[{region_code}:{source_key}] Скачиваю PBF...")
    download_file(session, source["url"], pbf_path)

    actual_md5 = md5sum(pbf_path)
    print(f"[{region_code}:{source_key}] expected md5 = {expected_md5}")
    print(f"[{region_code}:{source_key}] actual md5   = {actual_md5}")

    if actual_md5 != expected_md5:
        raise RuntimeError(f"[{region_code}:{source_key}] MD5 не совпал")

    print(f"[{region_code}:{source_key}] Готово: {pbf_path}")
    return {
        "source_key": source_key,
        "pbf_path": str(pbf_path),
        "md5_path": str(md5_path),
        "expected_md5": expected_md5,
        "actual_md5": actual_md5,
        "downloaded": True,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python download_region_extract.py <region_code>")

    region_code = sys.argv[1]
    meta = get_region_meta(region_code)
    sources = get_region_sources(region_code)

    print(f"[{region_code}] Регион: {meta['label']}")
    print(f"[{region_code}] Источников для скачивания: {len(sources)}")

    session = build_session()
    results = []

    for source in sources:
        result = download_one_source(
            session,
            region_code=region_code,
            source=source,
        )
        results.append(result)

    print(f"[{region_code}] Загрузка завершена. Источники:")
    for item in results:
        print(
            f" - {item['source_key']}: downloaded={item['downloaded']}, "
            f"expected_md5={item['expected_md5']}, path={item['pbf_path']}"
        )


if __name__ == "__main__":
    main()