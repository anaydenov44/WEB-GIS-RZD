import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class RzdClientError(RuntimeError):
    pass


class RzdClient:
    """
    Диагностический клиент для неофициального RZD API.

    Ничего не пишет в БД.
    Используется только для:
    - поиска station codes,
    - поиска поездов между станциями,
    - получения списка остановок поезда,
    - сохранения raw-ответов для анализа.
    """

    STATION_SUGGESTER_URL = "https://pass.rzd.ru/suggester"
    ROUTES_URL = "https://pass.rzd.ru/timetable/public/"
    STATION_LIST_URL = "http://pass.rzd.ru/timetable/public/ru"
    BASIC_ROUTE_URL = "https://pass.rzd.ru/ticket/services/route/basicRoute"

    def __init__(
        self,
        connect_timeout: int = 10,
        read_timeout: int = 90,
        poll_timeout_seconds: int = 60,
        poll_interval_seconds: float = 1.5,
        retries_total: int = 3,
        retries_backoff_factor: float = 1.2,
    ) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/123.0 Safari/537.36"
                ),
                "Referer": "https://ticket.rzd.ru/",
                "Origin": "https://ticket.rzd.ru",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Connection": "keep-alive",
            }
        )

        retry = Retry(
            total=retries_total,
            connect=retries_total,
            read=retries_total,
            status=retries_total,
            backoff_factor=retries_backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )

        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        self._warmup_done = False

    def _timeout(self) -> tuple[int, int]:
        return (self.connect_timeout, self.read_timeout)

    def _warmup_session(self) -> None:
        if self._warmup_done:
            return

        warmup_urls = [
            "https://ticket.rzd.ru/",
            "https://pass.rzd.ru/",
        ]

        for url in warmup_urls:
            try:
                response = self.session.get(
                    url,
                    timeout=self._timeout(),
                    allow_redirects=True,
                )
                response.raise_for_status()
            except Exception:
                pass

        self._warmup_done = True

    def _parse_xml_response(self, text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped:
            return None

        if not stripped.startswith("<") and not stripped.startswith("<?xml"):
            return None

        try:
            root = ET.fromstring(stripped)
        except ET.ParseError:
            return None

        result: dict[str, Any] = {
            "_xml_root_tag": root.tag,
        }

        children = list(root)
        if not children:
            result["value"] = root.text.strip() if root.text else ""
            return result

        for child in children:
            result[child.tag] = child.text.strip() if child.text else ""

        return result

    def _get_payload(self, url: str, params: dict[str, Any]) -> Any:
        self._warmup_session()

        filtered_params = {
            key: value
            for key, value in params.items()
            if value is not None
        }

        response = self.session.get(
            url,
            params=filtered_params,
            timeout=self._timeout(),
        )
        response.raise_for_status()

        text_body = response.text or ""

        xml_payload = self._parse_xml_response(text_body)
        if xml_payload is not None:
            return xml_payload

        try:
            return response.json()
        except Exception as exc:
            preview = text_body[:500]
            raise RzdClientError(
                f"Не удалось разобрать ответ ни как JSON, ни как XML. "
                f"URL={response.url}, status={response.status_code}, response_preview={preview}"
            ) from exc

    def _wait_for_rid_result(
        self,
        url: str,
        rid: str | int,
        *,
        layer_id: int,
        extra_params: dict[str, Any] | None = None,
    ) -> Any:
        started = time.time()
        last_payload = None

        while time.time() - started < self.poll_timeout_seconds:
            params = {
                "layer_id": layer_id,
                "rid": rid,
            }
            if extra_params:
                params.update(extra_params)

            payload = self._get_payload(url, params)
            last_payload = payload

            # 1. Если это уже готовый station-list payload — сразу выходим
            if layer_id == 5804:
                stations = self._extract_station_list_items(payload)
                if stations:
                    return payload

            # 2. Если это уже готовый routes payload — сразу выходим
            if layer_id == 5827:
                trains = self._extract_trains_from_routes_payload(payload)
                if trains:
                    return payload

            if isinstance(payload, dict):
                payload_type = str(payload.get("type", "")).upper()
                payload_result = str(payload.get("result", "")).upper()

                if payload_type == "REQUEST_ID":
                    time.sleep(self.poll_interval_seconds)
                    continue

                if payload_result == "OK":
                    return payload

                if payload_type in {"OK", "SUCCESS"}:
                    return payload

                if payload_result in {"FAIL", "ERROR"} or payload_type in {"FAIL", "ERROR"}:
                    raise RzdClientError(f"RZD вернул ошибку при polling RID: {payload}")

            elif isinstance(payload, list):
                return payload

            time.sleep(self.poll_interval_seconds)

        raise RzdClientError(
            f"Истёк таймаут ожидания RID={rid}. Последний payload={last_payload}"
        )

    def search_station_codes(
        self,
        station_name_part: str,
        *,
        lang: str = "ru",
        compact_mode: bool = True,
    ) -> dict[str, Any]:
        station_name_part = station_name_part.strip()
        if len(station_name_part) < 2:
            raise ValueError("station_name_part должен содержать минимум 2 символа")

        payload = self._get_payload(
            self.STATION_SUGGESTER_URL,
            {
                "stationNamePart": station_name_part,
                "lang": lang,
                "compactMode": "y" if compact_mode else "n",
            },
        )

        items: list[dict[str, Any]] = []

        if isinstance(payload, list):
            for row in payload:
                if not isinstance(row, dict):
                    continue

                name = row.get("station")
                code = row.get("code")

                if name is None and "n" in row:
                    name = row.get("n")
                if code is None and "c" in row:
                    code = row.get("c")

                items.append(
                    {
                        "name": name,
                        "code": str(code) if code is not None else None,
                        "raw": row,
                    }
                )

        return {
            "query": station_name_part,
            "count": len(items),
            "items": items,
            "raw": payload,
        }

    def search_routes(
        self,
        *,
        code0: str,
        code1: str,
        dep_date: str,
        dir_value: int = 0,
        tfl: int = 3,
        check_seats: bool = False,
        include_transfers: bool = False,
    ) -> dict[str, Any]:
        first_payload = self._get_payload(
            self.ROUTES_URL,
            {
                "layer_id": 5827,
                "dir": dir_value,
                "code0": code0,
                "code1": code1,
                "tfl": tfl,
                "checkSeats": 1 if check_seats else 0,
                "withoutSeats": "y" if not check_seats else None,
                "dt0": dep_date,
                "dt1": dep_date,
                "md": 1 if include_transfers else 0,
            },
        )

        if not isinstance(first_payload, dict):
            raise RzdClientError(f"Неожиданный ответ первого запроса маршрутов: {first_payload}")

        rid = first_payload.get("RID") or first_payload.get("rid")
        result = str(first_payload.get("result", "")).upper()
        payload_type = str(first_payload.get("type", "")).upper()

        if rid and (result == "RID" or payload_type == "REQUEST_ID"):
            final_payload = self._wait_for_rid_result(
                self.ROUTES_URL,
                rid=rid,
                layer_id=5827,
            )
        elif result == "OK":
            final_payload = first_payload
        else:
            trains = self._extract_trains_from_routes_payload(first_payload)
            if trains:
                final_payload = first_payload
            else:
                raise RzdClientError(f"RZD не выдал RID/OK для поиска маршрутов: {first_payload}")

        trains = self._extract_trains_from_routes_payload(final_payload)

        return {
            "request": {
                "code0": code0,
                "code1": code1,
                "dep_date": dep_date,
                "dir": dir_value,
                "tfl": tfl,
                "check_seats": check_seats,
                "include_transfers": include_transfers,
            },
            "count": len(trains),
            "items": trains,
            "raw": final_payload,
        }

    def _extract_trains_from_routes_payload(self, payload: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        def append_train(row: dict[str, Any]) -> None:
            number = (
                row.get("number")
                or row.get("trainNumber")
                or row.get("tnum0")
                or row.get("TRAIN_NUM")
                or row.get("Number")
            )
            if number is None:
                return

            route0 = row.get("route0")
            route1 = row.get("route1")

            if route0 is None and isinstance(row.get("Route"), dict):
                route0 = row["Route"].get("From")
                route1 = row["Route"].get("To")

            result.append(
                {
                    "number": number,
                    "brand": row.get("brand") or row.get("Brand"),
                    "carrier": row.get("carrier") or row.get("Carrier"),
                    "date0": row.get("date0"),
                    "date1": row.get("date1"),
                    "time0": row.get("time0"),
                    "time1": row.get("time1"),
                    "route0": route0,
                    "route1": route1,
                    "timeInWay": row.get("timeInWay"),
                    "raw": row,
                }
            )

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if any(key in node for key in ["number", "trainNumber", "tnum0", "TRAIN_NUM", "Number"]):
                    append_train(node)

                for value in node.values():
                    walk(value)

            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)

        dedup: list[dict[str, Any]] = []
        seen = set()

        for item in result:
            key = (
                str(item.get("number")),
                str(item.get("date0")),
                str(item.get("time0")),
                str(item.get("route0")),
                str(item.get("route1")),
            )
            if key in seen:
                continue
            seen.add(key)
            dedup.append(item)

        return dedup

    def get_train_station_list(
        self,
        *,
        train_number: str,
        dep_date: str,
    ) -> dict[str, Any]:
        try:
            basic_payload = self._get_payload(
                self.BASIC_ROUTE_URL,
                {
                    "STRUCTURE_ID": 704,
                    "trainNumber": train_number,
                    "depDate": dep_date,
                },
            )

            stations = self._extract_station_list_items(basic_payload)
            if stations:
                return {
                    "request": {
                        "train_number": train_number,
                        "dep_date": dep_date,
                        "source_mode": "basicRoute",
                    },
                    "count": len(stations),
                    "items": stations,
                    "raw": basic_payload,
                }
        except Exception:
            pass

        first_payload = self._get_payload(
            self.STATION_LIST_URL,
            {
                "layer_id": 5804,
                "train_num": train_number,
                "date": dep_date,
                "json": "y",
                "format": "array",
            },
        )

        rid = None
        if isinstance(first_payload, dict):
            rid = first_payload.get("rid") or first_payload.get("RID")
            payload_type = str(first_payload.get("type", "")).upper()
            payload_result = str(first_payload.get("result", "")).upper()

            stations = self._extract_station_list_items(first_payload)
            if stations:
                return {
                    "request": {
                        "train_number": train_number,
                        "dep_date": dep_date,
                        "source_mode": "layer_5804_direct",
                    },
                    "count": len(stations),
                    "items": stations,
                    "raw": first_payload,
                }

            if not rid and payload_type == "REQUEST_ID":
                rid = first_payload.get("rid") or first_payload.get("RID")

            if not rid and payload_result == "OK":
                return {
                    "request": {
                        "train_number": train_number,
                        "dep_date": dep_date,
                        "source_mode": "layer_5804_ok",
                    },
                    "count": len(stations),
                    "items": stations,
                    "raw": first_payload,
                }

        if not rid:
            raise RzdClientError(
                f"Не удалось получить RID для списка остановок поезда. payload={first_payload}"
            )

        final_payload = self._wait_for_rid_result(
            self.STATION_LIST_URL,
            rid=rid,
            layer_id=5804,
            extra_params={
                "json": "y",
                "format": "array",
                "train_num": train_number,
                "date": dep_date,
            },
        )

        stations = self._extract_station_list_items(final_payload)

        return {
            "request": {
                "train_number": train_number,
                "dep_date": dep_date,
                "source_mode": "layer_5804",
            },
            "count": len(stations),
            "items": stations,
            "raw": final_payload,
        }

    def _extract_station_list_items(self, payload: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        def append_station(row: dict[str, Any]) -> None:
            station_name = row.get("Station") or row.get("station") or row.get("name")
            station_code = row.get("Code") or row.get("code")

            if station_name is None and station_code is None:
                return

            result.append(
                {
                    "station_name": station_name,
                    "station_code": str(station_code) if station_code is not None else None,
                    "arrival_time": row.get("ArvTime") or row.get("arrival_time"),
                    "departure_time": row.get("DepTime") or row.get("departure_time"),
                    "waiting_time": row.get("WaitingTime") or row.get("waiting_time"),
                    "distance": row.get("Distance") or row.get("distance"),
                    "raw": row,
                }
            )

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if any(key in node for key in ["Station", "station", "Code", "code"]):
                    append_station(node)

                for value in node.values():
                    walk(value)

            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(payload)

        dedup: list[dict[str, Any]] = []
        seen = set()

        for item in result:
            key = (
                str(item.get("station_name")),
                str(item.get("station_code")),
                str(item.get("arrival_time")),
                str(item.get("departure_time")),
            )
            if key in seen:
                continue
            seen.add(key)
            dedup.append(item)

        return dedup

    @staticmethod
    def save_payload_to_file(payload: Any, output_dir: Path, prefix: str) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = output_dir / f"{prefix}_{timestamp}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path