import json
from pathlib import Path

import requests

NOMINATIM_LOOKUP_URL = "https://nominatim.openstreetmap.org/lookup"

BASE_DIR = Path(__file__).resolve().parent.parent.parent
OUTPUT_PATH = BASE_DIR / "front" / "public" / "federal-districts.geojson"
UNMATCHED_OUTPUT_PATH = BASE_DIR / "front" / "public" / "federal-districts-unmatched.json"

OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

FEDERAL_DISTRICTS = [
    {
        "code": "central_fd",
        "label": "Центральный федеральный округ",
        "relation_id": 1029256,
    },
    {
        "code": "northwestern_fd",
        "label": "Северо-Западный федеральный округ",
        "relation_id": 1216601,
    },
    {
        "code": "south_fd",
        "label": "Южный федеральный округ",
        "relation_id": 1059500,
    },
    {
        "code": "north_caucasus_fd",
        "label": "Северо-Кавказский федеральный округ",
        "relation_id": 389344,
    },
    {
        "code": "volga_fd",
        "label": "Приволжский федеральный округ",
        "relation_id": 1075831,
    },
    {
        "code": "ural_fd",
        "label": "Уральский федеральный округ",
        "relation_id": 1113276,
    },
    {
        "code": "siberian_fd",
        "label": "Сибирский федеральный округ",
        "relation_id": 1221148,
    },
    {
        "code": "far_eastern_fd",
        "label": "Дальневосточный федеральный округ",
        "relation_id": 1221185,
    },
]


def fetch_geojson_from_nominatim() -> dict:
    osm_ids = ",".join(f"R{item['relation_id']}" for item in FEDERAL_DISTRICTS)

    headers = {
        "User-Agent": "railway-webgis-diploma/1.0 (federal-districts-geojson-builder)",
        "Accept-Language": "ru,en",
    }

    params = {
        "osm_ids": osm_ids,
        "format": "geojson",
        "polygon_geojson": 1,
        "addressdetails": 1,
        "namedetails": 1,
        "extratags": 1,
    }

    response = requests.get(
        NOMINATIM_LOOKUP_URL,
        params=params,
        headers=headers,
        timeout=300,
    )
    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise ValueError("Nominatim вернул неожиданный формат ответа")

    return payload


def extract_osm_id(feature: dict):
    properties = feature.get("properties", {}) or {}
    osm_id = properties.get("osm_id")

    if osm_id is not None:
        try:
            return int(osm_id)
        except (TypeError, ValueError):
            return None

    feature_id = feature.get("id")
    if isinstance(feature_id, str):
        if feature_id.startswith("R"):
            try:
                return int(feature_id[1:])
            except ValueError:
                return None

    return None


def build_output_geojson(source_geojson: dict) -> tuple[dict, list]:
    source_features = source_geojson.get("features", []) or []
    source_by_relation_id: dict[int, dict] = {}

    for feature in source_features:
        relation_id = extract_osm_id(feature)
        if relation_id is not None:
            source_by_relation_id[relation_id] = feature

    output_features = []
    unmatched = []

    for district in FEDERAL_DISTRICTS:
        relation_id = district["relation_id"]
        source_feature = source_by_relation_id.get(relation_id)

        if not source_feature:
            unmatched.append(
                {
                    "code": district["code"],
                    "label": district["label"],
                    "relation_id": relation_id,
                    "reason": "relation_not_returned",
                }
            )
            continue

        geometry = source_feature.get("geometry")
        if not geometry:
            unmatched.append(
                {
                    "code": district["code"],
                    "label": district["label"],
                    "relation_id": relation_id,
                    "reason": "geometry_missing",
                }
            )
            continue

        source_properties = source_feature.get("properties", {}) or {}
        extratags = source_properties.get("extratags", {}) or {}
        namedetails = source_properties.get("namedetails", {}) or {}
        address = source_properties.get("address", {}) or {}

        output_features.append(
            {
                "type": "Feature",
                "properties": {
                    "code": district["code"],
                    "name": district["label"],
                    "osm_relation_id": relation_id,
                    "osm_name": (
                        namedetails.get("name:ru")
                        or namedetails.get("name")
                        or source_properties.get("display_name")
                        or district["label"]
                    ),
                    "admin_level": extratags.get("admin_level", "3"),
                    "source": "OpenStreetMap / Nominatim",
                    "country": address.get("country"),
                },
                "geometry": geometry,
            }
        )

    output_geojson = {
        "type": "FeatureCollection",
        "features": output_features,
    }

    return output_geojson, unmatched


def main():
    print("Fetching federal district relations from OSM Nominatim...")

    source_geojson = fetch_geojson_from_nominatim()
    output_geojson, unmatched = build_output_geojson(source_geojson)

    OUTPUT_PATH.write_text(
        json.dumps(output_geojson, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    UNMATCHED_OUTPUT_PATH.write_text(
        json.dumps(unmatched, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Saved GeoJSON: {OUTPUT_PATH}")
    print(f"Saved unmatched debug: {UNMATCHED_OUTPUT_PATH}")
    print(f"Features written: {len(output_geojson['features'])}")
    print(f"Unmatched: {len(unmatched)}")

    if unmatched:
        print("WARNING: not all districts were returned")
        for item in unmatched:
            print(
                f" - {item['code']} | relation_id={item['relation_id']} | reason={item['reason']}"
            )
    else:
        print("Done: all 8 federal districts were fetched successfully")


if __name__ == "__main__":
    main()