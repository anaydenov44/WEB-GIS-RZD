import React, { useMemo } from "react";
import ReactECharts from "echarts-for-react";
import type { AnalysisRoute } from "../../utils/analysisRoutes";

type Props = {
  routes: AnalysisRoute[];
  selectedRouteId: string | null;
  analysisRunId?: number;
  onSelectRoute: (routeId: string) => void;
};

export function RouteDistanceChart({
  routes,
  selectedRouteId,
  analysisRunId = 0,
  onSelectRoute,
}: Props) {
  const option = useMemo(() => {
    return {
      title: {
        text: "Длина маршрутов",
        left: 8,
        textStyle: { fontSize: 13 },
      },
      grid: {
        left: 48,
        right: 16,
        top: 42,
        bottom: 64,
      },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        formatter: (params: any) => {
          const item = Array.isArray(params) ? params[0] : params;
          const route = routes.find((candidate) => candidate.id === item?.data?.routeId) ?? routes[item.dataIndex];

          if (!route) return "";

          const original = routes.find((candidate) => candidate.id === "original");
          const originalLength = original?.lengthKm || 0;

          let ratioText = "";
          if (originalLength > 0 && route.id !== "original") {
            const ratio = ((route.lengthKm / originalLength - 1) * 100).toFixed(1);
            ratioText = `<br/>Отклонение от оригинала: ${Number(ratio) >= 0 ? "+" : ""}${ratio}%`;
          }

          return `
            <b>${route.label}</b><br/>
            Расстояние: ${route.lengthKm.toFixed(1)} км
            ${ratioText}
          `;
        },
      },
      xAxis: {
        type: "category",
        data: routes.map((route) => route.label),
        axisLabel: {
          interval: 0,
          rotate: 25,
        },
      },
      yAxis: {
        type: "value",
        name: "км",
      },
      series: [
        {
          type: "bar",
          data: routes.map((route) => ({
            value: route.id === "original" && route.lengthKm <= 0 ? 0.5 : route.lengthKm,
            realValue: route.lengthKm,
            routeId: route.id,
            itemStyle: {
              color: route.color,
              borderColor: route.id === selectedRouteId ? "#111827" : route.color,
              borderWidth: route.id === selectedRouteId ? 3 : 0,
            },
          })),
          barMaxWidth: 42,
        },
      ],
    };
  }, [routes, selectedRouteId]);

  return (
    <ReactECharts
      key={`length-chart-${analysisRunId}-${routes.map((route) => route.id).join("|")}`}
      option={option}
      notMerge={true}
      lazyUpdate={false}
      className="route-distance-chart"
      style={{ width: "100%", height: 320 }}
      onEvents={{
        click: (params: any) => {
          const routeId = params?.data?.routeId;
          if (routeId) {
            onSelectRoute(routeId);
          }
        },
      }}
    />
  );
}
