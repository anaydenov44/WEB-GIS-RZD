from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.analytics_schemas import (
    CorridorAnalysisParams,
    CorridorAnalysisResponse,
    HeatmapByGeometryRequest,
    PopulationSummaryRequest,
    PopulationSummaryResponse,
    VirtualRouteCorridorRequest,
)
from app.analytics_service import AnalyticsService
from app.db import get_db

from app.analytics_alternatives_schemas import (
    AlternativeRoutesParams,
    AlternativeRoutesResponse,
    AlternativesByStationsRequest,
)
from app.analytics_alternatives_service import (
    AlternativeRoutesService,
    AlternativesError,
)


router = APIRouter()


@router.post(
    "/routes/{route_id}/corridor",
    response_model=CorridorAnalysisResponse,
)
def analyze_route_corridor(
    route_id: int,
    params: CorridorAnalysisParams,
    db: Session = Depends(get_db),
):
    try:
        service = AnalyticsService(db)
        return service.analyze_real_route(route_id=route_id, params=params)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to analyze route corridor: {exc}",
        ) from exc


@router.post(
    "/virtual-route/corridor",
    response_model=CorridorAnalysisResponse,
)
def analyze_virtual_route_corridor(
    request: VirtualRouteCorridorRequest,
    db: Session = Depends(get_db),
):
    try:
        service = AnalyticsService(db)
        return service.analyze_virtual_route(
            route_geojson=request.route_geojson,
            params=request.params,
            start_station_id=request.start_station_id,
            end_station_id=request.end_station_id,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to analyze virtual route corridor: {exc}",
        ) from exc


@router.post(
    "/heatmap/by-geometry",
    response_model=CorridorAnalysisResponse,
)
def build_heatmap_by_geometry(
    request: HeatmapByGeometryRequest,
    db: Session = Depends(get_db),
):
    try:
        service = AnalyticsService(db)
        return service.build_population_heatmap_by_geometry(
            route_geojson=request.geometry,
            params=request.to_corridor_params(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build heatmap by geometry: {exc}",
        ) from exc


@router.post(
    "/routes/population-summary",
    response_model=PopulationSummaryResponse,
)
def build_population_summary_for_routes(
    request: PopulationSummaryRequest,
    db: Session = Depends(get_db),
):
    try:
        service = AnalyticsService(db)
        return service.build_population_summary_for_routes(request)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build population summary for routes: {exc}",
        ) from exc


@router.post(
    "/routes/{route_id}/alternatives",
    response_model=AlternativeRoutesResponse,
)
def build_route_alternatives(
    route_id: int,
    params: AlternativeRoutesParams,
    db: Session = Depends(get_db),
):
    try:
        service = AlternativeRoutesService(db)
        return service.build_for_route(route_id=route_id, params=params)

    except AlternativesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build route alternatives: {exc}",
        ) from exc


@router.post(
    "/alternatives/by-stations",
    response_model=AlternativeRoutesResponse,
)
def build_alternatives_by_stations(
    request: AlternativesByStationsRequest,
    db: Session = Depends(get_db),
):
    try:
        service = AlternativeRoutesService(db)
        return service.build_between_stations(
            origin_station_id=request.origin_station_id,
            destination_station_id=request.destination_station_id,
            params=request.params,
        )

    except AlternativesError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to build alternatives by stations: {exc}",
        ) from exc
