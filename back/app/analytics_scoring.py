from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class ScoreInput:
    population: int
    max_population: int | None
    distance_to_route_km: float
    distance_to_nearest_route_station_km: float | None
    corridor_km: float
    station_access_km: float
    estimated_connection_cost: float


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def get_attention_level(score: float) -> str:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def calculate_candidate_score(data: ScoreInput) -> float:
    """
    Интегральный рейтинг населённого пункта.

    Смысл:
    - больше население → выше score;
    - дальше от станции маршрута → выше score;
    - ближе к линии маршрута → выше score;
    - дороже подключение → ниже score.
    """

    population = max(data.population, 0)
    effective_max_population = data.max_population or max(population, 1)

    population_factor = math.log10(population + 1) / math.log10(effective_max_population + 1)
    population_factor = clamp(population_factor, 0.0, 1.0)

    if data.distance_to_nearest_route_station_km is None:
        underserved_factor = 1.0
    else:
        underserved_factor = data.distance_to_nearest_route_station_km / max(data.station_access_km, 1.0)
        underserved_factor = clamp(underserved_factor / 2.0, 0.0, 1.0)

    proximity_factor = 1.0 - data.distance_to_route_km / max(data.corridor_km, 1.0)
    proximity_factor = clamp(proximity_factor, 0.0, 1.0)

    # В v1 пока без полноценной сегментации перегонов.
    # Даём нейтральный вклад.
    station_gap_factor = 0.5

    cost_penalty = min(20.0, data.estimated_connection_cost / 1_000_000_000.0 * 5.0)

    score = (
        35.0 * population_factor
        + 25.0 * underserved_factor
        + 20.0 * proximity_factor
        + 20.0 * station_gap_factor
        - cost_penalty
    )

    return round(clamp(score, 0.0, 100.0), 2)