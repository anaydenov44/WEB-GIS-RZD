from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class AlternativeRoutesParams(BaseModel):
    max_alternatives: int = Field(default=3, ge=1, le=6)
    max_length_ratio: float = Field(default=1.8, ge=1.0, le=5.0)
    min_difference_ratio: float = Field(default=0.12, ge=0.0, le=1.0)
    penalty_factor: float = Field(default=2.8, ge=1.1, le=20.0)
    max_attempts: int = Field(default=18, ge=1, le=80)

    # Чтобы случайно не загрузить всю страну, когда scope не определился.
    max_edges_to_load: int = Field(default=450_000, ge=10_000, le=2_000_000)

    # Если true — сначала пытаемся найти scope_key, где присутствуют обе конечные вершины.
    prefer_common_scope: bool = True


class AlternativesByStationsRequest(BaseModel):
    origin_station_id: int
    destination_station_id: int
    params: AlternativeRoutesParams = Field(default_factory=AlternativeRoutesParams)


class AlternativeRouteItem(BaseModel):
    id: str
    rank: int

    origin_station_id: int
    destination_station_id: int

    source_node_hash: str
    target_node_hash: str

    scope_key: Optional[str] = None

    length_km: float
    length_ratio: float
    overlap_ratio: float
    difference_ratio: float
    edges_count: int

    geometry: dict[str, Any]


class AlternativeRoutesResponse(BaseModel):
    status: str = "ok"
    route_id: Optional[int] = None
    origin_station_id: int
    destination_station_id: int
    source_node_hash: str
    target_node_hash: str
    base_length_km: Optional[float] = None
    alternatives: list[AlternativeRouteItem]
    message: Optional[str] = None
