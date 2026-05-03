import React, { useEffect, useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { AnalysisRoute } from "../../utils/analysisRoutes";
import { RouteDistanceChart } from "./RouteDistanceChart";

type PopulationStats = {
  population_total?: number;
  populationTotal?: number;
  settlements_count?: number;
  settlementsCount?: number;
  population_density_per_km?: number;
  populationDensityPerKm?: number;
};

type Props = {
  routes: AnalysisRoute[];
  selectedRouteId: string | null;
  heatmapRouteId: string | null;
  heatmapLoading: boolean;
  populationStatsByRouteId?: Record<string, PopulationStats>;
  analysisRunId?: number;
  onSelectRoute: (routeId: string) => void;
  onShowHeatmap: () => void;
  onHideHeatmap: () => void;
};

type AnalysisRouteChartItem = {
  id: string;
  label: string;
  isOriginal: boolean;
  length_km: number;
  length_ratio: number;
  overlap_ratio: number;
  difference_ratio: number;
  population_total: number;
  settlements_count: number;
  population_density_per_km: number;
  color: string;
};

const ASSUMED_TRAIN_SPEED_KMH = 60;

function toNumber(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("ru-RU").format(Math.round(value));
}

function formatCompactNumber(value: number): string {
  return new Intl.NumberFormat("ru-RU", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}

function formatHours(hours: number): string {
  if (!Number.isFinite(hours)) return "—";

  const wholeHours = Math.floor(hours);
  const minutes = Math.round((hours - wholeHours) * 60);

  if (minutes === 60) {
    return `${wholeHours + 1} ч 0 мин`;
  }

  return `${wholeHours} ч ${minutes} мин`;
}

function makeRouteClickEvents(onSelectRoute: (routeId: string) => void) {
  return {
    click: (params: any) => {
      const routeId = params?.data?.routeId;
      if (routeId) {
        onSelectRoute(routeId);
      }
    },
  };
}

function getStatsValue(stats: PopulationStats | undefined, snakeKey: keyof PopulationStats, camelKey: keyof PopulationStats): number {
  return toNumber(stats?.[snakeKey] ?? stats?.[camelKey], 0);
}

function buildRouteChartItems(
  routes: AnalysisRoute[],
  populationStatsByRouteId: Record<string, PopulationStats>
): AnalysisRouteChartItem[] {
  return routes.map((route, index) => {
    const raw = route.raw || {};
    const isOriginal = route.kind === "original" || route.id === "original" || index === 0;
    const stats = populationStatsByRouteId[route.id] || raw.population_stats || raw.populationStats || raw.analytics;

    const lengthKm = toNumber(route.lengthKm ?? raw.length_km ?? raw.lengthKm, 0);
    const populationTotal = getStatsValue(stats, "population_total", "populationTotal");
    const settlementsCount = getStatsValue(stats, "settlements_count", "settlementsCount");
    const densityFromStats = getStatsValue(stats, "population_density_per_km", "populationDensityPerKm");

    return {
      id: route.id,
      label: route.label || (isOriginal ? "Оригинал" : `Альтернатива ${index}`),
      isOriginal,
      length_km: lengthKm,
      length_ratio: isOriginal ? 1 : toNumber(raw.length_ratio ?? raw.lengthRatio, 1),
      overlap_ratio: isOriginal ? 1 : toNumber(raw.overlap_ratio ?? raw.overlapRatio, 0),
      difference_ratio: isOriginal ? 0 : toNumber(raw.difference_ratio ?? raw.differenceRatio, 0),
      population_total: populationTotal,
      settlements_count: settlementsCount,
      population_density_per_km:
        densityFromStats > 0 ? densityFromStats : lengthKm > 0 ? populationTotal / lengthKm : 0,
      color: route.color || (isOriginal ? "#16a34a" : "#2563eb"),
    };
  });
}

function getBarBorder(item: AnalysisRouteChartItem, selectedRouteId: string | null) {
  return {
    color: item.color,
    borderColor: item.id === selectedRouteId ? "#111827" : item.color,
    borderWidth: item.id === selectedRouteId ? 2 : 0,
  };
}

function PopulationPlaceholder() {
  return (
    <div className="analytics-chart-placeholder">
      Статистика населения появится после расчёта охвата маршрутов. Если backend endpoint уже добавлен,
      данные загрузятся автоматически после построения альтернатив.
    </div>
  );
}

export function AnalyticsRightPanel({
  routes,
  selectedRouteId,
  heatmapRouteId,
  heatmapLoading,
  populationStatsByRouteId = {},
  analysisRunId = 0,
  onSelectRoute,
  onShowHeatmap,
  onHideHeatmap,
}: Props) {
  const analysisRouteItems = useMemo(
    () => buildRouteChartItems(routes, populationStatsByRouteId),
    [routes, populationStatsByRouteId]
  );

  const selectedRoute =
    routes.find((route) => route.id === selectedRouteId) ?? routes[0] ?? null;

  const heatmapRoute = routes.find((route) => route.id === heatmapRouteId) ?? null;

  const hasPopulationStats = analysisRouteItems.some(
    (item) => item.population_total > 0 || item.settlements_count > 0
  );

  const chartClickEvents = useMemo(
    () => makeRouteClickEvents(onSelectRoute),
    [onSelectRoute]
  );

  useEffect(() => {
    console.log("[ANALYTICS CHART ROUTES]", {
      analysisRunId,
      chartItemsCount: analysisRouteItems.length,
      chartItems: analysisRouteItems.map((item) => ({
        id: item.id,
        label: item.label,
        lengthKm: item.length_km,
      })),
    });
  }, [analysisRunId, analysisRouteItems]);

  const chartKeyPrefix = `${analysisRunId}-${analysisRouteItems.map((item) => item.id).join("|")}`;

  const travelTimeChartOption = useMemo(
    () => ({
      title: {
        text: "Условное время пути",
        left: 8,
        textStyle: { fontSize: 13 },
      },
      tooltip: {
        trigger: "axis",
        formatter: (params: any[]) => {
          const item = params?.[0]?.data;
          if (!item) return "";
          return `
            ${item.label}<br/>
            Длина: ${item.lengthKm.toFixed(1)} км<br/>
            Время при 60 км/ч: ${formatHours(item.realHours)}
          `;
        },
      },
      grid: {
        left: 48,
        right: 20,
        top: 42,
        bottom: 68,
      },
      xAxis: {
        type: "category",
        data: analysisRouteItems.map((item) => item.label),
        axisLabel: { interval: 0, rotate: 35 },
      },
      yAxis: {
        type: "value",
        name: "часы",
      },
      series: [
        {
          type: "bar",
          data: analysisRouteItems.map((item) => {
            const hours = item.length_km / ASSUMED_TRAIN_SPEED_KMH;
            return {
              value: hours,
              realHours: hours,
              lengthKm: item.length_km,
              routeId: item.id,
              label: item.label,
              itemStyle: getBarBorder(item, selectedRouteId),
            };
          }),
          barMaxWidth: 36,
        },
      ],
    }),
    [analysisRouteItems, selectedRouteId]
  );

  const differenceChartOption = useMemo(
    () => ({
      title: {
        text: "Отличие от оригинала",
        left: 8,
        textStyle: { fontSize: 13 },
      },
      tooltip: {
        trigger: "axis",
        formatter: (params: any[]) => {
          const item = params?.[0]?.data;
          if (!item) return "";
          return `${item.label}<br/>Отличие: ${item.realValue.toFixed(1)}%`;
        },
      },
      grid: {
        left: 96,
        right: 24,
        top: 42,
        bottom: 24,
      },
      xAxis: {
        type: "value",
        max: 100,
        axisLabel: { formatter: "{value}%" },
      },
      yAxis: {
        type: "category",
        data: analysisRouteItems.map((item) => item.label),
      },
      series: [
        {
          type: "bar",
          data: analysisRouteItems.map((item) => {
            const realValue = item.difference_ratio * 100;
            return {
              value: item.isOriginal ? 0.5 : realValue,
              realValue,
              label: item.label,
              itemStyle: getBarBorder(item, selectedRouteId),
              routeId: item.id,
            };
          }),
          barMaxWidth: 28,
        },
      ],
    }),
    [analysisRouteItems, selectedRouteId]
  );

  const populationTotalChartOption = useMemo(
    () => ({
      title: {
        text: "Население в радиусе маршрута",
        left: 8,
        textStyle: { fontSize: 13 },
      },
      tooltip: {
        trigger: "axis",
        formatter: (params: any[]) => {
          const item = params?.[0]?.data;
          if (!item) return "";
          return `${item.label}<br/>Население: ${formatNumber(item.value)} чел.<br/>Пунктов: ${formatNumber(item.settlementsCount || 0)}`;
        },
      },
      grid: {
        left: 72,
        right: 20,
        top: 42,
        bottom: 68,
      },
      xAxis: {
        type: "category",
        data: analysisRouteItems.map((item) => item.label),
        axisLabel: { interval: 0, rotate: 35 },
      },
      yAxis: {
        type: "value",
        axisLabel: {
          formatter: (value: number) => formatCompactNumber(value),
        },
      },
      series: [
        {
          type: "bar",
          data: analysisRouteItems.map((item) => ({
            value: item.population_total,
            routeId: item.id,
            label: item.label,
            settlementsCount: item.settlements_count,
            itemStyle: getBarBorder(item, selectedRouteId),
          })),
          barMaxWidth: 36,
        },
      ],
    }),
    [analysisRouteItems, selectedRouteId]
  );

  const populationPerKmChartOption = useMemo(
    () => ({
      title: {
        text: "Население на 1 км маршрута",
        left: 8,
        textStyle: { fontSize: 13 },
      },
      tooltip: {
        trigger: "axis",
        formatter: (params: any[]) => {
          const item = params?.[0]?.data;
          if (!item) return "";
          return `${item.label}<br/>Население / км: ${formatNumber(item.value)} чел.`;
        },
      },
      grid: {
        left: 72,
        right: 20,
        top: 42,
        bottom: 68,
      },
      xAxis: {
        type: "category",
        data: analysisRouteItems.map((item) => item.label),
        axisLabel: { interval: 0, rotate: 35 },
      },
      yAxis: {
        type: "value",
        axisLabel: {
          formatter: (value: number) => formatCompactNumber(value),
        },
      },
      series: [
        {
          type: "bar",
          data: analysisRouteItems.map((item) => ({
            value: item.population_density_per_km,
            routeId: item.id,
            label: item.label,
            itemStyle: getBarBorder(item, selectedRouteId),
          })),
          barMaxWidth: 36,
        },
      ],
    }),
    [analysisRouteItems, selectedRouteId]
  );

  const lengthPopulationScatterOption = useMemo(
    () => ({
      title: {
        text: "Длина vs население",
        left: 8,
        textStyle: { fontSize: 13 },
      },
      tooltip: {
        formatter: (params: any) => {
          const item = params.data;
          return `
            ${item.label}<br/>
            Длина: ${item.value[0].toFixed(1)} км<br/>
            Население: ${formatNumber(item.value[1])}
          `;
        },
      },
      grid: {
        left: 64,
        right: 20,
        top: 42,
        bottom: 48,
      },
      xAxis: { type: "value", name: "км" },
      yAxis: {
        type: "value",
        name: "население",
        axisLabel: {
          formatter: (value: number) => formatCompactNumber(value),
        },
      },
      series: [
        {
          type: "scatter",
          symbolSize: (value: number[], params: any) =>
            params?.data?.routeId === selectedRouteId ? 18 : 14,
          data: analysisRouteItems.map((item) => ({
            value: [item.length_km, item.population_total],
            routeId: item.id,
            label: item.label,
            itemStyle: {
              color: item.color,
              borderColor: item.id === selectedRouteId ? "#111827" : item.color,
              borderWidth: item.id === selectedRouteId ? 2 : 0,
            },
          })),
        },
      ],
    }),
    [analysisRouteItems, selectedRouteId]
  );

  const differencePopulationScatterOption = useMemo(
    () => ({
      title: {
        text: "Отличие vs население",
        left: 8,
        textStyle: { fontSize: 13 },
      },
      tooltip: {
        formatter: (params: any) => {
          const item = params.data;
          return `
            ${item.label}<br/>
            Отличие: ${item.value[0].toFixed(1)}%<br/>
            Население: ${formatNumber(item.value[1])}
          `;
        },
      },
      grid: {
        left: 64,
        right: 20,
        top: 42,
        bottom: 48,
      },
      xAxis: {
        type: "value",
        name: "отличие, %",
        axisLabel: { formatter: "{value}%" },
      },
      yAxis: {
        type: "value",
        name: "население",
        axisLabel: {
          formatter: (value: number) => formatCompactNumber(value),
        },
      },
      series: [
        {
          type: "scatter",
          symbolSize: (value: number[], params: any) =>
            params?.data?.routeId === selectedRouteId ? 18 : 14,
          data: analysisRouteItems.map((item) => ({
            value: [item.difference_ratio * 100, item.population_total],
            routeId: item.id,
            label: item.label,
            itemStyle: {
              color: item.color,
              borderColor: item.id === selectedRouteId ? "#111827" : item.color,
              borderWidth: item.id === selectedRouteId ? 2 : 0,
            },
          })),
        },
      ],
    }),
    [analysisRouteItems, selectedRouteId]
  );

  return (
    <aside className="analytics-right-panel">
      <div className="analytics-card">
        <h2 className="analytics-title">Аналитика маршрутов</h2>

        {routes.length === 0 ? (
          <p className="analytics-muted">
            Построй маршрут и альтернативы, чтобы увидеть графики.
          </p>
        ) : (
          <>
            <RouteDistanceChart
              routes={routes}
              selectedRouteId={selectedRouteId}
              analysisRunId={analysisRunId}
              onSelectRoute={onSelectRoute}
            />

            <ReactECharts
              key={`travel-time-${chartKeyPrefix}`}
              option={travelTimeChartOption}
              notMerge={true}
              lazyUpdate={false}
              className="analytics-echart"
              style={{ width: "100%", height: 280 }}
              onEvents={chartClickEvents}
            />
            <p className="analytics-chart-note">
              Условное время рассчитано при средней скорости {ASSUMED_TRAIN_SPEED_KMH} км/ч.
            </p>

            <ReactECharts
              key={`difference-${chartKeyPrefix}`}
              option={differenceChartOption}
              notMerge={true}
              lazyUpdate={false}
              className="analytics-echart"
              style={{ width: "100%", height: 260 }}
              onEvents={chartClickEvents}
            />

            {hasPopulationStats ? (
              <>
                <ReactECharts
                  key={`population-total-${chartKeyPrefix}`}
                  option={populationTotalChartOption}
                  notMerge={true}
                  lazyUpdate={false}
                  className="analytics-echart"
                  style={{ width: "100%", height: 280 }}
                  onEvents={chartClickEvents}
                />

                <ReactECharts
                  key={`population-per-km-${chartKeyPrefix}`}
                  option={populationPerKmChartOption}
                  notMerge={true}
                  lazyUpdate={false}
                  className="analytics-echart"
                  style={{ width: "100%", height: 280 }}
                  onEvents={chartClickEvents}
                />

                <ReactECharts
                  key={`length-population-${chartKeyPrefix}`}
                  option={lengthPopulationScatterOption}
                  notMerge={true}
                  lazyUpdate={false}
                  className="analytics-echart"
                  style={{ width: "100%", height: 280 }}
                  onEvents={chartClickEvents}
                />

                <ReactECharts
                  key={`difference-population-${chartKeyPrefix}`}
                  option={differencePopulationScatterOption}
                  notMerge={true}
                  lazyUpdate={false}
                  className="analytics-echart"
                  style={{ width: "100%", height: 280 }}
                  onEvents={chartClickEvents}
                />
              </>
            ) : (
              <PopulationPlaceholder />
            )}

            <div className="analytics-route-list">
              {routes.map((route) => (
                <button
                  key={route.id}
                  type="button"
                  className={
                    route.id === selectedRouteId
                      ? "analytics-route-item analytics-route-item-active"
                      : "analytics-route-item"
                  }
                  onClick={() => onSelectRoute(route.id)}
                >
                  <span
                    className="analytics-route-color"
                    style={{ backgroundColor: route.color }}
                  />
                  <span className="analytics-route-name">{route.label}</span>
                  <span className="analytics-route-distance">
                    {route.lengthKm.toFixed(1)} км
                  </span>
                </button>
              ))}
            </div>
          </>
        )}
      </div>

      <div className="analytics-card">
        <h3 className="analytics-subtitle">Тепловая карта</h3>

        {selectedRoute ? (
          <>
            <p className="analytics-muted">
              Выбран маршрут: <b>{selectedRoute.label}</b>
            </p>

            <div className="analytics-right-actions">
              <button
                type="button"
                className="analytics-primary-button"
                onClick={onShowHeatmap}
                disabled={heatmapLoading || !selectedRoute.geometry}
              >
                {heatmapLoading
                  ? "Строится..."
                  : "Отобразить тепловую карту населённых пунктов"}
              </button>

              {heatmapRoute && (
                <button
                  type="button"
                  className="analytics-secondary-button"
                  onClick={onHideHeatmap}
                >
                  Скрыть тепловую карту
                </button>
              )}
            </div>

            {heatmapRoute && (
              <p className="analytics-muted">
                Сейчас показана тепловая карта для: <b>{heatmapRoute.label}</b>
              </p>
            )}
          </>
        ) : (
          <p className="analytics-muted">Маршрут не выбран.</p>
        )}
      </div>
    </aside>
  );
}
