import json
import subprocess
import sys

import requests

from pipeline_utils import (
    BASE_DIR,
    build_aggregate_md5,
    cleanup_region_files,
    get_db_connection,
    get_region_meta,
    get_region_sources,
    get_source_md5_path,
)

# При запуске скрипта как `python scripts/run_region_pipeline.py`
# Python кладёт в sys.path папку scripts, а не корень backend.
# Добавляем BASE_DIR, чтобы были доступны импорты вида `from app...`.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.station_cleanup_service import apply_city_station_cleanup


def run_step(script_name: str, region_code: str):
    script_path = BASE_DIR / "scripts" / script_name
    cmd = [sys.executable, str(script_path), region_code]
    subprocess.run(cmd, check=True)


def try_fetch_remote_md5(region_code: str) -> tuple[str | None, dict[str, str | None], str | None]:
    sources = get_region_sources(region_code)
    remote_md5_by_source: dict[str, str | None] = {}
    errors: list[str] = []

    for source in sources:
        source_key = source["source_key"]
        try:
            response = requests.get(
                source["md5_url"],
                timeout=60,
                headers={"User-Agent": "railway-gis-diploma/1.0"},
            )
            response.raise_for_status()

            content = response.text.strip()
            if not content:
                raise RuntimeError("remote md5 response is empty")

            remote_md5 = content.split()[0].strip()
            if not remote_md5:
                raise RuntimeError("remote md5 is empty after parsing")

            remote_md5_by_source[source_key] = remote_md5

        except Exception as exc:
            remote_md5_by_source[source_key] = None
            errors.append(f"{source_key}: {exc}")

    aggregate_md5 = build_aggregate_md5(remote_md5_by_source)

    if errors:
        return aggregate_md5, remote_md5_by_source, "; ".join(errors)

    return aggregate_md5, remote_md5_by_source, None


def read_local_md5_file(region_code: str, source_key: str) -> str | None:
    md5_path = get_source_md5_path(region_code, source_key)
    if not md5_path.exists():
        return None

    content = md5_path.read_text(encoding="utf-8").strip()
    if not content:
        return None

    return content.split()[0].strip() or None


def read_local_md5_files(region_code: str) -> tuple[str | None, dict[str, str | None]]:
    local_md5_by_source: dict[str, str | None] = {}

    for source in get_region_sources(region_code):
        source_key = source["source_key"]
        local_md5_by_source[source_key] = read_local_md5_file(region_code, source_key)

    aggregate_md5 = build_aggregate_md5(local_md5_by_source)
    return aggregate_md5, local_md5_by_source


