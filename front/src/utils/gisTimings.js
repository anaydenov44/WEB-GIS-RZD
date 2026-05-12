// front/src/utils/gisTimings.js

const STORAGE_KEY = 'web_gis_perf_results';

function getStore() {
  if (!window.__GIS_TIMINGS__) {
    const saved = localStorage.getItem(STORAGE_KEY);
    window.__GIS_TIMINGS__ = saved ? JSON.parse(saved) : [];
  }

  return window.__GIS_TIMINGS__;
}

function saveStore() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(getStore()));
}

export function addMetric({
  operation,
  startedAt,
  finishedAt,
  durationMs,
  status = 'ok',
  details = {},
}) {
  const safeDuration =
    Number.isFinite(Number(durationMs)) && Number(durationMs) >= 0
      ? Number(durationMs)
      : Number(finishedAt ?? 0) - Number(startedAt ?? 0);

  const item = {
    operation,
    duration_ms: Number(safeDuration.toFixed(2)),
    status,
    timestamp: new Date().toISOString(),
    ...details,
  };

  getStore().push(item);
  saveStore();

  console.table([item]);

  return item;
}

export async function measureAsync(operation, fn, details = {}) {
  const startedAt = performance.now();

  try {
    const result = await fn();
    const finishedAt = performance.now();

    addMetric({
      operation,
      startedAt,
      finishedAt,
      durationMs: finishedAt - startedAt,
      status: 'ok',
      details,
    });

    return result;
  } catch (error) {
    const finishedAt = performance.now();

    addMetric({
      operation,
      startedAt,
      finishedAt,
      durationMs: finishedAt - startedAt,
      status: 'error',
      details: {
        ...details,
        error: error instanceof Error ? error.message : String(error),
      },
    });

    throw error;
  }
}

export function getMetrics() {
  return getStore();
}

export function clearMetrics() {
  window.__GIS_TIMINGS__ = [];
  localStorage.removeItem(STORAGE_KEY);
}

export function exportMetricsCsv(filename = 'web-gis-performance.csv') {
  const rows = getStore();

  if (rows.length === 0) {
    console.warn('Нет замеров для экспорта');
    return;
  }

  const headers = Array.from(
    rows.reduce((set, row) => {
      Object.keys(row).forEach((key) => set.add(key));
      return set;
    }, new Set())
  );

  const csv = [
    headers.join(';'),
    ...rows.map((row) =>
      headers
        .map((key) => {
          const value = row[key] ?? '';
          return `"${String(value).replaceAll('"', '""')}"`;
        })
        .join(';')
    ),
  ].join('\n');

  const blob = new Blob([csv], {
    type: 'text/csv;charset=utf-8;',
  });

  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');

  link.href = url;
  link.download = filename;
  link.click();

  URL.revokeObjectURL(url);
}