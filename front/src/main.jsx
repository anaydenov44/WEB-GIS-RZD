import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App.jsx';
import './index.css';

const appStart = performance.now();

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);

window.addEventListener('load', () => {
  const navigation = performance.getEntriesByType('navigation')[0];

  const totalLoadMs = navigation
    ? navigation.loadEventEnd - navigation.startTime
    : performance.now() - appStart;

  window.__GIS_PERF__ = window.__GIS_PERF__ || [];

  window.__GIS_PERF__.push({
    operation: 'Открытие интерфейса',
    duration_ms: Number(totalLoadMs.toFixed(2)),
    status: 'ok',
    timestamp: new Date().toISOString(),
  });

  localStorage.setItem('web_gis_perf_results', JSON.stringify(window.__GIS_PERF__));

  console.table(window.__GIS_PERF__);
});
