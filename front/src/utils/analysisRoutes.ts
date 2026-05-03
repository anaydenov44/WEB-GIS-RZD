export type AnalysisRouteKind = "original" | "alternative";

export type GeoJsonGeometry = {
  type: "LineString" | "MultiLineString";
  coordinates: any[];
};

export type AnalysisRoute = {
  id: string;
  kind: AnalysisRouteKind;
  label: string;
  lengthKm: number;
  geometry: GeoJsonGeometry | null;
  color: string;
  raw: any;
};

const ORIGINAL_ROUTE_COLOR = "#16a34a";

const ALTERNATIVE_COLORS = [
  "#2563eb",
  "#dc2626",
  "#9333ea",
  "#f97316",
  "#0891b2",
  "#ca8a04",
  "#be123c",
  "#4f46e5",
];

export function getAnalysisRouteColor(index: number): string {
  if (index === 0) {
    return ORIGINAL_ROUTE_COLOR;
  }

  const alternativeIndex = index - 1;

  if (alternativeIndex < ALTERNATIVE_COLORS.length) {
    return ALTERNATIVE_COLORS[alternativeIndex];
  }

  const hue = (index * 47 + 25) % 360;
  return `hsl(${hue}, 75%, 42%)`;
}

function toNumber(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function normalizeGeometry(input: any): GeoJsonGeometry | null {
  if (!input) return null;

  let value = input;

  if (typeof value === "string") {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }

  if (value?.type === "Feature") {
    return normalizeGeometry(value.geometry);
  }

  if (value?.type === "FeatureCollection") {
    const firstFeature = Array.isArray(value.features)
      ? value.features.find((feature: any) => feature?.geometry)
      : null;
    return normalizeGeometry(firstFeature?.geometry);
  }

  if (value?.type === "LineString" || value?.type === "MultiLineString") {
    return value as GeoJsonGeometry;
  }

  return null;
}

export function haversineKm(a: [number, number], b: [number, number]): number {
  const radiusKm = 6371.0088;

  const lon1 = (a[0] * Math.PI) / 180;
  const lat1 = (a[1] * Math.PI) / 180;
  const lon2 = (b[0] * Math.PI) / 180;
  const lat2 = (b[1] * Math.PI) / 180;

  const dLon = lon2 - lon1;
  const dLat = lat2 - lat1;

  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;

  return 2 * radiusKm * Math.asin(Math.sqrt(h));
}

export function getGeometryLengthKm(geometry: any): number {
  const normalizedGeometry = normalizeGeometry(geometry);
  if (!normalizedGeometry) return 0;

  const calcLine = (coords: [number, number][]) => {
    let total = 0;

    for (let i = 1; i < coords.length; i += 1) {
      const previous = coords[i - 1];
      const current = coords[i];

      if (
        Array.isArray(previous) &&
        Array.isArray(current) &&
        Number.isFinite(Number(previous[0])) &&
        Number.isFinite(Number(previous[1])) &&
        Number.isFinite(Number(current[0])) &&
        Number.isFinite(Number(current[1]))
      ) {
        total += haversineKm(
          [Number(previous[0]), Number(previous[1])],
          [Number(current[0]), Number(current[1])]
        );
      }
    }

    return total;
  };

  if (normalizedGeometry.type === "LineString") {
    return calcLine((normalizedGeometry.coordinates ?? []) as [number, number][]);
  }

  if (normalizedGeometry.type === "MultiLineString") {
    return (normalizedGeometry.coordinates ?? []).reduce(
      (sum: number, line: [number, number][]) => sum + calcLine(line),
      0
    );
  }

  return 0;
}

function getStopDistanceKm(routeItem: any): number {
  const stops = routeItem?.stops ?? routeItem?.item?.stops ?? [];

  if (!Array.isArray(stops) || stops.length === 0) {
    return 0;
  }

  const lastWithDistance = [...stops]
    .reverse()
    .find((stop) => stop?.distance_km !== undefined && stop?.distance_km !== null);

  return lastWithDistance ? toNumber(lastWithDistance.distance_km) : 0;
}

export function getRouteGeometry(routeItem: any): GeoJsonGeometry | null {
  return normalizeGeometry(
    routeItem?.geometry ??
      routeItem?.item?.geometry ??
      routeItem?.route?.geometry ??
      routeItem?.raw?.geometry ??
      null
  );
}

export function getRouteLengthKm(routeItem: any): number {
  const explicit = toNumber(
    routeItem?.length_km ??
      routeItem?.lengthKm ??
      routeItem?.distance_km ??
      routeItem?.distanceKm ??
      routeItem?.summary?.distance_km ??
      routeItem?.summary?.distanceKm ??
      routeItem?.item?.summary?.distance_km ??
      routeItem?.item?.summary?.distanceKm ??
      routeItem?.route?.distance_km ??
      routeItem?.diagnostics?.selected_path?.total_distance_km,
    0
  );

  if (explicit > 0) {
    return explicit;
  }

  const stopDistanceKm = getStopDistanceKm(routeItem);
  if (stopDistanceKm > 0) {
    return stopDistanceKm;
  }

  return getGeometryLengthKm(getRouteGeometry(routeItem));
}

export function buildAnalysisRoutes(params: {
  originalRoute: any | null;
  alternatives: any[];
}): AnalysisRoute[] {
  const { originalRoute, alternatives } = params;

  const result: AnalysisRoute[] = [];

  if (originalRoute) {
    result.push({
      id: "original",
      kind: "original",
      label: "Оригинал",
      lengthKm: getRouteLengthKm(originalRoute),
      geometry: getRouteGeometry(originalRoute),
      color: getAnalysisRouteColor(0),
      raw: originalRoute,
    });
  }

  for (const [index, alternative] of alternatives.entries()) {
    result.push({
      id: alternative.id ?? `alternative-${index + 1}`,
      kind: "alternative",
      label: `Альтернатива ${alternative.display_rank ?? alternative.rank ?? index + 1}`,
      lengthKm: getRouteLengthKm(alternative),
      geometry: getRouteGeometry(alternative),
      color: getAnalysisRouteColor(index + 1),
      raw: alternative,
    });
  }

  return result;
}
