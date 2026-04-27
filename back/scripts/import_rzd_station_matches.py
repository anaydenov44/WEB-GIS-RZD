import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import text

from app.db import engine


def main():
    parser = argparse.ArgumentParser(
        description="Пакетный импорт соответствий RZD station code -> stations.id"
    )
    parser.add_argument("json_path", help="Путь к JSON-файлу со списком соответствий")
    args = parser.parse_args()

    json_path = Path(args.json_path).resolve()
    if not json_path.exists():
        raise FileNotFoundError(f"Не найден файл: {json_path}")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else [payload]

    if not items:
        raise ValueError("JSON пустой")

    imported = 0

    with engine.begin() as connection:
        for item in items:
            station_code_rzd = str(item["station_code_rzd"]).strip()
            station_name_rzd = item.get("station_name_rzd")
            station_id = int(item["station_id"])
            match_method = item.get("match_method") or "manual_confirmed"
            match_confidence = float(item.get("match_confidence", 1.00))
            notes = item.get("notes")
            is_active = bool(item.get("is_active", True))

            station_exists = connection.execute(
                text("""
                    SELECT id
                    FROM stations
                    WHERE id = :station_id;
                """),
                {"station_id": station_id},
            ).scalar_one_or_none()

            if station_exists is None:
                raise ValueError(f"station_id={station_id} не найден в таблице stations")

            connection.execute(
                text("""
                    INSERT INTO rzd_station_matches (
                        station_code_rzd,
                        station_name_rzd,
                        station_id,
                        match_method,
                        match_confidence,
                        notes,
                        is_active,
                        updated_at
                    )
                    VALUES (
                        :station_code_rzd,
                        :station_name_rzd,
                        :station_id,
                        :match_method,
                        :match_confidence,
                        :notes,
                        :is_active,
                        NOW()
                    )
                    ON CONFLICT (station_code_rzd)
                    DO UPDATE SET
                        station_name_rzd = EXCLUDED.station_name_rzd,
                        station_id = EXCLUDED.station_id,
                        match_method = EXCLUDED.match_method,
                        match_confidence = EXCLUDED.match_confidence,
                        notes = EXCLUDED.notes,
                        is_active = EXCLUDED.is_active,
                        updated_at = NOW();
                """),
                {
                    "station_code_rzd": station_code_rzd,
                    "station_name_rzd": station_name_rzd,
                    "station_id": station_id,
                    "match_method": match_method,
                    "match_confidence": match_confidence,
                    "notes": notes,
                    "is_active": is_active,
                },
            )
            imported += 1

    print("Готово.")
    print(f"Импортировано/обновлено соответствий: {imported}")


if __name__ == "__main__":
    main()