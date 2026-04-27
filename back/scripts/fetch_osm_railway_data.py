import json
import time
from pathlib import Path
from typing import Any

import requests

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]

OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

SAMARA_AREA_QUERY = """
area["name"="Самарская область"]["boundary"="administrative"]->.searchArea;
"""

STATIONS_QUERY = f"""
[out:json][timeout:180];
{SAMARA_AREA_QUERY}
(
  node(area.searchArea)["railway"="station"];
  way(area.searchArea)["railway"="station"];
  relation(area.searchArea)["railway"="station"];

  node(area.searchArea)["railway"="halt"];
  way(area.searchArea)["railway"="halt"];
  relation(area.searchArea)["railway"="halt"];
);
out geom;
"""

LINES_QUERY = f"""
[out:json][timeout:180];
{SAMARA_AREA_QUERY}
(
  way(area.searchArea)["railway"="rail"];
);
out geom;
"""

HEADERS = {
    "User-Agent": "railway-gis-diploma/0.1 (local-development)"
}


def run_overpass_query(query: str, label: str) -> dict[str, Any]:
    last_error: Exception | None = None

    for url in OVERPASS_URLS:
        for attempt in range(3):
            try:
                print(f"[{label}] Запрос к {url} (попытка {attempt + 1}/3)")
                response = requests.post(
                    url,
                    data={"data": query},
                    headers=HEADERS,
                    timeout=300,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                print(f"[{label}] Ошибка: {exc}")
                time.sleep(3)

    if last_error is None:
        raise RuntimeError(f"[{label}] Неизвестная ошибка запроса Overpass")

    raise last_error


def relation_center_from_members(members: list[dict[str, Any]]) -> list[float] | None:
    coords: list[tuple[float, float]] = []

    for member in members:
        geometry = member.get("geometry", [])
        for point in geometry:
            lon = point.get("lon")
            lat = point.get("lat")
            if lon is not None and lat is not None:
                coords.append((lon, lat))

    if not coords:
        return None

    avg_lon = sum(lon for lon, _ in coords) / len(coords)
    avg_lat = sum(lat for _, lat in coords) / len(coords)
    return [avg_lon, avg_lat]


def element_to_feature_station(element: dict[str, Any]) -> dict[str, Any] | None:
    element_type = element["type"]
    osm_id = str(element["id"])
    tags = element.get("tags", {})
    railway = tags.get("railway")
    name = tags.get("name")

    properties = {
        "osm_element_type": element_type,
        "osm_id": osm_id,
        "name": name,
        "railway": railway,
        "tags": tags,
    }

    if element_type == "node":
        lon = element.get("lon")
        lat = element.get("lat")
        if lon is None or lat is None:
            return None

        return {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lon, lat],
            },
            "properties": properties,
        }

    if element_type == "way":
        geometry = element.get("geometry", [])
        if not geometry:
            return None

        coordinates = [[point["lon"], point["lat"]] for point in geometry]

        if len(coordinates) >= 4 and coordinates[0] == coordinates[-1]:
            return {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [coordinates],
                },
                "properties": properties,
            }

        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates,
            },
            "properties": properties,
        }

    if element_type == "relation":
        members = element.get("members", [])
        center = relation_center_from_members(members)
        if center is None:
            return None

        return {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": center,
            },
            "properties": properties,
        }

    return None


def element_to_feature_line(element: dict[str, Any]) -> dict[str, Any] | None:
    element_type = element["type"]
    osm_id = str(element["id"])
    tags = element.get("tags", {})
    railway = tags.get("railway")
    name = tags.get("name")

    properties = {
        "osm_element_type": element_type,
        "osm_id": osm_id,
        "name": name,
        "railway": railway,
        "tags": tags,
    }

    if element_type == "way":
        geometry = element.get("geometry", [])
        if not geometry:
            return None

        coordinates = [[point["lon"], point["lat"]] for point in geometry]

        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates,
            },
            "properties": properties,
        }

    if element_type == "relation":
        members = element.get("members", [])
        line_parts: list[list[list[float]]] = []

        for member in members:
            geometry = member.get("geometry", [])
            coords = [
                [point["lon"], point["lat"]]
                for point in geometry
                if "lon" in point and "lat" in point
            ]
            if len(coords) >= 2:
                line_parts.append(coords)

        if not line_parts:
            return None

        return {
            "type": "Feature",
            "geometry": {
                "type": "MultiLineString",
                "coordinates": line_parts,
            },
            "properties": properties,
        }

    return None


def build_feature_collection(
    elements: list[dict[str, Any]],
    converter,
) -> dict[str, Any]:
    features: list[dict[str, Any]] = []

    for element in elements:
        feature = converter(element)
        if feature is not None:
            features.append(feature)

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def save_geojson(path: Path, feature_collection: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(feature_collection, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    print("Запрашиваю станции и остановочные пункты Самарской области...")
    stations_raw = run_overpass_query(STATIONS_QUERY, "stations")
    time.sleep(2)

    print("Запрашиваю железнодорожные линии Самарской области...")
    lines_raw = run_overpass_query(LINES_QUERY, "lines")

    station_elements = stations_raw.get("elements", [])
    line_elements = lines_raw.get("elements", [])

    stations_geojson = build_feature_collection(
        station_elements,
        element_to_feature_station,
    )
    lines_geojson = build_feature_collection(
        line_elements,
        element_to_feature_line,
    )

    stations_path = OUTPUT_DIR / "samara_stations.geojson"
    lines_path = OUTPUT_DIR / "samara_rail_lines.geojson"

    save_geojson(stations_path, stations_geojson)
    save_geojson(lines_path, lines_geojson)

    print(f"Готово. Станции сохранены в: {stations_path}")
    print(f"Готово. Линии сохранены в: {lines_path}")
    print(f"Станций/остановок: {len(stations_geojson['features'])}")
    print(f"Линий: {len(lines_geojson['features'])}")


if __name__ == "__main__":
    main()