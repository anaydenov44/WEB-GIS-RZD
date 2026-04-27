import sys
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from pipeline_utils import get_md5_path, get_pbf_path, get_region_meta, md5sum


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
    session.headers.update(
        {
            "User-Agent": "railway-gis-diploma/1.0",
        }
    )
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def download_file(session: requests.Session, url: str, target: Path) -> None:
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

    return content.split()[0].strip()


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python download_region_extract.py <region_code>")

    region_code = sys.argv[1]
    meta = get_region_meta(region_code)

    pbf_path = get_pbf_path(region_code)
    md5_path = get_md5_path(region_code)

    session = build_session()

    expected_md5 = None
    md5_download_error = None

    print(f"[{region_code}] Скачиваю md5...")
    try:
        download_file(session, meta["md5_url"], md5_path)
        expected_md5 = read_cached_md5(md5_path)
        if not expected_md5:
            raise RuntimeError("Не удалось прочитать скачанный md5 файл")
    except Exception as exc:
        md5_download_error = str(exc)
        expected_md5 = read_cached_md5(md5_path)

    if expected_md5 is None:
        raise RuntimeError(
            f"[{region_code}] Не удалось получить md5 ни удалённо, ни из локального кэша. "
            f"Причина: {md5_download_error}"
        )

    if md5_download_error:
        print(f"[{region_code}] Remote md5 недоступен, использую локальный cached md5.")
        print(f"[{region_code}] Причина remote md5 error: {md5_download_error}")

    if pbf_path.exists():
        actual_md5 = md5sum(pbf_path)
        if actual_md5 == expected_md5:
            print(f"[{region_code}] PBF уже актуален: {pbf_path}")
            return

    if md5_download_error and not pbf_path.exists():
        raise RuntimeError(
            f"[{region_code}] Remote md5 недоступен и локальный PBF отсутствует. "
            f"Безопасное обновление невозможно. Причина: {md5_download_error}"
        )

    print(f"[{region_code}] Скачиваю PBF...")
    download_file(session, meta["url"], pbf_path)

    actual_md5 = md5sum(pbf_path)
    print(f"[{region_code}] expected md5 = {expected_md5}")
    print(f"[{region_code}] actual   md5 = {actual_md5}")

    if actual_md5 != expected_md5:
        raise RuntimeError(f"[{region_code}] MD5 не совпал")

    print(f"[{region_code}] Готово: {pbf_path}")


if __name__ == "__main__":
    main()