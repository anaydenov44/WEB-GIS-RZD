const BACKEND_URL = 'http://127.0.0.1:8000';

export const DEFAULT_ANALYTICS_PARAMS = {
  corridor_km: 25,
  station_access_km: 10,
  min_population: 3000,
  max_population: 500000,
  min_score: 0,
  cost_per_km: 250000000,
  station_cost: 800000000,
  exclude_aggregate_like_names: false,
  max_results: 500,
};

export const DEFAULT_ALTERNATIVES_PARAMS = {
  max_alternatives: 5,
  max_length_ratio: 1.8,
  min_difference_ratio: 0.15,
  penalty_factor: 3,
  max_search_radius_km: 50,
  max_attempts: 40,
  max_edges_to_load: 450000,
  prefer_common_scope: true,
};

function normalizeAnalyticsParams(params = {}) {
  return {
    corridor_km: Number(params.corridor_km ?? params.searchRadiusKm ?? params.search_radius_km ?? 25),
    station_access_km: Number(
      params.station_access_km ??
        params.stationServiceRadiusKm ??
        params.station_service_radius_km ??
        10
    ),
    min_population: Number(params.min_population ?? params.minPopulation ?? 3000),
    max_population: Number(params.max_population ?? params.maxPopulation ?? 500000),
    min_score: Number(params.min_score ?? params.minScore ?? 0),
    cost_per_km: Number(params.cost_per_km ?? params.costPerKm ?? 250000000),
    station_cost: Number(params.station_cost ?? params.stationCost ?? 800000000),
    exclude_aggregate_like_names: Boolean(
      params.exclude_aggregate_like_names ?? params.excludeAggregateLikeNames ?? false
    ),
    max_results: Number(params.max_results ?? params.maxObjects ?? params.max_objects ?? 500),
  };
}

function normalizeAlternativesParams(params = {}) {
  return {
    max_alternatives: Number(params.max_alternatives ?? params.maxAlternatives ?? 5),
    max_length_ratio: Number(
      params.max_length_ratio ?? params.maxLengthRatio ?? params.maxDetourRatio ?? 1.8
    ),
    min_difference_ratio: Number(params.min_difference_ratio ?? params.minDifferenceRatio ?? 0.15),
    penalty_factor: Number(params.penalty_factor ?? params.penaltyFactor ?? 3),
    max_search_radius_km: Number(
      params.max_search_radius_km ?? params.maxSearchRadiusKm ?? params.max_search_radius ?? 50
    ),
    max_attempts: Number(params.max_attempts ?? params.maxAttempts ?? 40),
    max_edges_to_load: Number(params.max_edges_to_load ?? params.maxEdgesToLoad ?? 450000),
    prefer_common_scope: Boolean(params.prefer_common_scope ?? params.preferCommonScope ?? true),
  };
}

async function readApiError(response, fallbackMessage) {
  try {
    const data = await response.json();

    if (typeof data?.detail === 'string') {
      return data.detail;
    }

    if (typeof data?.message === 'string') {
      return data.message;
    }

    return fallbackMessage;
  } catch {
    return fallbackMessage;
  }
}

async function postJson(url, payload, fallbackErrorMessage) {
  const response = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(await readApiError(response, fallbackErrorMessage));
  }

  return await response.json();
}

export async function analyzeRealRouteCorridor(routeId, params = {}) {
  if (!routeId) {
    throw new Error('routeId is required');
  }

  return await postJson(
    `${BACKEND_URL}/analytics/routes/${routeId}/corridor`,
    normalizeAnalyticsParams(params),
    'Не удалось выполнить аналитику маршрута'
  );
}

export async function analyzeVirtualRouteCorridor({
  routeGeojson,
  startStationId,
  endStationId,
  params = {},
}) {
  if (!routeGeojson) {
    throw new Error('routeGeojson is required');
  }

  return await postJson(
    `${BACKEND_URL}/analytics/virtual-route/corridor`,
    {
      route_geojson: routeGeojson,
      start_station_id: startStationId ?? null,
      end_station_id: endStationId ?? null,
      params: normalizeAnalyticsParams(params),
    },
    'Не удалось выполнить аналитику виртуального маршрута'
  );
}

export async function buildPopulationHeatmapByGeometry({ geometry, params = {} }) {
  if (!geometry) {
    throw new Error('geometry is required');
  }

  return await postJson(
    `${BACKEND_URL}/analytics/heatmap/by-geometry`,
    {
      geometry,
      ...normalizeAnalyticsParams(params),
    },
    'Не удалось построить тепловую карту'
  );
}

export async function buildRouteAlternatives(routeId, params = {}) {
  if (!routeId) {
    throw new Error('routeId is required');
  }

  return await postJson(
    `${BACKEND_URL}/analytics/routes/${routeId}/alternatives`,
    normalizeAlternativesParams(params),
    'Не удалось построить альтернативные маршруты'
  );
}

export async function buildAlternativesByStations(
  originStationId,
  destinationStationId,
  params = {}
) {
  if (!originStationId) {
    throw new Error('originStationId is required');
  }

  if (!destinationStationId) {
    throw new Error('destinationStationId is required');
  }

  return await postJson(
    `${BACKEND_URL}/analytics/alternatives/by-stations`,
    {
      origin_station_id: originStationId,
      destination_station_id: destinationStationId,
      params: normalizeAlternativesParams(params),
    },
    'Не удалось построить альтернативные маршруты между станциями'
  );
}

export async function buildPopulationSummaryForRoutes({ routes = [], params = {} }) {
  const normalizedRoutes = (routes || [])
    .filter((route) => route?.id && route?.geometry)
    .map((route) => ({
      id: String(route.id),
      geometry: route.geometry,
    }));

  if (normalizedRoutes.length === 0) {
    return { items: [] };
  }

  const normalizedParams = normalizeAnalyticsParams(params);

  return await postJson(
    `${BACKEND_URL}/analytics/routes/population-summary`,
    {
      radius_km: normalizedParams.corridor_km,
      corridor_km: normalizedParams.corridor_km,
      min_population: normalizedParams.min_population,
      max_population: normalizedParams.max_population,
      exclude_aggregate_like_names: normalizedParams.exclude_aggregate_like_names,
      routes: normalizedRoutes,
    },
    'Не удалось рассчитать население вдоль маршрутов'
  );
}
