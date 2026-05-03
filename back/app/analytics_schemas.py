from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class CorridorAnalysisParams(BaseModel):
    corridor_km: float = Field(default=25.0, ge=1.0, le=300.0)
    station_access_km: float = Field(default=10.0, ge=1.0, le=100.0)

    min_population: int = Field(default=3000, ge=0)
    max_population: Optional[int] = Field(default=500000, ge=0)

    min_score: float = Field(default=0.0, ge=0.0, le=100.0)

    cost_per_km: float = Field(default=250_000_000.0, ge=0.0)
    station_cost: float = Field(default=800_000_000.0, ge=0.0)

    exclude_aggregate_like_names: bool = False

    # Чтобы карта не захлебнулась точками.
    max_results: int = Field(default=500, ge=1, le=5000)


class VirtualRouteCorridorRequest(BaseModel):
    route_geojson: dict[str, Any]
    start_station_id: Optional[int] = None
    end_station_id: Optional[int] = None
    params: CorridorAnalysisParams = Field(default_factory=CorridorAnalysisParams)


class HeatmapByGeometryRequest(BaseModel):
    geometry: dict[str, Any]
    corridor_km: float = Field(default=25.0, ge=1.0, le=300.0)
    station_access_km: float = Field(default=10.0, ge=1.0, le=100.0)
    min_population: int = Field(default=3000, ge=0)
    max_population: Optional[int] = Field(default=500000, ge=0)
    min_score: float = Field(default=0.0, ge=0.0, le=100.0)
    cost_per_km: float = Field(default=250_000_000.0, ge=0.0)
    station_cost: float = Field(default=800_000_000.0, ge=0.0)
    exclude_aggregate_like_names: bool = False
    max_results: int = Field(default=500, ge=1, le=5000)

    def to_corridor_params(self) -> CorridorAnalysisParams:
        return CorridorAnalysisParams(
            corridor_km=self.corridor_km,
            station_access_km=self.station_access_km,
            min_population=self.min_population,
            max_population=self.max_population,
            min_score=self.min_score,
            cost_per_km=self.cost_per_km,
            station_cost=self.station_cost,
            exclude_aggregate_like_names=self.exclude_aggregate_like_names,
            max_results=self.max_results,
        )


class PopulationSummaryRouteInput(BaseModel):
    id: str
    geometry: dict[str, Any]


class PopulationSummaryRequest(BaseModel):
    routes: list[PopulationSummaryRouteInput]
    radius_km: Optional[float] = Field(default=None, ge=1.0, le=300.0)
    corridor_km: Optional[float] = Field(default=None, ge=1.0, le=300.0)
    min_population: int = Field(default=3000, ge=0)
    max_population: Optional[int] = Field(default=500000, ge=0)
    exclude_aggregate_like_names: bool = False

    @property
    def effective_radius_km(self) -> float:
        return float(self.radius_km or self.corridor_km or 25.0)


class PopulationSummaryItem(BaseModel):
    route_id: str
    population_total: int
    settlements_count: int
    population_density_per_km: float
    length_km: Optional[float] = None


class PopulationSummaryResponse(BaseModel):
    items: list[PopulationSummaryItem]


class RouteInfo(BaseModel):
    id: Optional[int] = None
    source: str
    length_km: Optional[float] = None
    stations_count: Optional[int] = None


class AnalyticsSummary(BaseModel):
    settlements_in_corridor: int
    candidate_settlements: int
    served_population: int
    underserved_population: int
    max_attention_score: float
    avg_attention_score: float


class VirtualStationInfo(BaseModel):
    geometry: dict[str, Any]
    distance_from_settlement_km: float


class SettlementCandidate(BaseModel):
    id: int
    name: str
    settlement_type: Optional[str] = None
    region: Optional[str] = None
    federal_district: Optional[str] = None
    population: int

    distance_to_route_km: float
    distance_to_nearest_route_station_km: Optional[float] = None

    served: bool
    score: float
    attention_level: str

    estimated_connection_km: float
    estimated_connection_cost: float
    cost_per_1000_people: Optional[float] = None

    geometry: dict[str, Any]
    virtual_station: VirtualStationInfo


class CorridorAnalysisResponse(BaseModel):
    route: RouteInfo
    params: CorridorAnalysisParams
    summary: AnalyticsSummary
    settlements: list[SettlementCandidate]
