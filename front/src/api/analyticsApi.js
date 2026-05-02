const BACKEND_URL = 'http://127.0.0.1:8000';

export const DEFAULT_ANALYTICS_PARAMS = {
  searchRadiusKm: 30,
  stationServiceRadiusKm: 10,
  minPopulation: 1000,
  maxPopulation: 10000000,
  minScore: 0,
  maxObjects: 300,
};

export const DEFAULT_ALTERNATIVES_PARAMS = {
  maxAlternatives: 5,
  maxDetourRatio: 1.8,
  maxSearchRadiusKm: 50,
};

function normalizeAnalyticsParams(params = {}) {
  return {
    search_radius_km: Number(params.searchRadiusKm ?? params.search_radius_km ?? 30),

    // В UI этот параметр можно не показывать, но backend пока получает дефолт.
    station_service_radius_km: Number(
      params.stationServiceRadiusKm ??
        params.station_service_radius_km ??
        10
    ),

    min_population: Number(params.minPopulation ?? params.min_population ?? 0),
    max_population: Number(
      params.maxPopulation ??
        params.max_population ??
        10000000
    ),

    // Score оставляем техническим фильтром. В UI можно не показывать.
    min_score: Number(params.minScore ?? params.min_score ?? 0),

    max_objects: Number(params.maxObjects ?? params.max_objects ?? 300),
  };
}

function normalizeAlternativesParams(params = {}) {
  return {
    max_alternatives: Number(
      params.maxAlternatives ??
        params.max_alternatives ??
        5
    ),
    max_detour_ratio: Number(
      params.maxDetourRatio ??
        params.max_detour_ratio ??
        1.8
    ),
    max_search_radius_km: Number(
      params.maxSearchRadiusKm ??
        params.max_search_radius_km ??
        50
    ),
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
      ...normalizeAnalyticsParams(params),
    },
    'Не удалось выполнить аналитику виртуального маршрута'
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
      ...normalizeAlternativesParams(params),
    },
    'Не удалось построить альтернативные маршруты между станциями'
  );
}