def build_source_url_payload(region_code: str) -> str:
    payload = {
        "region_code": region_code,
        "sources": [
            {
                "source_key": source["source_key"],
                "label": source.get("label"),
                "url": source["url"],
                "md5_url": source["md5_url"],
            }
            for source in get_region_sources(region_code)
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def create_dataset_run(region_code: str, region_label: str, source_url: str, source_md5: str | None) -> int:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dataset_runs (
                    region_code,
                    region_label,
                    source_url,
                    source_md5,
                    status
                )
                VALUES (%s, %s, %s, %s, 'running')
                RETURNING id;
                """,
                (region_code, region_label, source_url, source_md5),
            )
            run_id = cur.fetchone()[0]
        conn.commit()
        return run_id
    finally:
        conn.close()


def update_dataset_run_source_md5(run_id: int, source_md5: str | None):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dataset_runs
                SET source_md5 = %s
                WHERE id = %s;
                """,
                (source_md5, run_id),
            )
        conn.commit()
    finally:
        conn.close()


def finish_dataset_run(run_id: int, status: str, notes: str | None = None):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dataset_runs
                SET
                    finished_at = NOW(),
                    status = %s,
                    notes = CASE
                        WHEN %s IS NULL THEN notes
                        WHEN notes IS NULL OR notes = '' THEN %s
                        ELSE notes || E'\n' || %s
                    END
                WHERE id = %s;
                """,
                (status, notes, notes, notes, run_id),
            )
        conn.commit()
    finally:
        conn.close()


def update_dataset_run_counts(run_id: int, region_code: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM osm_stations_raw WHERE region_code = %s;", (region_code,))
            stations_raw_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM osm_rail_lines_raw WHERE region_code = %s;", (region_code,))
            lines_raw_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM stations WHERE region_code = %s;", (region_code,))
            stations_core_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM rail_lines WHERE region_code = %s;", (region_code,))
            lines_core_count = cur.fetchone()[0]

            cur.execute(
                """
                UPDATE dataset_runs
                SET
                    stations_raw_count = %s,
                    lines_raw_count = %s,
                    stations_core_count = %s,
                    lines_core_count = %s
                WHERE id = %s;
                """,
                (
                    stations_raw_count,
                    lines_raw_count,
                    stations_core_count,
                    lines_core_count,
                    run_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def append_dataset_run_note(run_id: int, extra_note: str):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE dataset_runs
                SET notes = CASE
                    WHEN notes IS NULL OR notes = '' THEN %s
                    ELSE notes || E'\n' || %s
                END
                WHERE id = %s;
                """,
                (extra_note, extra_note, run_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_latest_successful_md5(region_code: str) -> str | None:
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source_md5
                FROM dataset_runs
                WHERE region_code = %s
                  AND status = 'finished'
                  AND source_md5 IS NOT NULL
                  AND source_md5 <> ''
                ORDER BY finished_at DESC NULLS LAST, id DESC
                LIMIT 1;
                """,
                (region_code,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return row[0]
    finally:
        conn.close()


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Использование: python run_region_pipeline.py <region_code>")

    region_code = sys.argv[1]
    meta = get_region_meta(region_code)

    print(f"[{region_code}] Проверяю remote md5 по всем источникам...")
    remote_aggregate_md5, remote_md5_by_source, remote_md5_error = try_fetch_remote_md5(region_code)
    latest_successful_md5 = get_latest_successful_md5(region_code)
    local_cached_aggregate_md5, local_md5_by_source = read_local_md5_files(region_code)

    initial_source_md5 = remote_aggregate_md5 or local_cached_aggregate_md5 or latest_successful_md5

    run_id = create_dataset_run(
        region_code=meta["code"],
        region_label=meta["label"],
        source_url=build_source_url_payload(region_code),
        source_md5=initial_source_md5,
    )

    try:
        if remote_aggregate_md5 and latest_successful_md5 and latest_successful_md5 == remote_aggregate_md5:
            note = (
                "Обновление пропущено: aggregated remote md5 совпадает с последним успешным запуском.\n"
                f"remote_md5_by_source={json.dumps(remote_md5_by_source, ensure_ascii=False, sort_keys=True)}\n"
                f"remote_aggregate_md5={remote_aggregate_md5}"
            )
            finish_dataset_run(run_id, "skipped", note)
            print(f"[{region_code}] Пропуск обновления: данные уже актуальны.")
            return

        if (
            remote_aggregate_md5 is None
            and remote_md5_error
            and local_cached_aggregate_md5
            and latest_successful_md5
            and local_cached_aggregate_md5 == latest_successful_md5
        ):
            note = (
                "Обновление пропущено: remote md5 недоступен, использован локальный cached aggregated md5, "
                "совпадающий с последним успешным запуском. Свежесть удалённого источника не подтверждена.\n"
                f"local_md5_by_source={json.dumps(local_md5_by_source, ensure_ascii=False, sort_keys=True)}\n"
                f"cached_aggregate_md5={local_cached_aggregate_md5}\n"
                f"remote_md5_error={remote_md5_error}"
            )
            finish_dataset_run(run_id, "skipped", note)
            print(
                f"[{region_code}] Пропуск обновления: remote md5 недоступен, "
                f"но локальный кэш совпадает с последним успешным запуском."
            )
            return

        if remote_md5_error:
            append_dataset_run_note(
                run_id,
                "Precheck remote md5 не сработал по части источников или полностью, "
                "запускаю обычный pipeline без раннего skip.\n"
                f"remote_md5_by_source={json.dumps(remote_md5_by_source, ensure_ascii=False, sort_keys=True)}\n"
                f"Причина: {remote_md5_error}",
            )
            print(f"[{region_code}] Remote md5 precheck частично/полностью недоступен, продолжаю pipeline...")
        else:
            print(f"[{region_code}] Remote md5 по всем источникам получен, требуется обновление.")

        print(f"[{region_code}] Шаг 1/4: download")
        run_step("download_region_extract.py", region_code)

        local_md5_after_download, local_md5_by_source_after_download = read_local_md5_files(region_code)
        update_dataset_run_source_md5(
            run_id,
            local_md5_after_download or remote_aggregate_md5 or initial_source_md5,
        )

        append_dataset_run_note(
            run_id,
            "MD5 после download:\n"
            + json.dumps(local_md5_by_source_after_download, ensure_ascii=False, sort_keys=True),
        )

        print(f"[{region_code}] Шаг 2/4: import to staging")
        run_step("import_region_raw_pbf.py", region_code)

        print(f"[{region_code}] Шаг 3/4: publish staging -> raw")
        run_step("publish_region_stage.py", region_code)

        print(f"[{region_code}] Шаг 4/4: normalize raw -> core")
        run_step("normalize_region_core.py", region_code)

        cleanup_result = apply_city_station_cleanup(region_code)
        print("Station cleanup result:", cleanup_result)

        if cleanup_result:
            append_dataset_run_note(
                run_id,
                "Station cleanup result:\n" + "\n".join(str(item) for item in cleanup_result),
            )

        update_dataset_run_counts(run_id, region_code)

        deleted_files = cleanup_region_files(region_code)
        if deleted_files:
            append_dataset_run_note(
                run_id,
                "Удалены файлы после успешного pipeline:\n" + "\n".join(deleted_files),
            )
            print(f"[{region_code}] Удалены временные файлы:")
            for item in deleted_files:
                print(f" - {item}")
        else:
            print(f"[{region_code}] Временные файлы не удалялись.")

        finish_dataset_run(run_id, "finished")
        print(f"[{region_code}] Pipeline завершён успешно.")

    except Exception as exc:
        finish_dataset_run(
            run_id,
            "failed",
            f"Ошибка pipeline: {exc}\nStage-данные этого region_code могут остаться для диагностики.",
        )
        raise


if __name__ == "__main__":
    main()