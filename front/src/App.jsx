import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import './index.css';
import './styles/analytics-right-panel.css';
import 'ol/ol.css';

import Map from 'ol/Map.js';
import View from 'ol/View.js';
import TileLayer from 'ol/layer/Tile.js';
import VectorLayer from 'ol/layer/Vector.js';
import VectorImageLayer from 'ol/layer/VectorImage.js';
import HeatmapLayer from 'ol/layer/Heatmap.js';
import OSM from 'ol/source/OSM.js';
import VectorSource from 'ol/source/Vector.js';
import Feature from 'ol/Feature.js';
import Point from 'ol/geom/Point.js';
import LineString from 'ol/geom/LineString.js';
import GeoJSON from 'ol/format/GeoJSON.js';
import { fromLonLat, transformExtent } from 'ol/proj.js';
import { Style, Stroke, Fill, Circle as CircleStyle, Text } from 'ol/style.js';
import { getCenter } from 'ol/extent.js';

import AnalyticsPanel from './components/AnalyticsPanel.jsx';
import { StationSearchSelect } from './components/StationSearchSelect';
import { AnalyticsRightPanel } from './components/analytics/AnalyticsRightPanel';
import { buildAnalysisRoutes } from './utils/analysisRoutes';
import {
  formatModeLabel,
  formatRegionCode,
  formatRegionShortCode,
  formatStationType,
} from './utils/labels.js';
import {
  DEFAULT_ANALYTICS_PARAMS,
  DEFAULT_ALTERNATIVES_PARAMS,
  analyzeRealRouteCorridor,
  analyzeVirtualRouteCorridor,
  buildPopulationHeatmapByGeometry,
  buildPopulationSummaryForRoutes,
  buildRouteAlternatives,
  buildAlternativesByStations,
} from './api/analyticsApi.js';

const BACKEND_URL = 'http://127.0.0.1:8000';
const RZD_SEARCH_DAYS_AHEAD = 2;
const DEBUG_ROUTE_FLOW = true;

function routeDebug(label, payload = null) {
  if (!DEBUG_ROUTE_FLOW) return;
  const time = new Date().toISOString();

  if (payload === null || payload === undefined) {
    console.log(`[route-flow ${time}] ${label}`);
    return;
  }

  console.groupCollapsed(`[route-flow ${time}] ${label}`);
  console.log(payload);
  console.groupEnd();
}

const RUSSIA_EXTENT = transformExtent([19, 35, 205, 82], 'EPSG:4326', 'EPSG:3857');

const REGION_META = [
  { code: 'central_fd', label: 'Центральный федеральный округ', mapLabel: 'Центральный ФО' },
  { code: 'northwestern_fd', label: 'Северо-Западный федеральный округ', mapLabel: 'Северо-Западный ФО' },
  { code: 'south_fd', label: 'Южный федеральный округ', mapLabel: 'Южный ФО' },
  { code: 'north_caucasus_fd', label: 'Северо-Кавказский федеральный округ', mapLabel: 'СКФО' },
  { code: 'volga_fd', label: 'Приволжский федеральный округ', mapLabel: 'Приволжский ФО' },
  { code: 'ural_fd', label: 'Уральский федеральный округ', mapLabel: 'Уральский ФО' },
  { code: 'siberian_fd', label: 'Сибирский федеральный округ', mapLabel: 'Сибирский ФО' },
  { code: 'far_eastern_fd', label: 'Дальневосточный федеральный округ', mapLabel: 'Дальневосточный ФО' },
];

const REGION_META_BY_CODE = Object.fromEntries(REGION_META.map((item) => [item.code, item]));

const REGION_LABEL_POINTS_LONLAT = {
  northwestern_fd: [42.5, 63.7],
  ural_fd: [67.5, 59.6],
  siberian_fd: [91.2, 59.5],
  far_eastern_fd: [135.0, 62.7],
};

const REGION_FOCUS_CONFIG = {
  central_fd: { zoom: 5.6 },
  northwestern_fd: { centerLonLat: [43.0, 63.0], zoom: 4.9 },
  south_fd: { zoom: 5.8 },
  north_caucasus_fd: { zoom: 6.1 },
  volga_fd: { zoom: 5.3 },
  ural_fd: { centerLonLat: [67.0, 59.5], zoom: 5.0 },
  siberian_fd: { centerLonLat: [92.0, 60.0], zoom: 4.8 },
  far_eastern_fd: { centerLonLat: [135.0, 61.5], zoom: 4.4 },
};

const DEFAULT_UPDATE_STATE = {
  status: 'not_checked',
  message: 'Проверка обновлений ещё не выполнялась',
  can_update: false,
};

function formatFieldValue(value) {
  if (value === null || value === undefined) return 'не указано';
  if (typeof value === 'string' && value.trim() === '') return 'не указано';
  return value;
}

function formatRouteTitle(route) {
  if (!route) return 'Маршрут не выбран';
  if (route.train_number && route.route_name) return `№ ${route.train_number} · ${route.route_name}`;
  if (route.train_number) return `№ ${route.train_number}`;
  return route.route_name || 'Без названия';
}

function formatRouteDirection(route) {
  if (!route) return 'не указано';
  return `${formatFieldValue(route.origin_station_name)} → ${formatFieldValue(route.destination_station_name)}`;
}

function formatRouteDate(value) {
  return value || 'не указано';
}

function buildRouteMetaLine(route) {
  const stopsCount = route?.stops_count ?? route?.total_stops_count ?? 0;
  const matchedCount = route?.matched_stops_count ?? 0;
  const unresolvedCount = route?.unresolved_stops_count ?? Math.max(0, stopsCount - matchedCount);
  return `Остановок: ${stopsCount} • Смэтчено: ${matchedCount} • Без match: ${unresolvedCount}`;
}

function getDefaultRzdDate() {
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

async function readApiError(response, fallbackMessage) {
  try {
    const data = await response.json();
    if (typeof data?.detail === 'string') return data.detail;
    if (typeof data?.message === 'string') return data.message;
    return fallbackMessage;
  } catch {
    return fallbackMessage;
  }
}

function addUniqueRegionCode(result, seen, code) {
  if (!code || seen.has(code)) return;
  seen.add(code);
  result.push(code);
}

function expandCorridorRegionCodes(regionCodes) {
  const result = [];
  const seen = new Set();

  for (const code of regionCodes || []) addUniqueRegionCode(result, seen, code);

  const codes = new Set(result);

  if (codes.has('central_fd') && codes.has('ural_fd')) addUniqueRegionCode(result, seen, 'volga_fd');
  if (codes.has('central_fd') && codes.has('siberian_fd')) {
    addUniqueRegionCode(result, seen, 'volga_fd');
    addUniqueRegionCode(result, seen, 'ural_fd');
  }
  if (codes.has('central_fd') && codes.has('far_eastern_fd')) {
    addUniqueRegionCode(result, seen, 'volga_fd');
    addUniqueRegionCode(result, seen, 'ural_fd');
    addUniqueRegionCode(result, seen, 'siberian_fd');
  }
  if (codes.has('ural_fd') && codes.has('far_eastern_fd')) addUniqueRegionCode(result, seen, 'siberian_fd');
  if (codes.has('volga_fd') && codes.has('far_eastern_fd')) {
    addUniqueRegionCode(result, seen, 'ural_fd');
    addUniqueRegionCode(result, seen, 'siberian_fd');
  }

  return result;
}

function buildVirtualScopeRegionCodes(originStation, destinationStation) {
  const result = [];
  const seen = new Set();

  for (const code of [originStation?.region_code, destinationStation?.region_code]) {
    addUniqueRegionCode(result, seen, code);
  }

  return expandCorridorRegionCodes(result);
}

function getUpdateStatusPresentation(status) {
  switch (status) {
    case 'update_available':
      return { label: 'Найдены обновления', background: '#fff7ed', border: '#fdba74', color: '#9a3412' };
    case 'up_to_date':
      return { label: 'Обновление не требуется', background: '#f0fdf4', border: '#86efac', color: '#166534' };
    case 'check_failed':
      return { label: 'Не удалось проверить', background: '#fef2f2', border: '#fca5a5', color: '#991b1b' };
    case 'running':
      return { label: 'Обновление выполняется', background: '#eff6ff', border: '#93c5fd', color: '#1d4ed8' };
    case 'finished':
      return { label: 'Обновление завершено', background: '#f0fdf4', border: '#86efac', color: '#166534' };
    case 'failed':
      return { label: 'Ошибка обновления', background: '#fef2f2', border: '#fca5a5', color: '#991b1b' };
    case 'checking':
      return { label: 'Проверка обновлений...', background: '#eff6ff', border: '#93c5fd', color: '#1d4ed8' };
    case 'starting':
      return { label: 'Запуск обновления...', background: '#eff6ff', border: '#93c5fd', color: '#1d4ed8' };
    case 'already_running':
      return { label: 'Обновление уже выполняется', background: '#eff6ff', border: '#93c5fd', color: '#1d4ed8' };
    default:
      return { label: 'Не проверялось', background: '#f8fafc', border: '#cbd5e1', color: '#475569' };
  }
}

function deriveSearchSections(items, loadedRegionCodes) {
  const selected = [];
  const other = [];
  for (const item of items) {
    if (loadedRegionCodes.includes(item.region_code)) selected.push(item);
    else other.push(item);
  }
  return { selected, other };
}

function Header({ mode }) {
  return (
    <header className="header app-header">
      <div className="app-header__title-block">
        <h1 className="app-header__title">{formatModeLabel(mode)}</h1>
      </div>
      <div className="app-header__right" />
    </header>
  );
}

function ActionButton({ children, variant = 'secondary', disabled = false, onClick, fullWidth = false, large = false }) {
  const variantStyles = {
    primary: {
      background: disabled ? '#cbd5e1' : '#1f2937',
      color: '#ffffff',
      border: '1px solid transparent',
      boxShadow: disabled ? 'none' : '0 8px 18px rgba(15, 23, 42, 0.14)',
    },
    secondary: {
      background: disabled ? '#f1f5f9' : '#ffffff',
      color: disabled ? '#94a3b8' : '#111827',
      border: disabled ? '1px solid #e2e8f0' : '1px solid rgba(148,163,184,0.45)',
      boxShadow: disabled ? 'none' : '0 4px 10px rgba(15, 23, 42, 0.06)',
    },
    success: {
      background: disabled ? '#dcfce7' : '#166534',
      color: '#ffffff',
      border: '1px solid transparent',
      boxShadow: disabled ? 'none' : '0 8px 18px rgba(22, 101, 52, 0.18)',
    },
  };

  const style = variantStyles[variant] || variantStyles.secondary;

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        borderRadius: large ? 16 : 12,
        padding: large ? '16px 22px' : '10px 14px',
        fontSize: large ? 16 : 14,
        fontWeight: 700,
        cursor: disabled ? 'default' : 'pointer',
        transition: 'all 0.18s ease',
        width: fullWidth ? '100%' : 'auto',
        ...style,
      }}
    >
      {children}
    </button>
  );
}

function LoadingOverlay({ title = 'Загрузка данных', progress, message }) {
  return (
    <div
      style={{
        position: 'absolute',
        inset: 0,
        zIndex: 40,
        background: 'rgba(255,255,255,0.82)',
        backdropFilter: 'blur(4px)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        pointerEvents: 'all',
      }}
    >
      <div
        style={{
          width: 440,
          maxWidth: 'calc(100% - 32px)',
          background: '#ffffff',
          borderRadius: 20,
          padding: 20,
          boxShadow: '0 14px 34px rgba(15, 23, 42, 0.15)',
          border: '1px solid rgba(148,163,184,0.25)',
        }}
      >
        <div style={{ fontSize: 18, fontWeight: 700, color: '#111827', marginBottom: 10 }}>{title}</div>
        <div style={{ fontSize: 14, color: '#475569', marginBottom: 14 }}>{message}</div>
        <div style={{ width: '100%', height: 10, background: '#e5e7eb', borderRadius: 999, overflow: 'hidden' }}>
          <div style={{ width: `${Math.max(0, Math.min(100, progress))}%`, height: '100%', background: '#374151', transition: 'width 220ms ease' }} />
        </div>
        <div style={{ marginTop: 10, fontSize: 13, color: '#64748b' }}>{Math.round(progress)}%</div>
      </div>
    </div>
  );
}

function UpdateStatusBox({ item }) {
  const presentation = getUpdateStatusPresentation(item?.status);
  return (
    <div style={{ background: presentation.background, border: `1px solid ${presentation.border}`, color: presentation.color, borderRadius: 14, padding: '12px 14px', marginBottom: 12 }}>
      <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 4 }}>{presentation.label}</div>
      <div style={{ fontSize: 13, lineHeight: 1.45 }}>{item?.message || 'Статус недоступен'}</div>
      {item?.error && <div style={{ fontSize: 12, lineHeight: 1.45, marginTop: 8, opacity: 0.9 }}>{item.error}</div>}
      {item?.notes && <div style={{ fontSize: 12, lineHeight: 1.45, marginTop: 8, opacity: 0.9, whiteSpace: 'pre-wrap' }}>{item.notes}</div>}
    </div>
  );
}

function UpdateStatusBadge({ item }) {
  const presentation = getUpdateStatusPresentation(item?.status);
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', padding: '4px 8px', borderRadius: 999, fontSize: 12, fontWeight: 700, background: presentation.background, border: `1px solid ${presentation.border}`, color: presentation.color, whiteSpace: 'nowrap' }}>
      {presentation.label}
    </span>
  );
}

function RegionUpdatesCompactList({ updateStates }) {
  return (
    <div style={{ display: 'grid', gap: 8, maxHeight: 220, overflowY: 'auto', paddingRight: 2 }}>
      {REGION_META.map((region) => {
        const item = updateStates[region.code] || DEFAULT_UPDATE_STATE;
        return (
          <div key={region.code} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '8px 10px', borderRadius: 12, background: '#f8fafc', border: '1px solid rgba(148,163,184,0.18)' }}>
            <div style={{ fontSize: 13, color: '#111827', lineHeight: 1.3 }}>{region.mapLabel}</div>
            <UpdateStatusBadge item={item} />
          </div>
        );
      })}
    </div>
  );
}

function RegionSelectionOverlay({
  activeRegionCode,
  pendingRegionCodes,
  regionSummaries,
  onToggleRegion,
  onLoadSelected,
  updateStates,
  onCheckRegionUpdates,
  onRunRegionUpdate,
  onCheckAllUpdates,
  onRunAllAvailableUpdates,
  checkingAllUpdates,
  runningAllUpdates,
}) {
  const regionMeta = REGION_META_BY_CODE[activeRegionCode] || null;
  const summary = activeRegionCode ? regionSummaries[activeRegionCode] : null;
  const isChecked = activeRegionCode ? pendingRegionCodes.includes(activeRegionCode) : false;
  const updateItem = activeRegionCode ? updateStates[activeRegionCode] || DEFAULT_UPDATE_STATE : DEFAULT_UPDATE_STATE;

  const updatesAvailableCount = Object.values(updateStates).filter((item) => item.status === 'update_available').length;
  const checkingFailedCount = Object.values(updateStates).filter((item) => item.status === 'check_failed').length;

  return (
    <>
      <div style={{ position: 'absolute', top: 16, right: 16, zIndex: 20, width: 400, maxWidth: 'calc(100% - 32px)', background: 'rgba(255,255,255,0.96)', backdropFilter: 'blur(8px)', border: '1px solid rgba(148,163,184,0.35)', borderRadius: 20, boxShadow: '0 14px 34px rgba(15, 23, 42, 0.12)', padding: 18 }}>
        {!regionMeta ? (
          <>
            <div style={{ fontSize: 13, color: '#64748b', marginBottom: 8 }}>Стартовый экран</div>
            <h2 style={{ margin: '0 0 10px 0', fontSize: 22, lineHeight: 1.2 }}>Выбери федеральный округ на карте</h2>
            <p style={{ margin: '0 0 14px 0', color: '#475569', lineHeight: 1.5 }}>Наведи курсор на округ, кликни по нему и отметь чекбокс в карточке.</p>
          </>
        ) : (
          <>
            <div style={{ fontSize: 13, color: '#64748b', marginBottom: 6 }}>Выбор округа</div>
            <h2 style={{ margin: '0 0 10px 0', fontSize: 22, lineHeight: 1.2 }}>{regionMeta.label}</h2>
            <div style={{ background: '#f8fafc', borderRadius: 14, padding: '12px 14px', border: '1px solid rgba(148,163,184,0.2)', marginBottom: 14 }}>
              <div style={{ fontSize: 13, color: '#64748b', marginBottom: 4 }}>Станций в округе</div>
              <div style={{ fontSize: 26, fontWeight: 700, color: '#111827' }}>{summary?.stations_count ?? 'не указано'}</div>
            </div>
            <label style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', fontSize: 15, color: '#111827', marginBottom: 18 }}>
              <input type="checkbox" checked={isChecked} onChange={() => onToggleRegion(activeRegionCode)} />
              <span>Добавить округ к загрузке</span>
            </label>
            <section style={{ borderTop: '1px solid rgba(148,163,184,0.25)', paddingTop: 16 }}>
              <div style={{ fontSize: 14, fontWeight: 700, color: '#111827', marginBottom: 10 }}>Обновление данных</div>
              <UpdateStatusBox item={updateItem} />
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                <ActionButton variant="secondary" onClick={() => onCheckRegionUpdates(activeRegionCode)} disabled={['checking', 'starting', 'running'].includes(updateItem.status)}>
                  Проверить обновления
                </ActionButton>
                {updateItem.status === 'update_available' && (
                  <ActionButton variant="primary" onClick={() => onRunRegionUpdate(activeRegionCode)} disabled={updateItem.status === 'running' || updateItem.status === 'starting'}>
                    Обновить округ
                  </ActionButton>
                )}
              </div>
            </section>
          </>
        )}

        <section style={{ borderTop: '1px solid rgba(148,163,184,0.25)', paddingTop: 16, marginTop: 18 }}>
          <div style={{ fontSize: 14, fontWeight: 700, color: '#111827', marginBottom: 10 }}>Обновления по всем округам</div>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
            <ActionButton variant="secondary" onClick={onCheckAllUpdates} disabled={checkingAllUpdates || runningAllUpdates}>
              {checkingAllUpdates ? 'Проверка...' : 'Проверить наличие обновлений для всех округов'}
            </ActionButton>
            {updatesAvailableCount > 0 && (
              <ActionButton variant="primary" onClick={onRunAllAvailableUpdates} disabled={runningAllUpdates}>
                {runningAllUpdates ? 'Запуск...' : `Обновить все округа с найденными обновлениями (${updatesAvailableCount})`}
              </ActionButton>
            )}
          </div>
          <div style={{ fontSize: 13, color: '#64748b', lineHeight: 1.5, marginBottom: 12 }}>
            Найдено округов с обновлениями: {updatesAvailableCount}<br />Ошибок проверки: {checkingFailedCount}
          </div>
          <RegionUpdatesCompactList updateStates={updateStates} />
        </section>
      </div>

      {pendingRegionCodes.length > 0 && (
        <div style={{ position: 'absolute', left: '50%', bottom: 18, transform: 'translateX(-50%)', zIndex: 20, width: 'min(520px, calc(100% - 32px))' }}>
          <ActionButton variant="primary" onClick={onLoadSelected} fullWidth large>
            Загрузить выбранное ({pendingRegionCodes.length})
          </ActionButton>
        </div>
      )}
    </>
  );
}

function SidebarModeSwitch({ panelMode, setPanelMode }) {
  return (
    <section className="card sidebar-mode-card">
      <div className="panel-mode-switch">
        <button className={panelMode === 'infrastructure' ? 'panel-mode-button active' : 'panel-mode-button'} onClick={() => setPanelMode('infrastructure')}>Инфра.</button>
        <button className={panelMode === 'routes' ? 'panel-mode-button active' : 'panel-mode-button'} onClick={() => setPanelMode('routes')}>РЖД</button>
        <button className={panelMode === 'virtual' ? 'panel-mode-button active' : 'panel-mode-button'} onClick={() => setPanelMode('virtual')}>Вирт. маршрут</button>
      </div>
    </section>
  );
}

function StationCard({ station }) {
  const branch = station?.railway_branch ?? station?.branch ?? station?.operator_branch;
  const stationType = station?.station_type ?? station?.type;

  if (!station) {
    return <p>Выбери станцию на карте или через поиск.</p>;
  }

  return (
    <div className="station-card">
      <h2 className="station-card__title">Карточка станции</h2>

      <div className="station-card__row"><span className="station-card__label">ID:</span><span>{station.id}</span></div>
      <div className="station-card__row"><span className="station-card__label">Округ:</span><span>{formatRegionCode(station.region_code)}</span></div>
      <div className="station-card__row"><span className="station-card__label">Название:</span><span>{station.name || 'не указано'}</span></div>
      <div className="station-card__row"><span className="station-card__label">Тип:</span><span>{formatStationType(stationType)}</span></div>
      <div className="station-card__row"><span className="station-card__label">Главная станция:</span><span>{station.is_main_rail_station ? 'да' : 'нет'}</span></div>

      {station.operator_name || station.operator ? (
        <div className="station-card__row"><span className="station-card__label">Оператор:</span><span>{station.operator_name || station.operator}</span></div>
      ) : null}
      {branch ? <div className="station-card__row"><span className="station-card__label">Филиал:</span><span>{branch}</span></div> : null}
      {station.uic_ref ? <div className="station-card__row"><span className="station-card__label">UIC:</span><span>{station.uic_ref}</span></div> : null}
      {station.esr_user ? <div className="station-card__row"><span className="station-card__label">ESR:</span><span>{station.esr_user}</span></div> : null}

      {station.lat != null && station.lon != null && (
        <div className="station-card__row">
          <span className="station-card__label">Координаты:</span>
          <span>{Number(station.lat).toFixed(6)}, {Number(station.lon).toFixed(6)}</span>
        </div>
      )}
    </div>
  );
}

function InfrastructureSidebar({ loading, error, stations, linesCount, selectedStation, onSelectStation, loadedRegionCodes, showServiceLines, setShowServiceLines, onBackToSelection }) {
  return (
    <>
      <section className="card">
        <h2>Загруженные округа</h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
          {loadedRegionCodes.map((code) => (
            <span key={code} style={{ display: 'inline-flex', alignItems: 'center', padding: '6px 10px', borderRadius: 999, background: '#e5e7eb', color: '#111827', fontSize: 13, fontWeight: 600 }}>
              {formatRegionShortCode(code)}
            </span>
          ))}
        </div>
        <button onClick={onBackToSelection}>Изменить выбор округов</button>
      </section>

      <section className="card">
        <h2>Слои</h2>
        <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
          <input type="checkbox" checked={showServiceLines} onChange={(e) => setShowServiceLines(e.target.checked)} />
          <span>Показывать специальные / служебные пути</span>
        </label>
      </section>

      <section className="card">
        <h2>Поиск станции</h2>
        <StationSearchSelect
          placeholder="Введите название станции"
          selectedStation={selectedStation}
          onSelect={(station) => onSelectStation(station.id)}
        />
      </section>

      <section className="card">
        <h2>Статус данных</h2>
        <div className="data-status-row"><span>Станций загружено:</span><strong>{stations.length}</strong></div>
        <div className="data-status-row"><span>Линий загружено:</span><strong>{linesCount}</strong></div>
        {loading && <p>Загрузка...</p>}
        {error && <p className="error-text">Ошибка: {error}</p>}
      </section>

      <section className="card">
        <StationCard station={selectedStation} />
      </section>
    </>
  );
}

function RouteDetailsCard({ selectedRoute, routesLoading, onClearRoute, onSelectStation }) {
  return (
    <section className="card route-details-card">
      <h2>Карточка маршрута</h2>
      {!selectedRoute ? (
        <p>Выбери или импортируй маршрут, чтобы увидеть его остановки и отрисовку на карте.</p>
      ) : (
        <>
          <div className="route-details-header">
            <div>
              <div className="route-details-title">{formatRouteTitle(selectedRoute)}</div>
              <div className="route-details-direction">{formatRouteDirection(selectedRoute)}</div>
            </div>
            <button className="subtle-button" onClick={onClearRoute}>Снять выбор</button>
          </div>

          <div className="route-details-grid">
            <div className="route-chip"><span>Дата</span><strong>{formatRouteDate(selectedRoute.snapshot_date)}</strong></div>
            <div className="route-chip"><span>Остановок</span><strong>{selectedRoute.stops_count ?? selectedRoute.stops?.length ?? 0}</strong></div>
            <div className="route-chip"><span>Смэтчено</span><strong>{selectedRoute.matched_stops_count ?? 0}</strong></div>
            <div className="route-chip"><span>Без match</span><strong>{selectedRoute.unresolved_stops_count ?? 0}</strong></div>
          </div>

          {selectedRoute.notes && <div className="route-notes"><strong>Примечание:</strong> {selectedRoute.notes}</div>}

          <div className="route-stops-block">
            <div className="route-stops-title">Остановки маршрута</div>
            {routesLoading ? (
              <p>Загрузка маршрута...</p>
            ) : !selectedRoute.stops || selectedRoute.stops.length === 0 ? (
              <p>Остановки отсутствуют.</p>
            ) : (
              <div className="route-stop-list" style={{ maxHeight: 420, overflowY: 'auto', paddingRight: 4 }}>
                {selectedRoute.stops.map((stop) => {
                  const isMatched = Boolean(stop.station_id);
                  return (
                    <button
                      key={`${selectedRoute.id}-${stop.stop_sequence}`}
                      className={isMatched ? 'route-stop-item matched' : 'route-stop-item unresolved'}
                      onClick={() => stop.station_id && onSelectStation(stop.station_id)}
                      disabled={!stop.station_id}
                    >
                      <div className="route-stop-sequence">{stop.stop_sequence}</div>
                      <div className="route-stop-content">
                        <div className="route-stop-name">{stop.station_name_raw || stop.station_name_matched || 'Без названия'}</div>
                        <div className="route-stop-meta">{stop.station_code_rzd || 'без кода'} • {stop.arrival_time || '—'} / {stop.departure_time || '—'}</div>
                        <div className="route-stop-status">{isMatched ? `Связано с OSM: ${stop.station_name_matched || `station_id=${stop.station_id}`}` : 'Пока без связи с stations'}</div>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        </>
      )}
    </section>
  );
}

function RzdRouteSearchCard({
  rzdOriginStation,
  rzdDestinationStation,
  onSelectRzdOrigin,
  onSelectRzdDestination,
  rzdTrains,
  rzdSearchLoading,
  rzdImportLoading,
  rzdError,
  rzdMessage,
  rzdCalendarDebug,
  onSearchRzdRoutes,
  onImportRzdTrain,
  rzdSearchProgress,
  rzdSearchProgressMessage,
}) {
  const canSearch = Boolean(rzdOriginStation?.id) && Boolean(rzdDestinationStation?.id) && !rzdSearchLoading;

  return (
    <section className="card">
      <h2>Поиск реального поезда А→Б</h2>
      <p>Выбери станцию отправления и назначения через поиск. Поезд между ними проверяется через РЖД API.</p>

      <div style={{ display: 'grid', gap: 12, marginBottom: 14 }}>
        <StationSearchSelect
          label="Откуда"
          placeholder="Введите станцию отправления"
          selectedStation={rzdOriginStation}
          onSelect={onSelectRzdOrigin}
        />

        <StationSearchSelect
          label="Куда"
          placeholder="Введите станцию назначения"
          selectedStation={rzdDestinationStation}
          onSelect={onSelectRzdDestination}
        />
      </div>

      <div style={{ marginBottom: 14, fontSize: 13, color: '#64748b', background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 12, padding: 10 }}>
        Система проверит ближайшие 2 дня и покажет даты, на которые найдены поезда.
      </div>

      <ActionButton variant="primary" fullWidth onClick={onSearchRzdRoutes} disabled={!canSearch}>
        {rzdSearchLoading ? 'Ищем поезда...' : 'Найти поезда'}
      </ActionButton>

      {rzdSearchLoading && (
        <div style={{ marginTop: 12, background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 12, padding: 10 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 12, color: '#475569', marginBottom: 7 }}>
            <span>{rzdSearchProgressMessage || 'Идёт поиск...'}</span>
            <strong>{Math.round(rzdSearchProgress || 0)}%</strong>
          </div>
          <div style={{ width: '100%', height: 7, background: '#e5e7eb', borderRadius: 999, overflow: 'hidden' }}>
            <div style={{ width: `${Math.max(0, Math.min(100, rzdSearchProgress || 0))}%`, height: '100%', background: '#374151', transition: 'width 240ms ease' }} />
          </div>
        </div>
      )}

      {rzdError && <div style={{ marginTop: 12, fontSize: 13, color: '#991b1b', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 12, padding: 10 }}>{rzdError}</div>}
      {rzdMessage && !rzdError && <div style={{ marginTop: 12, fontSize: 13, color: '#475569', background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 12, padding: 10 }}>{rzdMessage}</div>}

      {rzdCalendarDebug?.fallback_message && (
        <div style={{ marginTop: 12, fontSize: 13, color: '#92400e', background: '#fffbeb', border: '1px solid #fcd34d', borderRadius: 12, padding: 10, lineHeight: 1.45 }}>
          {rzdCalendarDebug.fallback_message}
        </div>
      )}

      {rzdCalendarDebug?.date_summaries?.length > 0 && (
        <div style={{ marginTop: 12, background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 12, padding: 10 }}>
          <div style={{ fontSize: 13, fontWeight: 800, color: '#334155', marginBottom: 8 }}>Проверенные даты</div>
          <div style={{ display: 'grid', gap: 6, maxHeight: 180, overflowY: 'auto' }}>
            {rzdCalendarDebug.date_summaries.map((item) => (
              <div key={item.date} style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 12, color: item.status === 'failed' ? '#991b1b' : '#475569', background: item.trains_count > 0 ? '#f0fdf4' : '#ffffff', border: item.status === 'failed' ? '1px solid #fecaca' : '1px solid #e2e8f0', borderRadius: 10, padding: '7px 8px' }} title={item.error || ''}>
                <span>{item.date_rzd || item.date}</span>
                <strong>{item.status === 'failed' ? 'ошибка' : `${item.trains_count || 0} поездов`}</strong>
              </div>
            ))}
          </div>
        </div>
      )}

      {rzdTrains.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#475569', marginBottom: 8, textTransform: 'uppercase', letterSpacing: '0.04em' }}>Найденные поезда</div>
          <div className="rzd-train-list" style={{ display: 'grid', gap: 8, maxHeight: 360, overflowY: 'auto', paddingRight: 4 }}>
            {rzdTrains.map((train, index) => (
              <div key={`${train.train_number}-${train.search_date || train.departure_date}-${train.departure_time}-${index}`} style={{ border: '1px solid rgba(148,163,184,0.35)', borderRadius: 14, padding: 12, background: '#ffffff' }}>
                <div style={{ fontSize: 16, fontWeight: 800, color: '#111827' }}>№ {train.train_number}{train.brand ? ` · ${train.brand}` : ''}</div>
                <div style={{ fontSize: 13, color: '#475569', marginTop: 4 }}>{train.origin_name || 'откуда не указано'} → {train.destination_name || 'куда не указано'}</div>
                <div style={{ fontSize: 13, color: '#64748b', marginTop: 4 }}>
                  {train.search_date_rzd || train.search_date || train.departure_date || 'дата не указана'} • {train.departure_time || '—'} → {train.arrival_time || '—'}{train.time_in_way ? ` • в пути ${train.time_in_way}` : ''}
                </div>
                {train.similar_used && (
                  <div style={{ marginTop: 6, fontSize: 12, color: '#92400e', background: '#fffbeb', border: '1px solid #fde68a', borderRadius: 10, padding: 8, lineHeight: 1.4 }}>
                    Найдено через похожие станции: {train.used_origin_station_name || 'станция отправления'} → {train.used_destination_station_name || 'станция назначения'}
                  </div>
                )}
                <div style={{ marginTop: 10 }}>
                  <ActionButton variant="success" onClick={() => onImportRzdTrain(train)} disabled={rzdImportLoading}>
                    {rzdImportLoading ? 'Импортируем...' : 'Показать на карте'}
                  </ActionButton>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function RoutesSidebar(props) {
  return (
    <>
      <RzdRouteSearchCard {...props} />
      <RouteDetailsCard
        selectedRoute={props.selectedRoute}
        routesLoading={props.routesLoading}
        onClearRoute={props.onClearRoute}
        onSelectStation={props.onSelectStation}
      />
    </>
  );
}

function VirtualRoutesSidebar({
  virtualOriginStation,
  virtualDestinationStation,
  onSelectVirtualOrigin,
  onSelectVirtualDestination,
  onBuildVirtualRoute,
  virtualRouteLoading,
  virtualRouteError,
  virtualRouteMessage,
  topologyProgress,
  topologyProgressMessage,
}) {
  return (
    <>
      <section className="card">
        <h2>Виртуальный маршрут по OSM</h2>
        <p>Этот режим строит теоретический путь по OSM topology graph. Это не расписание РЖД.</p>
      </section>

      <section className="card">
        <h2>Точки маршрута</h2>
        <div style={{ display: 'grid', gap: 12, marginBottom: 14 }}>
          <StationSearchSelect
            label="Станция А"
            placeholder="Введите начальную станцию"
            selectedStation={virtualOriginStation}
            onSelect={onSelectVirtualOrigin}
          />

          <StationSearchSelect
            label="Станция Б"
            placeholder="Введите конечную станцию"
            selectedStation={virtualDestinationStation}
            onSelect={onSelectVirtualDestination}
          />
        </div>

        <ActionButton variant="primary" fullWidth onClick={onBuildVirtualRoute} disabled={virtualRouteLoading || !virtualOriginStation?.id || !virtualDestinationStation?.id}>
          {virtualRouteLoading ? 'Строим виртуальный путь...' : 'Построить виртуальный путь по OSM'}
        </ActionButton>

        {topologyProgress > 0 && (
          <div style={{ marginTop: 12, background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 12, padding: 10 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8, fontSize: 12, color: '#475569', marginBottom: 7 }}>
              <span>{topologyProgressMessage || 'Подготавливаем topology graph...'}</span>
              <strong>{Math.round(topologyProgress)}%</strong>
            </div>
            <div style={{ width: '100%', height: 7, background: '#e5e7eb', borderRadius: 999, overflow: 'hidden' }}>
              <div style={{ width: `${Math.max(0, Math.min(100, topologyProgress))}%`, height: '100%', background: '#7c3aed', transition: 'width 240ms ease' }} />
            </div>
          </div>
        )}

        {virtualRouteError && <div style={{ marginTop: 12, fontSize: 13, color: '#991b1b', background: '#fef2f2', border: '1px solid #fecaca', borderRadius: 12, padding: 10 }}>{virtualRouteError}</div>}
        {virtualRouteMessage && !virtualRouteError && <div style={{ marginTop: 12, fontSize: 13, color: '#475569', background: '#f8fafc', border: '1px solid #e2e8f0', borderRadius: 12, padding: 10 }}>{virtualRouteMessage}</div>}
      </section>
    </>
  );
}

function LoadedSidebar({ panelMode, setPanelMode, ...props }) {
  return (
    <aside className="sidebar">
      <SidebarModeSwitch panelMode={panelMode} setPanelMode={setPanelMode} />
      {panelMode === 'infrastructure' ? (
        <InfrastructureSidebar {...props} />
      ) : panelMode === 'routes' ? (
        <RoutesSidebar {...props} />
      ) : (
        <VirtualRoutesSidebar {...props} />
      )}
    </aside>
  );
}

function createStationFeatures(stations, selectedStation) {
  const combinedStations = [...stations];
  if (selectedStation && Number.isFinite(selectedStation.lon) && Number.isFinite(selectedStation.lat) && !combinedStations.some((item) => item.id === selectedStation.id)) {
    combinedStations.push(selectedStation);
  }

  return combinedStations
    .filter((station) => Number.isFinite(Number(station.lon)) && Number.isFinite(Number(station.lat)))
    .map((station) => {
      const feature = new Feature({
        geometry: new Point(fromLonLat([Number(station.lon), Number(station.lat)])),
        stationId: station.id,
        name: station.name,
        station_type: station.station_type,
        region_code: station.region_code,
        is_main_rail_station: Boolean(station.is_main_rail_station),
        is_visible_default: station.is_visible_default,
        exclude_reason: station.exclude_reason,
        esr_user: station.esr_user,
        uic_ref: station.uic_ref,
      });
      feature.setId(station.id);
      return feature;
    });
}

function normalizeGeoJsonGeometry(input) {
  if (!input) return null;

  let value = input;

  if (typeof value === 'string') {
    try {
      value = JSON.parse(value);
    } catch {
      return null;
    }
  }

  if (!value || typeof value !== 'object') return null;

  if (value.type === 'Feature') {
    return normalizeGeoJsonGeometry(value.geometry);
  }

  if (value.type === 'FeatureCollection') {
    const firstFeature = Array.isArray(value.features)
      ? value.features.find((feature) => feature?.geometry)
      : null;
    return normalizeGeoJsonGeometry(firstFeature?.geometry);
  }

  const geometryTypes = new Set([
    'Point',
    'MultiPoint',
    'LineString',
    'MultiLineString',
    'Polygon',
    'MultiPolygon',
    'GeometryCollection',
  ]);

  if (!geometryTypes.has(value.type)) return null;

  return value;
}

function createLineFeatures(lines) {
  const format = new GeoJSON();

  return lines.flatMap((line) => {
    try {
      const geometryObject = normalizeGeoJsonGeometry(line.geometry);

      if (!geometryObject) {
        return [];
      }

      const features = format.readFeatures(
        {
          type: 'FeatureCollection',
          features: [
            {
              type: 'Feature',
              geometry: geometryObject,
              properties: {
                id: line.id,
                region_code: line.region_code,
                name: line.name,
                line_type: line.line_type,
                usage_type: line.usage_type,
                service_type: line.service_type,
                is_service_line: Boolean(line.is_service_line),
                is_main_passenger_line: Boolean(line.is_main_passenger_line),
                is_visible_default: line.is_visible_default,
                exclude_reason: line.exclude_reason,
              },
            },
          ],
        },
        { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }
      );

      return features.filter((feature) => {
        const geometry = feature?.getGeometry?.();
        return geometry && typeof geometry.getExtent === 'function';
      });
    } catch (error) {
      console.error('Ошибка разбора геометрии линии:', line, error);
      return [];
    }
  });
}

function createRouteGeometryFeature(geometry, geometrySource = null, extraProperties = {}) {
  const geometryObject = normalizeGeoJsonGeometry(geometry);
  if (!geometryObject) return null;
  try {
    const format = new GeoJSON();
    return format.readFeature(
      { type: 'Feature', geometry: geometryObject, properties: { geometry_source: geometrySource, ...extraProperties } },
      { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }
    );
  } catch (error) {
    console.error('Ошибка построения route geometry feature:', error);
    return null;
  }
}

function createRouteNetworkSegmentFeatures(networkSegments) {
  const format = new GeoJSON();
  return (networkSegments || []).flatMap((segment, index) => {
    try {
      const geometryObject = normalizeGeoJsonGeometry(segment.geometry);
      if (!geometryObject) return [];
      const features = format.readFeatures(
        {
          type: 'FeatureCollection',
          features: [
            {
              type: 'Feature',
              geometry: geometryObject,
              properties: {
                segment_index: segment.segment_index,
                edge_index: segment.edge_index,
                line_id: segment.line_id,
                part_index: segment.part_index,
                length_km: segment.length_km,
                segment_source: segment.segment_source,
              },
            },
          ],
        },
        { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }
      );
      for (const feature of features) feature.setId(`route-network-segment-${index}`);
      return features;
    } catch (error) {
      console.error('Ошибка разбора route network segment:', segment, error);
      return [];
    }
  });
}

function createRouteStopFeatures(stops) {
  return (stops || [])
    .filter((stop) => Number.isFinite(Number(stop.lon)) && Number.isFinite(Number(stop.lat)))
    .map((stop) => {
      const feature = new Feature({
        geometry: new Point(fromLonLat([Number(stop.lon), Number(stop.lat)])),
        stationId: stop.station_id || null,
        routeStopSequence: stop.stop_sequence,
        routeStopKind: stop.is_origin ? 'origin' : stop.is_destination ? 'destination' : 'intermediate',
      });
      feature.setId(`route-stop-${stop.stop_sequence}`);
      return feature;
    });
}

function getPointCoordinatesFromGeometry(value) {
  const geometry = normalizeGeoJsonGeometry(value);

  if (!geometry) return null;

  if (geometry.type === 'Point' && Array.isArray(geometry.coordinates)) {
    return geometry.coordinates;
  }

  if (geometry.type === 'MultiPoint' && Array.isArray(geometry.coordinates?.[0])) {
    return geometry.coordinates[0];
  }

  return null;
}

function isValidOlFeature(feature) {
  const geometry = feature?.getGeometry?.();
  return Boolean(geometry && typeof geometry.getExtent === 'function');
}

function addValidFeature(source, feature) {
  if (!source || !isValidOlFeature(feature)) return;
  source.addFeature(feature);
}

function addValidFeatures(source, features) {
  if (!source || !Array.isArray(features) || features.length === 0) return;

  const validFeatures = features.filter(isValidOlFeature);
  if (validFeatures.length > 0) source.addFeatures(validFeatures);
}

function createAnalyticsSettlementFeatures(settlements) {
  const format = new GeoJSON();
  return (settlements || []).flatMap((settlement) => {
    try {
      const geometryObject = normalizeGeoJsonGeometry(settlement.geometry);
      if (!geometryObject) return [];
      const population = Number(settlement.population || 0);
      const score = Number(settlement.score || 0);
      const weight = Math.max(0.05, Math.min(1, Math.max(score / 100, Math.log10(Math.max(10, population)) / 7)));
      const features = format.readFeatures(
        {
          type: 'FeatureCollection',
          features: [
            {
              type: 'Feature',
              geometry: geometryObject,
              properties: {
                analyticsSettlementId: settlement.id,
                settlement,
                score: settlement.score,
                weight,
                attention_level: settlement.attention_level,
                served: settlement.served,
              },
            },
          ],
        },
        { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }
      );
      for (const feature of features) feature.setId(`analytics-settlement-${settlement.id}`);
      return features;
    } catch (error) {
      console.error('Ошибка разбора analytics settlement:', settlement, error);
      return [];
    }
  });
}

function createAnalyticsVirtualStationFeature(candidate) {
  const geometryObject = normalizeGeoJsonGeometry(candidate?.virtual_station?.geometry);
  if (!geometryObject) return null;
  try {
    const format = new GeoJSON();
    const feature = format.readFeature(
      { type: 'Feature', geometry: geometryObject, properties: { analyticsVirtualStation: true, candidate } },
      { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }
    );
    feature.setId(`analytics-virtual-station-${candidate.id}`);
    return feature;
  } catch (error) {
    console.error('Ошибка разбора virtual station:', candidate, error);
    return null;
  }
}

function createAnalyticsConnectionFeature(candidate) {
  const settlementCoords = getPointCoordinatesFromGeometry(candidate?.geometry);
  const virtualStationCoords = getPointCoordinatesFromGeometry(candidate?.virtual_station?.geometry);

  if (!settlementCoords || !virtualStationCoords) return null;

  const feature = new Feature({
    geometry: new LineString([fromLonLat(settlementCoords), fromLonLat(virtualStationCoords)]),
    analyticsConnection: true,
    candidate,
  });

  feature.setId(`analytics-connection-${candidate.id}`);
  return feature;
}

function createAnalysisRouteFeatures(analysisRoutes) {
  const format = new GeoJSON();
  return (analysisRoutes || []).flatMap((route) => {
    try {
      const geometryObject = normalizeGeoJsonGeometry(route.geometry);
      if (!geometryObject) return [];
      const features = format.readFeatures(
        {
          type: 'FeatureCollection',
          features: [
            {
              type: 'Feature',
              geometry: geometryObject,
              properties: {
                analysis_route_id: route.id,
                analysis_route_kind: route.kind,
                analysis_route_color: route.color,
                analysis_route_label: route.label,
                analysis_route_length_km: route.lengthKm,
              },
            },
          ],
        },
        { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' }
      );
      for (const feature of features) feature.setId(`analysis-route-${route.id}`);
      return features;
    } catch (error) {
      console.error('Ошибка разбора analysis route:', route, error);
      return [];
    }
  });
}

function extractHeatmapSettlements(payload) {
  if (!payload) return [];
  if (Array.isArray(payload)) return payload;
  return payload.settlements || payload.heatmap_points || payload.items || payload.results || payload.objects || payload.points || [];
}

function makeAnalysisRouteStyle(selectedAnalysisRouteId) {
  return (feature) => {
    const routeId = feature.get('analysis_route_id');
    const kind = feature.get('analysis_route_kind');
    const color = feature.get('analysis_route_color') || '#2563eb';
    const selected = routeId === selectedAnalysisRouteId;

    return [
      new Style({ stroke: new Stroke({ color: 'rgba(255,255,255,0.96)', width: selected ? 11 : 8 }), zIndex: selected ? 122 : kind === 'original' ? 118 : 116 }),
      new Style({ stroke: new Stroke({ color, width: selected ? 7 : 4, lineDash: kind === 'alternative' ? [12, 8] : undefined }), zIndex: selected ? 123 : kind === 'original' ? 119 : 117 }),
    ];
  };
}

function buildDistrictLabelFeatures(federalDistrictsData) {
  if (!federalDistrictsData) return [];
  const format = new GeoJSON();
  const districtFeatures = format.readFeatures(federalDistrictsData, { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' });
  return districtFeatures.map((feature) => {
    const code = feature.get('code');
    const manualLonLat = REGION_LABEL_POINTS_LONLAT[code];
    const coordinate = manualLonLat ? fromLonLat(manualLonLat) : getCenter(feature.getGeometry().getExtent());
    return new Feature({ geometry: new Point(coordinate), code, mapLabel: REGION_META_BY_CODE[code]?.mapLabel || feature.get('name') || code });
  });
}

function buildCombinedExtentForRegions(federalDistrictsData, regionCodes) {
  if (!federalDistrictsData || regionCodes.length === 0) return null;
  const format = new GeoJSON();
  const districtFeatures = format.readFeatures(federalDistrictsData, { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' });
  const selectedFeatures = districtFeatures.filter((feature) => regionCodes.includes(feature.get('code')));
  if (selectedFeatures.length === 0) return null;

  let combinedExtent = selectedFeatures[0].getGeometry().getExtent().slice();
  for (let i = 1; i < selectedFeatures.length; i += 1) {
    const extent = selectedFeatures[i].getGeometry().getExtent();
    combinedExtent[0] = Math.min(combinedExtent[0], extent[0]);
    combinedExtent[1] = Math.min(combinedExtent[1], extent[1]);
    combinedExtent[2] = Math.max(combinedExtent[2], extent[2]);
    combinedExtent[3] = Math.max(combinedExtent[3], extent[3]);
  }
  return combinedExtent;
}

function getDistrictFeatureByCode(federalDistrictsData, regionCode) {
  if (!federalDistrictsData || !regionCode) return null;
  const format = new GeoJSON();
  const districtFeatures = format.readFeatures(federalDistrictsData, { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' });
  return districtFeatures.find((feature) => feature.get('code') === regionCode) || null;
}

function MapPanel({
  federalDistrictsData,
  russiaFeatureData,
  selectionMode,
  activeRegionCode,
  pendingRegionCodes,
  loadedRegionCodes,
  onActiveRegionChange,
  stations,
  lines,
  selectedStation,
  onSelectStation,
  selectedRoute,
  loading,
  mapMode,
  analyticsResult,
  selectedAnalyticsCandidate,
  onSelectAnalyticsCandidate,
  showAnalyticsHeatmap,
  showAnalyticsPoints,
  showAlternatives,
  analyticsParams,
  heatmapData,
  heatmapSettlements,
  analysisRoutes,
  selectedAnalysisRouteId,
  onSelectAnalysisRoute,
}) {
  const mapElementRef = useRef(null);
  const mapRef = useRef(null);

  const districtLayerRef = useRef(null);
  const districtLabelLayerRef = useRef(null);
  const russiaBoundaryLayerRef = useRef(null);
  const stationLayerRef = useRef(null);
  const lineLayerRef = useRef(null);
  const routeLineLayerRef = useRef(null);
  const routeNetworkSegmentsLayerRef = useRef(null);
  const routeStopsLayerRef = useRef(null);
  const analyticsSettlementsLayerRef = useRef(null);
  const analyticsHeatmapLayerRef = useRef(null);
  const analyticsVirtualStationLayerRef = useRef(null);
  const analyticsConnectionLayerRef = useRef(null);
  const analyticsRoutesLayerRef = useRef(null);

  const districtSourceRef = useRef(null);
  const districtLabelSourceRef = useRef(null);
  const russiaBoundarySourceRef = useRef(null);
  const stationSourceRef = useRef(null);
  const lineSourceRef = useRef(null);
  const routeLineSourceRef = useRef(null);
  const routeNetworkSegmentsSourceRef = useRef(null);
  const routeStopsSourceRef = useRef(null);
  const analyticsSettlementsSourceRef = useRef(null);
  const analyticsHeatmapSourceRef = useRef(null);
  const analyticsVirtualStationSourceRef = useRef(null);
  const analyticsConnectionSourceRef = useRef(null);
  const analyticsRoutesSourceRef = useRef(null);

  const hoverRegionCodeRef = useRef(null);
  const activeRegionCodeRef = useRef(activeRegionCode);
  const pendingRegionCodesRef = useRef(pendingRegionCodes);
  const loadedRegionCodesRef = useRef(loadedRegionCodes);
  const selectionModeRef = useRef(selectionMode);
  const selectedStationIdRef = useRef(selectedStation?.id ?? null);
  const mapModeRef = useRef(mapMode);
  const onSelectAnalyticsCandidateRef = useRef(onSelectAnalyticsCandidate);
  const selectedAnalyticsCandidateRef = useRef(selectedAnalyticsCandidate);
  const selectedAnalysisRouteIdRef = useRef(selectedAnalysisRouteId);
  const onSelectAnalysisRouteRef = useRef(onSelectAnalysisRoute);

  const districtStyleFunction = useCallback((feature) => {
    const code = feature.get('code');
    const isHovered = hoverRegionCodeRef.current === code;
    const isActive = activeRegionCodeRef.current === code;
    const isPending = pendingRegionCodesRef.current.includes(code);
    const isLoaded = loadedRegionCodesRef.current.includes(code);
    const isSelectionMode = selectionModeRef.current;

    let strokeColor = '#6b7280';
    let strokeWidth = 1.8;
    let fillColor = 'rgba(0,0,0,0)';

    if (isSelectionMode) {
      if (isPending || isActive) {
        strokeColor = '#374151';
        strokeWidth = 2.8;
        fillColor = 'rgba(55, 65, 81, 0.16)';
      } else if (isHovered) {
        strokeColor = '#4b5563';
        strokeWidth = 2.4;
        fillColor = 'rgba(75, 85, 99, 0.10)';
      } else {
        strokeColor = '#6b7280';
        strokeWidth = 1.6;
      }
    } else if (!isLoaded) {
      strokeColor = '#9ca3af';
      strokeWidth = 1.2;
    }

    return new Style({ stroke: new Stroke({ color: strokeColor, width: strokeWidth }), fill: new Fill({ color: fillColor }), zIndex: isHovered || isActive || isPending ? 12 : 8 });
  }, []);

  const districtLabelStyleFunction = useCallback((feature) => {
    if (!selectionModeRef.current) return null;
    const code = feature.get('code');
    const fontSize = code === 'far_eastern_fd' ? 12 : 13;
    return new Style({
      text: new Text({
        text: feature.get('mapLabel'),
        font: `600 ${fontSize}px Inter, Arial, sans-serif`,
        fill: new Fill({ color: '#1f2937' }),
        backgroundFill: new Fill({ color: 'rgba(255,255,255,0.78)' }),
        padding: [3, 5, 3, 5],
        overflow: true,
      }),
      zIndex: 30,
    });
  }, []);

  const routeStopsStyleFunction = useCallback((feature) => {
    const kind = feature.get('routeStopKind');
    const radius = kind === 'origin' || kind === 'destination' ? 6.5 : 4.5;
    return new Style({ image: new CircleStyle({ radius, fill: new Fill({ color: '#15803d' }), stroke: new Stroke({ color: '#ffffff', width: 1.4 }) }), zIndex: 90 });
  }, []);

  useEffect(() => { activeRegionCodeRef.current = activeRegionCode; districtLayerRef.current?.changed(); }, [activeRegionCode]);
  useEffect(() => { pendingRegionCodesRef.current = pendingRegionCodes; districtLayerRef.current?.changed(); }, [pendingRegionCodes]);
  useEffect(() => { loadedRegionCodesRef.current = loadedRegionCodes; districtLayerRef.current?.changed(); }, [loadedRegionCodes]);
  useEffect(() => { selectionModeRef.current = selectionMode; districtLayerRef.current?.changed(); districtLabelLayerRef.current?.setVisible(selectionMode); russiaBoundaryLayerRef.current?.changed(); }, [selectionMode]);
  useEffect(() => { selectedStationIdRef.current = selectedStation?.id ?? null; stationLayerRef.current?.changed(); }, [selectedStation]);
  useEffect(() => { onSelectAnalyticsCandidateRef.current = onSelectAnalyticsCandidate; }, [onSelectAnalyticsCandidate]);
  useEffect(() => { selectedAnalyticsCandidateRef.current = selectedAnalyticsCandidate; analyticsSettlementsLayerRef.current?.changed(); }, [selectedAnalyticsCandidate]);
  useEffect(() => { selectedAnalysisRouteIdRef.current = selectedAnalysisRouteId; analyticsRoutesLayerRef.current?.setStyle(makeAnalysisRouteStyle(selectedAnalysisRouteId)); analyticsRoutesLayerRef.current?.changed(); }, [selectedAnalysisRouteId]);
  useEffect(() => { onSelectAnalysisRouteRef.current = onSelectAnalysisRoute; }, [onSelectAnalysisRoute]);

  useEffect(() => {
    mapModeRef.current = mapMode;
    const isAnalytics = mapMode === 'analytics';

    lineLayerRef.current?.setVisible(!isAnalytics);
    stationLayerRef.current?.setVisible(!isAnalytics);
    routeLineLayerRef.current?.setVisible(!isAnalytics);
    routeNetworkSegmentsLayerRef.current?.setVisible(!isAnalytics);
    routeStopsLayerRef.current?.setVisible(!isAnalytics);
    districtLayerRef.current?.setVisible(selectionMode || !isAnalytics);
    districtLabelLayerRef.current?.setVisible(selectionMode && !isAnalytics);

    analyticsSettlementsLayerRef.current?.setVisible(isAnalytics && showAnalyticsPoints && Boolean(heatmapData));
    analyticsHeatmapLayerRef.current?.setVisible(isAnalytics && showAnalyticsHeatmap && Boolean(heatmapData));
    analyticsVirtualStationLayerRef.current?.setVisible(isAnalytics);
    analyticsConnectionLayerRef.current?.setVisible(isAnalytics);
    analyticsRoutesLayerRef.current?.setVisible(isAnalytics && showAlternatives);
  }, [mapMode, selectionMode, showAnalyticsHeatmap, showAnalyticsPoints, showAlternatives, heatmapData]);

  useEffect(() => {
    if (!mapElementRef.current || mapRef.current) return;

    const baseLayer = new TileLayer({ preload: 2, source: new OSM({ crossOrigin: 'anonymous', transition: 0 }) });
    const districtSource = new VectorSource();
    const districtLabelSource = new VectorSource();
    const russiaBoundarySource = new VectorSource();
    const stationSource = new VectorSource();
    const lineSource = new VectorSource();
    const routeLineSource = new VectorSource();
    const routeNetworkSegmentsSource = new VectorSource();
    const routeStopsSource = new VectorSource();
    const analyticsSettlementsSource = new VectorSource();
    const analyticsHeatmapSource = new VectorSource();
    const analyticsVirtualStationSource = new VectorSource();
    const analyticsConnectionSource = new VectorSource();
    const analyticsRoutesSource = new VectorSource();

    const districtLayer = new VectorLayer({ source: districtSource, style: districtStyleFunction, declutter: false });
    const districtLabelLayer = new VectorLayer({ source: districtLabelSource, style: districtLabelStyleFunction, declutter: true });
    const russiaBoundaryLayer = new VectorLayer({
      source: russiaBoundarySource,
      style: () => new Style({ stroke: new Stroke({ color: selectionModeRef.current ? 'rgba(55, 65, 81, 0.65)' : 'rgba(55, 65, 81, 0.32)', width: selectionModeRef.current ? 2.2 : 1.4, lineDash: selectionModeRef.current ? undefined : [12, 8] }), zIndex: 10 }),
    });

    const lineLayer = new VectorImageLayer({
      source: lineSource,
      imageRatio: 1.3,
      style: (feature) => {
        const isService = Boolean(feature.get('is_service_line'));
        const isMainPassenger = Boolean(feature.get('is_main_passenger_line'));
        const isVisibleDefault = feature.get('is_visible_default');
        const excludeReason = feature.get('exclude_reason');
        if (isVisibleDefault === false || excludeReason) return null;
        if (isService) return new Style({ stroke: new Stroke({ color: 'rgba(100, 116, 139, 0.45)', width: 1.2, lineDash: [6, 6] }), zIndex: 12 });
        if (isMainPassenger) return new Style({ stroke: new Stroke({ color: 'rgba(37, 99, 235, 0.72)', width: 2.4 }), zIndex: 18 });
        return new Style({ stroke: new Stroke({ color: 'rgba(37, 99, 235, 0.45)', width: 1.6 }), zIndex: 14 });
      },
    });

    const routeLineLayer = new VectorLayer({
      source: routeLineSource,
      style: (feature) => {
        const geometrySource = feature.get('geometry_source');
        const isVirtual = geometrySource === 'virtual_osm_path';
        const isFallback = geometrySource === 'fallback_station_chain';
        const color = isVirtual ? '#7c3aed' : '#16a34a';
        return [
          new Style({ stroke: new Stroke({ color: 'rgba(255,255,255,0.96)', width: 9, lineDash: isFallback ? [14, 10] : undefined }), zIndex: 78 }),
          new Style({ stroke: new Stroke({ color, width: 4.8, lineDash: isVirtual || isFallback ? [10, 8] : undefined }), zIndex: 79 }),
        ];
      },
    });

    const routeNetworkSegmentsLayer = new VectorLayer({
      source: routeNetworkSegmentsSource,
      style: (feature) => {
        const segmentSource = feature.get('segment_source');
        const isVirtual = segmentSource === 'virtual_osm_path';
        return [
          new Style({ stroke: new Stroke({ color: 'rgba(255,255,255,0.98)', width: 11 }), zIndex: 84 }),
          new Style({ stroke: new Stroke({ color: isVirtual ? '#7c3aed' : '#16a34a', width: isVirtual ? 6 : 7, lineDash: isVirtual ? [10, 8] : undefined }), zIndex: 85 }),
        ];
      },
    });

    const routeStopsLayer = new VectorLayer({ source: routeStopsSource, style: routeStopsStyleFunction, declutter: false });
    const analyticsHeatmapLayer = new HeatmapLayer({ source: analyticsHeatmapSource, blur: 18, radius: 16, weight: (feature) => feature.get('weight') || 0.1, visible: false, zIndex: 92 });
    const analyticsSettlementsLayer = new VectorLayer({
      source: analyticsSettlementsSource,
      visible: false,
      style: (feature) => {
        const settlement = feature.get('settlement');
        const isSelected = settlement?.id === selectedAnalyticsCandidateRef.current?.id;
        const level = feature.get('attention_level');
        const served = feature.get('served');
        let fillColor = '#22c55e';
        if (served) fillColor = '#64748b';
        else if (level === 'high') fillColor = '#dc2626';
        else if (level === 'medium') fillColor = '#f59e0b';
        return new Style({ image: new CircleStyle({ radius: isSelected ? 9 : 6, fill: new Fill({ color: fillColor }), stroke: new Stroke({ color: '#ffffff', width: isSelected ? 2.4 : 1.5 }) }), zIndex: isSelected ? 105 : 96 });
      },
    });
    const analyticsConnectionLayer = new VectorLayer({ source: analyticsConnectionSource, visible: false, style: () => new Style({ stroke: new Stroke({ color: '#ef4444', width: 2.4, lineDash: [8, 8] }), zIndex: 106 }) });
    const analyticsVirtualStationLayer = new VectorLayer({ source: analyticsVirtualStationSource, visible: false, style: () => new Style({ image: new CircleStyle({ radius: 8, fill: new Fill({ color: '#7c3aed' }), stroke: new Stroke({ color: '#ffffff', width: 2 }) }), zIndex: 107 }) });
    const analyticsRoutesLayer = new VectorLayer({ source: analyticsRoutesSource, visible: false, style: makeAnalysisRouteStyle(selectedAnalysisRouteIdRef.current) });
    const stationLayer = new VectorImageLayer({
      source: stationSource,
      imageRatio: 1.3,
      style: (feature) => {
        const isSelected = feature.get('stationId') === selectedStationIdRef.current;
        return new Style({ image: new CircleStyle({ radius: isSelected ? 8 : 5, fill: new Fill({ color: isSelected ? '#16a34a' : '#dc2626' }), stroke: new Stroke({ color: '#ffffff', width: 1.5 }) }), zIndex: isSelected ? 36 : 30 });
      },
    });

    const view = new View({ center: fromLonLat([90, 61]), zoom: 3.8, minZoom: 3.5, maxZoom: 16, extent: RUSSIA_EXTENT, smoothExtentConstraint: false, smoothResolutionConstraint: false, multiWorld: false, enableRotation: false });
    const map = new Map({ target: mapElementRef.current, layers: [baseLayer, districtLayer, russiaBoundaryLayer, districtLabelLayer, lineLayer, stationLayer, routeLineLayer, routeNetworkSegmentsLayer, routeStopsLayer, analyticsHeatmapLayer, analyticsSettlementsLayer, analyticsConnectionLayer, analyticsVirtualStationLayer, analyticsRoutesLayer], view, loadTilesWhileAnimating: true, loadTilesWhileInteracting: true });

    map.on('pointermove', (event) => {
      if (!selectionModeRef.current) {
        if (hoverRegionCodeRef.current !== null) {
          hoverRegionCodeRef.current = null;
          districtLayer.changed();
        }
        return;
      }

      const districtFeature = map.forEachFeatureAtPixel(event.pixel, (foundFeature, layer) => (layer === districtLayerRef.current ? foundFeature : null), { hitTolerance: 4 });
      const nextHoverCode = districtFeature?.get('code') || null;
      if (nextHoverCode !== hoverRegionCodeRef.current) {
        hoverRegionCodeRef.current = nextHoverCode;
        districtLayer.changed();
      }
    });

    map.on('singleclick', (event) => {
      if (selectionModeRef.current) {
        const districtFeature = map.forEachFeatureAtPixel(event.pixel, (foundFeature, layer) => (layer === districtLayerRef.current ? foundFeature : null), { hitTolerance: 4 });
        if (districtFeature) onActiveRegionChange(districtFeature.get('code'));
        return;
      }

      if (mapModeRef.current === 'analytics') {
        const analysisRouteFeature = map.forEachFeatureAtPixel(event.pixel, (foundFeature) => (foundFeature.get('analysis_route_id') ? foundFeature : null), { hitTolerance: 8 });
        if (analysisRouteFeature) {
          onSelectAnalysisRouteRef.current?.(analysisRouteFeature.get('analysis_route_id'));
          return;
        }

        const analyticsFeature = map.forEachFeatureAtPixel(event.pixel, (foundFeature, layer) => (layer === analyticsSettlementsLayerRef.current ? foundFeature : null), { hitTolerance: 7 });
        if (analyticsFeature) {
          const settlement = analyticsFeature.get('settlement');
          if (settlement) onSelectAnalyticsCandidateRef.current?.(settlement);
          return;
        }
      }

      const stationFeature = map.forEachFeatureAtPixel(event.pixel, (foundFeature) => (foundFeature.get('stationId') ? foundFeature : null), { hitTolerance: 4 });
      if (stationFeature) onSelectStation(stationFeature.get('stationId'));
    });

    mapRef.current = map;
    districtLayerRef.current = districtLayer;
    districtLabelLayerRef.current = districtLabelLayer;
    russiaBoundaryLayerRef.current = russiaBoundaryLayer;
    stationLayerRef.current = stationLayer;
    lineLayerRef.current = lineLayer;
    routeLineLayerRef.current = routeLineLayer;
    routeNetworkSegmentsLayerRef.current = routeNetworkSegmentsLayer;
    routeStopsLayerRef.current = routeStopsLayer;
    analyticsSettlementsLayerRef.current = analyticsSettlementsLayer;
    analyticsHeatmapLayerRef.current = analyticsHeatmapLayer;
    analyticsVirtualStationLayerRef.current = analyticsVirtualStationLayer;
    analyticsConnectionLayerRef.current = analyticsConnectionLayer;
    analyticsRoutesLayerRef.current = analyticsRoutesLayer;

    districtSourceRef.current = districtSource;
    districtLabelSourceRef.current = districtLabelSource;
    russiaBoundarySourceRef.current = russiaBoundarySource;
    stationSourceRef.current = stationSource;
    lineSourceRef.current = lineSource;
    routeLineSourceRef.current = routeLineSource;
    routeNetworkSegmentsSourceRef.current = routeNetworkSegmentsSource;
    routeStopsSourceRef.current = routeStopsSource;
    analyticsSettlementsSourceRef.current = analyticsSettlementsSource;
    analyticsHeatmapSourceRef.current = analyticsHeatmapSource;
    analyticsVirtualStationSourceRef.current = analyticsVirtualStationSource;
    analyticsConnectionSourceRef.current = analyticsConnectionSource;
    analyticsRoutesSourceRef.current = analyticsRoutesSource;

    setTimeout(() => map.updateSize(), 200);

    return () => {
      map.setTarget(undefined);
      mapRef.current = null;
    };
  }, [districtLabelStyleFunction, districtStyleFunction, onActiveRegionChange, onSelectStation, routeStopsStyleFunction]);

  useEffect(() => {
    if (!mapRef.current) return;
    const updateMapSize = () => mapRef.current?.updateSize();
    const t1 = window.setTimeout(updateMapSize, 0);
    const t2 = window.setTimeout(updateMapSize, 250);
    const t3 = window.setTimeout(updateMapSize, 700);
    window.addEventListener('resize', updateMapSize);
    return () => {
      window.clearTimeout(t1);
      window.clearTimeout(t2);
      window.clearTimeout(t3);
      window.removeEventListener('resize', updateMapSize);
    };
  }, []);

  useEffect(() => { mapRef.current?.updateSize(); }, [selectionMode, loading, loadedRegionCodes.length]);

  useEffect(() => {
    if (!federalDistrictsData || !districtSourceRef.current || !districtLabelSourceRef.current) return;
    const format = new GeoJSON();
    const districtFeatures = format.readFeatures(federalDistrictsData, { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' });
    districtSourceRef.current.clear(true);
    addValidFeatures(districtSourceRef.current, districtFeatures);
    const labelFeatures = buildDistrictLabelFeatures(federalDistrictsData);
    districtLabelSourceRef.current.clear(true);
    addValidFeatures(districtLabelSourceRef.current, labelFeatures);
  }, [federalDistrictsData]);

  useEffect(() => {
    if (!russiaFeatureData || !russiaBoundarySourceRef.current) return;
    const format = new GeoJSON();
    const feature = format.readFeature(russiaFeatureData, { dataProjection: 'EPSG:4326', featureProjection: 'EPSG:3857' });
    russiaBoundarySourceRef.current.clear(true);
    addValidFeature(russiaBoundarySourceRef.current, feature);
  }, [russiaFeatureData]);

  useEffect(() => {
    if (!stationSourceRef.current) return;
    const features = createStationFeatures(stations, selectedStation);
    stationSourceRef.current.clear(true);
    addValidFeatures(stationSourceRef.current, features);
  }, [stations, selectedStation]);

  useEffect(() => {
    if (!lineSourceRef.current) return;

    const features = createLineFeatures(lines).filter((feature) => {
      const geometry = feature?.getGeometry?.();
      return geometry && typeof geometry.getExtent === 'function';
    });

    lineSourceRef.current.clear(true);

    if (features.length > 0) {
      addValidFeatures(lineSourceRef.current, features);
    }
  }, [lines]);

  useEffect(() => {
    if (!routeLineSourceRef.current || !routeNetworkSegmentsSourceRef.current || !routeStopsSourceRef.current) return;
    routeLineSourceRef.current.clear(true);
    routeNetworkSegmentsSourceRef.current.clear(true);
    routeStopsSourceRef.current.clear(true);
    if (!selectedRoute) return;

    routeDebug('Map route render', {
      route_id: selectedRoute?.id,
      route_name: selectedRoute?.route_name,
      geometry_source: selectedRoute?.geometry_source,
      has_geometry: Boolean(selectedRoute?.geometry),
      network_segments_count: selectedRoute?.network_segments?.length || 0,
      stops_count: selectedRoute?.stops?.length || 0,
    });

    if (selectedRoute.geometry) {
      const routeFeature = createRouteGeometryFeature(selectedRoute.geometry, selectedRoute.geometry_source || null);
      addValidFeature(routeLineSourceRef.current, routeFeature);
    }

    const networkSegmentFeatures = createRouteNetworkSegmentFeatures(selectedRoute.network_segments || []);
    addValidFeatures(routeNetworkSegmentsSourceRef.current, networkSegmentFeatures);

    const stopFeatures = createRouteStopFeatures(selectedRoute.stops);
    addValidFeatures(routeStopsSourceRef.current, stopFeatures);
  }, [selectedRoute]);

  useEffect(() => {
    if (!mapRef.current || !federalDistrictsData) return;

    if (selectionMode && activeRegionCode) {
      const districtFeature = getDistrictFeatureByCode(federalDistrictsData, activeRegionCode);
      const geometry = districtFeature?.getGeometry();
      if (geometry) {
        const focusConfig = REGION_FOCUS_CONFIG[activeRegionCode] || {};
        const center = focusConfig.centerLonLat ? fromLonLat(focusConfig.centerLonLat) : getCenter(geometry.getExtent());
        const zoom = focusConfig.zoom ?? 5.3;
        mapRef.current.getView().animate({ center, zoom, duration: 320 });
      }
      return;
    }

    if (!selectionMode && loadedRegionCodes.length > 0) {
      const combinedExtent = buildCombinedExtentForRegions(federalDistrictsData, loadedRegionCodes);
      if (combinedExtent) mapRef.current.getView().fit(combinedExtent, { padding: [60, 60, 60, 60], duration: 320, maxZoom: 7 });
    }
  }, [selectionMode, activeRegionCode, loadedRegionCodes, federalDistrictsData]);

  useEffect(() => {
    if (!mapRef.current || !selectedStation) return;
    if (!Number.isFinite(Number(selectedStation.lon)) || !Number.isFinite(Number(selectedStation.lat))) return;
    mapRef.current.getView().animate({ center: fromLonLat([Number(selectedStation.lon), Number(selectedStation.lat)]), zoom: Math.max(mapRef.current.getView().getZoom() ?? 6, 8), duration: 320 });
  }, [selectedStation]);

  useEffect(() => {
    if (!mapRef.current || !selectedRoute) return;
    const routeNetworkSegmentsSource = routeNetworkSegmentsSourceRef.current;
    const routeLineSource = routeLineSourceRef.current;
    const routeStopsSource = routeStopsSourceRef.current;
    const segmentFeatures = routeNetworkSegmentsSource?.getFeatures() || [];
    const lineFeatures = routeLineSource?.getFeatures() || [];
    const stopFeatures = routeStopsSource?.getFeatures() || [];

    if (segmentFeatures.length > 0) {
      const extent = routeNetworkSegmentsSource.getExtent();
      if (extent && Number.isFinite(extent[0])) {
        mapRef.current.getView().fit(extent, { padding: [70, 70, 70, 70], duration: 420, maxZoom: 8 });
        return;
      }
    }
    if (lineFeatures.length > 0) {
      const extent = routeLineSource.getExtent();
      if (extent && Number.isFinite(extent[0])) {
        mapRef.current.getView().fit(extent, { padding: [70, 70, 70, 70], duration: 420, maxZoom: 8 });
        return;
      }
    }
    if (stopFeatures.length > 0) {
      const extent = routeStopsSource.getExtent();
      if (extent && Number.isFinite(extent[0])) mapRef.current.getView().fit(extent, { padding: [70, 70, 70, 70], duration: 420, maxZoom: 8 });
    }
  }, [selectedRoute]);

  useEffect(() => {
    if (!analyticsSettlementsSourceRef.current) return;

    analyticsSettlementsSourceRef.current.clear(true);

    if (!heatmapData) {
      analyticsSettlementsLayerRef.current?.setVisible(false);
      return;
    }

    const sourceSettlements = heatmapSettlements?.length > 0
      ? heatmapSettlements
      : extractHeatmapSettlements(heatmapData);

    const settlements = sourceSettlements.filter((item) => {
      const population = Number(item.population ?? 0);
      const minPopulation = Number(analyticsParams?.min_population ?? 0);
      const maxPopulation = Number(analyticsParams?.max_population ?? Number.POSITIVE_INFINITY);
      return population >= minPopulation && population <= maxPopulation;
    });

    const features = createAnalyticsSettlementFeatures(settlements);

    if (features.length > 0) {
      addValidFeatures(analyticsSettlementsSourceRef.current, features);
    }
  }, [heatmapData, heatmapSettlements, analyticsParams]);

  useEffect(() => {
    if (!analyticsHeatmapSourceRef.current) return;
    const heatmapSettlements = extractHeatmapSettlements(heatmapData).filter((item) => {
      const population = Number(item.population ?? 0);
      const minPopulation = Number(analyticsParams?.min_population ?? 0);
      const maxPopulation = Number(analyticsParams?.max_population ?? Number.POSITIVE_INFINITY);
      return population >= minPopulation && population <= maxPopulation;
    });
    const features = createAnalyticsSettlementFeatures(heatmapSettlements);
    analyticsHeatmapSourceRef.current.clear(true);
    addValidFeatures(analyticsHeatmapSourceRef.current, features.map((feature) => feature.clone()));
    analyticsHeatmapLayerRef.current?.setVisible(mapMode === 'analytics' && showAnalyticsHeatmap && Boolean(heatmapData));
  }, [heatmapData, analyticsParams, mapMode, showAnalyticsHeatmap]);

  useEffect(() => {
    if (!analyticsVirtualStationSourceRef.current || !analyticsConnectionSourceRef.current) return;
    analyticsVirtualStationSourceRef.current.clear(true);
    analyticsConnectionSourceRef.current.clear(true);
    const virtualStationFeature = createAnalyticsVirtualStationFeature(selectedAnalyticsCandidate);
    const connectionFeature = createAnalyticsConnectionFeature(selectedAnalyticsCandidate);
    addValidFeature(analyticsVirtualStationSourceRef.current, virtualStationFeature);
    addValidFeature(analyticsConnectionSourceRef.current, connectionFeature);
    analyticsSettlementsLayerRef.current?.changed();
  }, [selectedAnalyticsCandidate]);

  useEffect(() => {
    if (!analyticsRoutesSourceRef.current) return;
    const features = createAnalysisRouteFeatures(analysisRoutes || []);
    analyticsRoutesSourceRef.current.clear(true);
    addValidFeatures(analyticsRoutesSourceRef.current, features);
    analyticsRoutesLayerRef.current?.setStyle(makeAnalysisRouteStyle(selectedAnalysisRouteId));
    analyticsRoutesLayerRef.current?.changed();
  }, [analysisRoutes, selectedAnalysisRouteId]);

  return (
    <section className="map-section" style={{ position: 'relative', flex: 1, minWidth: 0, minHeight: 0, height: '100%', display: 'flex' }}>
      <div ref={mapElementRef} className="map-container" />
    </section>
  );
}

export default function App() {
  const [federalDistrictsData, setFederalDistrictsData] = useState(null);
  const [russiaFeatureData, setRussiaFeatureData] = useState(null);
  const [regionSummaries, setRegionSummaries] = useState({});

  const [pendingRegionCodes, setPendingRegionCodes] = useState([]);
  const [loadedRegionCodes, setLoadedRegionCodes] = useState([]);
  const [activeRegionCode, setActiveRegionCode] = useState(null);

  const [allStations, setAllStations] = useState([]);
  const [stations, setStations] = useState([]);
  const [lines, setLines] = useState([]);
  const [searchSections, setSearchSections] = useState({ selected: [], other: [] });

  const [routes, setRoutes] = useState([]);
  const [routesLoading, setRoutesLoading] = useState(false);
  const [routesError, setRoutesError] = useState('');
  const [routeSearchQuery, setRouteSearchQuery] = useState('');
  const [selectedRoute, setSelectedRoute] = useState(null);
  const [stationRoutes, setStationRoutes] = useState([]);
  const [sidebarMode, setSidebarMode] = useState('infrastructure');

  const [rzdOriginStation, setRzdOriginStation] = useState(null);
  const [rzdDestinationStation, setRzdDestinationStation] = useState(null);
  const [rzdDepDate, setRzdDepDate] = useState(getDefaultRzdDate);
  const [rzdTrains, setRzdTrains] = useState([]);
  const [rzdSearchLoading, setRzdSearchLoading] = useState(false);
  const [rzdImportLoading, setRzdImportLoading] = useState(false);
  const [rzdError, setRzdError] = useState('');
  const [rzdMessage, setRzdMessage] = useState('');
  const [rzdCalendarDebug, setRzdCalendarDebug] = useState(null);
  const [rzdSearchProgress, setRzdSearchProgress] = useState(0);
  const [rzdSearchProgressMessage, setRzdSearchProgressMessage] = useState('');

  const [virtualOriginStation, setVirtualOriginStation] = useState(null);
  const [virtualDestinationStation, setVirtualDestinationStation] = useState(null);
  const [virtualRouteLoading, setVirtualRouteLoading] = useState(false);
  const [virtualRouteError, setVirtualRouteError] = useState('');
  const [virtualRouteMessage, setVirtualRouteMessage] = useState('');
  const [topologyProgress, setTopologyProgress] = useState(0);
  const [topologyProgressMessage, setTopologyProgressMessage] = useState('');

  const [mapMode, setMapMode] = useState('research');
  const [analyticsParams, setAnalyticsParams] = useState(DEFAULT_ANALYTICS_PARAMS);
  const [analyticsResult, setAnalyticsResult] = useState(null);
  const [analyticsLoading, setAnalyticsLoading] = useState(false);
  const [analyticsError, setAnalyticsError] = useState('');
  const [selectedAnalyticsCandidate, setSelectedAnalyticsCandidate] = useState(null);
  const [showAnalyticsHeatmap, setShowAnalyticsHeatmap] = useState(false);
  const [showAnalyticsPoints, setShowAnalyticsPoints] = useState(false);
  const [alternativesParams, setAlternativesParams] = useState(DEFAULT_ALTERNATIVES_PARAMS);
  const [routeAlternatives, setRouteAlternatives] = useState(null);
  const [alternativesLoading, setAlternativesLoading] = useState(false);
  const [alternativesProgress, setAlternativesProgress] = useState(0);
  const [alternativesError, setAlternativesError] = useState('');
  const [selectedAlternativeId, setSelectedAlternativeId] = useState(null);
  const [showAlternatives, setShowAlternatives] = useState(true);
  const [selectedAnalysisRouteId, setSelectedAnalysisRouteId] = useState('original');
  const [heatmapRouteId, setHeatmapRouteId] = useState(null);
  const [heatmapLoading, setHeatmapLoading] = useState(false);
  const [heatmapData, setHeatmapData] = useState(null);
  const [heatmapSettlements, setHeatmapSettlements] = useState([]);
  const [populationStatsByRouteId, setPopulationStatsByRouteId] = useState({});
  const [analysisRunId, setAnalysisRunId] = useState(() => Date.now());

  const [updateStates, setUpdateStates] = useState({});
  const [checkingAllUpdates, setCheckingAllUpdates] = useState(false);
  const [runningAllUpdates, setRunningAllUpdates] = useState(false);

  const [loading, setLoading] = useState(false);
  const [loadingProgress, setLoadingProgress] = useState(0);
  const [loadingMessage, setLoadingMessage] = useState('');

  const [routeLoadingVisible, setRouteLoadingVisible] = useState(false);
  const [routeLoadingProgress, setRouteLoadingProgress] = useState(0);
  const [routeLoadingMessage, setRouteLoadingMessage] = useState('');

  const [error, setError] = useState('');
  const [selectedStation, setSelectedStation] = useState(null);
  const [showServiceLines, setShowServiceLines] = useState(false);
  const [isSearchMode, setIsSearchMode] = useState(false);

  const hasLoadedData = loadedRegionCodes.length > 0;
  const selectionMode = !hasLoadedData;
  const linesCount = useMemo(() => lines.length, [lines]);

  const analysisRoutes = useMemo(() => buildAnalysisRoutes({ originalRoute: selectedRoute, alternatives: routeAlternatives?.alternatives || [] }), [selectedRoute, routeAlternatives]);
  const selectedAnalysisRoute = useMemo(() => analysisRoutes.find((route) => route.id === selectedAnalysisRouteId) || analysisRoutes[0] || null, [analysisRoutes, selectedAnalysisRouteId]);

  const initialLoadCompletedRef = useRef(false);
  const routeLoadingTimerRef = useRef(null);
  const rzdSearchProgressTimerRef = useRef(null);
  const analysisRunIdRef = useRef(analysisRunId);
  const selectedAnalysisRouteIdAppRef = useRef(selectedAnalysisRouteId);

  const appMode = selectionMode ? 'region_selection' : mapMode === 'analytics' ? 'analytics' : sidebarMode;

  useEffect(() => {
    analysisRunIdRef.current = analysisRunId;
  }, [analysisRunId]);

  useEffect(() => {
    selectedAnalysisRouteIdAppRef.current = selectedAnalysisRouteId;
  }, [selectedAnalysisRouteId]);

  useEffect(() => {
    if (mapMode !== 'analytics' || analysisRoutes.length === 0) {
      setPopulationStatsByRouteId({});
      return undefined;
    }

    const routesForSummary = analysisRoutes
      .filter((route) => route.geometry)
      .map((route) => ({ id: route.id, geometry: route.geometry }));

    if (routesForSummary.length === 0) {
      setPopulationStatsByRouteId({});
      return undefined;
    }

    let cancelled = false;
    const currentRunId = analysisRunId;

    async function loadPopulationSummary() {
      try {
        const payload = await buildPopulationSummaryForRoutes({
          routes: routesForSummary,
          params: analyticsParams,
        });

        if (cancelled || analysisRunIdRef.current !== currentRunId) return;

        const nextStats = {};
        for (const item of payload?.items || []) {
          if (item?.route_id) {
            nextStats[String(item.route_id)] = item;
          }
        }

        setPopulationStatsByRouteId(nextStats);
      } catch (err) {
        if (!cancelled && analysisRunIdRef.current === currentRunId) {
          console.warn('Population summary failed:', err);
          setPopulationStatsByRouteId({});
        }
      }
    }

    loadPopulationSummary();

    return () => {
      cancelled = true;
    };
  }, [mapMode, analysisRoutes, analyticsParams, analysisRunId]);

  useEffect(() => {
    if (analysisRoutes.length === 0) {
      setSelectedAnalysisRouteId('original');
      return;
    }
    const exists = analysisRoutes.some((route) => route.id === selectedAnalysisRouteId);
    if (!exists) setSelectedAnalysisRouteId(analysisRoutes[0].id);
  }, [analysisRoutes, selectedAnalysisRouteId]);

  useEffect(() => {
    if (!alternativesLoading) return undefined;

    setAlternativesProgress((current) => (current > 0 ? current : 5));

    const interval = window.setInterval(() => {
      setAlternativesProgress((current) => {
        if (current >= 100) return current;
        if (current < 55) return current + 7;
        if (current < 78) return current + 3;
        if (current < 90) return current + 1;
        return current;
      });
    }, 350);

    return () => window.clearInterval(interval);
  }, [alternativesLoading]);

  const loadRegionSummaries = useCallback(async () => {
    try {
      const response = await fetch(`${BACKEND_URL}/api/regions/summary`);
      if (!response.ok) throw new Error('Не удалось загрузить сводку по округам');
      const data = await response.json();
      const summariesByCode = Object.fromEntries((data.items || []).map((item) => [item.code, item]));
      setRegionSummaries(summariesByCode);
    } catch (err) {
      console.error('Ошибка загрузки сводки по округам:', err);
    }
  }, []);

  const startRouteLoadingOverlay = useCallback(() => {
    if (routeLoadingTimerRef.current) window.clearInterval(routeLoadingTimerRef.current);
    setRouteLoadingVisible(true);
    setRouteLoadingProgress(8);
    setRouteLoadingMessage('Запрашиваем маршрут у сервера...');
    let currentProgress = 8;
    routeLoadingTimerRef.current = window.setInterval(() => {
      currentProgress = Math.min(currentProgress + 4, 92);
      if (currentProgress < 25) setRouteLoadingMessage('Запрашиваем маршрут у сервера...');
      else if (currentProgress < 45) setRouteLoadingMessage('Получаем список остановок...');
      else if (currentProgress < 65) setRouteLoadingMessage('Подбираем кандидаты станций...');
      else if (currentProgress < 82) setRouteLoadingMessage('Проверяем достижимость по графу...');
      else setRouteLoadingMessage('Подготавливаем отображение маршрута на карте...');
      setRouteLoadingProgress(currentProgress);
    }, 260);
  }, []);

  const finishRouteLoadingOverlay = useCallback(() => {
    if (routeLoadingTimerRef.current) {
      window.clearInterval(routeLoadingTimerRef.current);
      routeLoadingTimerRef.current = null;
    }
    setRouteLoadingProgress(100);
    setRouteLoadingMessage('Маршрут готов');
    window.setTimeout(() => {
      setRouteLoadingVisible(false);
      setRouteLoadingProgress(0);
      setRouteLoadingMessage('');
    }, 280);
  }, []);

  const startRzdSearchProgress = useCallback(() => {
    if (rzdSearchProgressTimerRef.current) window.clearInterval(rzdSearchProgressTimerRef.current);
    setRzdSearchProgress(8);
    setRzdSearchProgressMessage('Подбираем станции и коды РЖД...');
    let progress = 8;
    const startedAt = Date.now();
    rzdSearchProgressTimerRef.current = window.setInterval(() => {
      progress = Math.min(progress + 3, 92);
      const elapsedSeconds = Math.round((Date.now() - startedAt) / 1000);
      if (elapsedSeconds > 30) setRzdSearchProgressMessage('РЖД API отвечает медленно. Продолжаем ждать результат...');
      else if (elapsedSeconds > 15) setRzdSearchProgressMessage('Поиск занимает дольше обычного. Проверяем дополнительные пары станций...');
      else if (progress < 25) setRzdSearchProgressMessage('Подбираем ближайшие станции...');
      else if (progress < 45) setRzdSearchProgressMessage('Проверяем коды РЖД...');
      else if (progress < 70) setRzdSearchProgressMessage('Проверяем даты отправления...');
      else setRzdSearchProgressMessage('Собираем найденные варианты...');
      setRzdSearchProgress(progress);
    }, 350);
  }, []);

  const finishRzdSearchProgress = useCallback((message = 'Поиск завершён') => {
    if (rzdSearchProgressTimerRef.current) {
      window.clearInterval(rzdSearchProgressTimerRef.current);
      rzdSearchProgressTimerRef.current = null;
    }
    setRzdSearchProgress(100);
    setRzdSearchProgressMessage(message);
    window.setTimeout(() => {
      setRzdSearchProgress(0);
      setRzdSearchProgressMessage('');
    }, 700);
  }, []);

  useEffect(() => () => {
    if (routeLoadingTimerRef.current) window.clearInterval(routeLoadingTimerRef.current);
    if (rzdSearchProgressTimerRef.current) window.clearInterval(rzdSearchProgressTimerRef.current);
  }, []);

  useEffect(() => {
    async function loadFederalDistricts() {
      try {
        const response = await fetch('/federal-districts.geojson');
        if (!response.ok) throw new Error('Не удалось загрузить federal-districts.geojson');
        const data = await response.json();
        setFederalDistrictsData(data);
      } catch (err) {
        console.error('Ошибка загрузки контуров федеральных округов:', err);
        setError('Не удалось загрузить контуры федеральных округов');
      }
    }

    async function loadRussiaBoundary() {
      try {
        const response = await fetch('/countries.geojson');
        if (!response.ok) throw new Error('Не удалось загрузить countries.geojson');
        const data = await response.json();
        const russiaFeature = data.features?.find((feature) => feature?.properties?.['ISO3166-1-Alpha-3'] === 'RUS' || feature?.properties?.ISO_A3 === 'RUS' || feature?.properties?.name === 'Russia') || null;
        setRussiaFeatureData(russiaFeature);
      } catch (err) {
        console.error('Ошибка загрузки контура России:', err);
      }
    }

    loadFederalDistricts();
    loadRussiaBoundary();
    loadRegionSummaries();
  }, [loadRegionSummaries]);

  useEffect(() => {
    const runningRegionCodes = Object.entries(updateStates).filter(([, item]) => item.status === 'running' || item.status === 'starting').map(([code]) => code);
    if (runningRegionCodes.length === 0) return undefined;

    const timer = setInterval(async () => {
      for (const regionCode of runningRegionCodes) {
        try {
          const response = await fetch(`${BACKEND_URL}/api/dataset-runs/latest?region_code=${encodeURIComponent(regionCode)}`);
          if (!response.ok) continue;
          const data = await response.json();
          const latest = data.item;
          if (!latest) continue;

          if (latest.status === 'running') {
            setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'running', message: 'Обновление выполняется...', notes: latest.notes || '' } }));
            continue;
          }

          if (latest.status === 'finished') {
            setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'finished', message: 'Обновление завершено. Чтобы увидеть новые данные на карте, заново выбери округа.', notes: latest.notes || '', can_update: false } }));
            await loadRegionSummaries();
            continue;
          }

          if (latest.status === 'failed') {
            setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'failed', message: 'Во время обновления произошла ошибка', notes: latest.notes || '', can_update: false } }));
            continue;
          }

          if (latest.status === 'skipped') {
            setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'up_to_date', message: 'Обновление не требуется', notes: latest.notes || '', can_update: false } }));
          }
        } catch (err) {
          console.error('Ошибка polling статуса обновления:', regionCode, err);
        }
      }
    }, 4000);

    return () => clearInterval(timer);
  }, [updateStates, loadRegionSummaries]);

  const togglePendingRegion = useCallback((regionCode) => {
    if (!regionCode) return;
    setPendingRegionCodes((prev) => (prev.includes(regionCode) ? prev.filter((code) => code !== regionCode) : [...prev, regionCode]));
  }, []);

  const loadAllSelectedData = useCallback(async ({ regionCodes, includeServiceLines }) => {
    if (!regionCodes.length) return;
    const regionCodesParam = regionCodes.join(',');
    setLoading(true);
    setLoadingProgress(8);
    setLoadingMessage('Подготавливаем загрузку выбранных округов...');
    setError('');

    try {
      const stationsUrl = `${BACKEND_URL}/api/stations?region_codes=${encodeURIComponent(regionCodesParam)}&limit=100000`;
      const linesUrl = `${BACKEND_URL}/api/lines?region_codes=${encodeURIComponent(regionCodesParam)}&limit=400000&include_service=${includeServiceLines ? 'true' : 'false'}`;
      setLoadingProgress(18);
      setLoadingMessage('Загружаем станции выбранных округов...');
      const stationsResponse = await fetch(stationsUrl);
      if (!stationsResponse.ok) throw new Error('Не удалось загрузить станции');
      const stationsData = await stationsResponse.json();
      setLoadingProgress(52);
      setLoadingMessage('Загружаем линии выбранных округов...');
      const linesResponse = await fetch(linesUrl);
      if (!linesResponse.ok) throw new Error('Не удалось загрузить линии');
      const linesData = await linesResponse.json();
      setLoadingProgress(86);
      setLoadingMessage('Подготавливаем данные для отображения...');
      const stationsItems = stationsData.items || [];
      const linesItems = linesData.items || [];
      setAllStations(stationsItems);
      setStations(stationsItems);
      setLines(linesItems);
      setSearchSections({ selected: stationsItems, other: [] });
      setSelectedStation(null);
      setStationRoutes([]);
      setIsSearchMode(false);
      setLoadedRegionCodes(regionCodes);
      setSidebarMode('infrastructure');
      initialLoadCompletedRef.current = true;
      setLoadingProgress(100);
      setLoadingMessage('Готово');
      setTimeout(() => { setLoading(false); setLoadingProgress(0); setLoadingMessage(''); }, 180);
    } catch (err) {
      console.error(err);
      setLoading(false);
      setLoadingProgress(0);
      setLoadingMessage('');
      setError(err instanceof Error ? err.message : 'Ошибка загрузки данных');
    }
  }, []);

  const handleLoadSelected = useCallback(async () => {
    if (pendingRegionCodes.length === 0) return;
    await loadAllSelectedData({ regionCodes: pendingRegionCodes, includeServiceLines: showServiceLines });
  }, [pendingRegionCodes, showServiceLines, loadAllSelectedData]);

  const handleBackToSelection = useCallback(() => {
    setPendingRegionCodes([...loadedRegionCodes]);
    setLoadedRegionCodes([]);
    setAllStations([]);
    setStations([]);
    setLines([]);
    setRoutes([]);
    setSelectedRoute(null);
    setStationRoutes([]);
    setSearchSections({ selected: [], other: [] });
    setSelectedStation(null);
    setIsSearchMode(false);
    setSidebarMode('infrastructure');
    setError('');
    initialLoadCompletedRef.current = false;
  }, [loadedRegionCodes]);

  useEffect(() => {
    if (!initialLoadCompletedRef.current || !loadedRegionCodes.length) return;
    async function reloadLinesOnly() {
      try {
        setLoading(true);
        setLoadingProgress(18);
        setLoadingMessage('Обновляем линии для загруженных округов...');
        setError('');
        const regionCodesParam = loadedRegionCodes.join(',');
        const linesUrl = `${BACKEND_URL}/api/lines?region_codes=${encodeURIComponent(regionCodesParam)}&limit=400000&include_service=${showServiceLines ? 'true' : 'false'}`;
        const response = await fetch(linesUrl);
        if (!response.ok) throw new Error('Не удалось загрузить линии');
        const data = await response.json();
        setLoadingProgress(85);
        setLoadingMessage('Применяем изменения...');
        setLines(data.items || []);
        setLoadingProgress(100);
        setLoadingMessage('Готово');
        setTimeout(() => { setLoading(false); setLoadingProgress(0); setLoadingMessage(''); }, 160);
      } catch (err) {
        console.error(err);
        setLoading(false);
        setLoadingProgress(0);
        setLoadingMessage('');
        setError(err instanceof Error ? err.message : 'Ошибка загрузки линий');
      }
    }
    reloadLinesOnly();
  }, [showServiceLines, loadedRegionCodes]);

  const handleSearch = useCallback(async (query = '') => {
    const trimmed = query.trim();
    if (!trimmed) {
      setStations(allStations);
      setSearchSections({ selected: allStations, other: [] });
      setIsSearchMode(false);
      return;
    }
    try {
      setLoading(true);
      setError('');
      const response = await fetch(`${BACKEND_URL}/api/search/stations?q=${encodeURIComponent(trimmed)}&limit=2000`);
      if (!response.ok) throw new Error('Не удалось выполнить поиск');
      const data = await response.json();
      const items = data.items || [];
      const sections = deriveSearchSections(items, loadedRegionCodes);
      setStations([...sections.selected, ...sections.other]);
      setSearchSections(sections);
      setIsSearchMode(true);
      setSelectedStation(null);
      setStationRoutes([]);
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Ошибка поиска');
    } finally {
      setLoading(false);
    }
  }, [allStations, loadedRegionCodes]);

  const handleSelectStation = useCallback(async (stationOrId) => {
    const stationId = typeof stationOrId === 'object' ? stationOrId?.id : stationOrId;
    if (!stationId) return;
    try {
      setError('');
      const stationResponse = await fetch(`${BACKEND_URL}/api/stations/${stationId}?include_hidden=true`);
      if (!stationResponse.ok) throw new Error('Не удалось загрузить станцию');
      const stationData = await stationResponse.json();
      setSelectedStation(stationData);
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Ошибка загрузки станции');
    }
  }, []);

  const handleSelectStationObject = useCallback((station) => {
    if (!station?.id) return;
    setSelectedStation(station);
    handleSelectStation(station.id);
  }, [handleSelectStation]);

  const handleSelectRzdOrigin = useCallback((station) => {
    setRzdOriginStation(station);
    setRzdTrains([]);
    setRzdError('');
    setRzdMessage(`Выбрана станция отправления: ${station.name}`);
    handleSelectStationObject(station);
  }, [handleSelectStationObject]);

  const handleSelectRzdDestination = useCallback((station) => {
    setRzdDestinationStation(station);
    setRzdTrains([]);
    setRzdError('');
    setRzdMessage(`Выбрана станция назначения: ${station.name}`);
    handleSelectStationObject(station);
  }, [handleSelectStationObject]);

  const handleSelectVirtualOrigin = useCallback((station) => {
    setVirtualOriginStation(station);
    setVirtualRouteError('');
    setVirtualRouteMessage(`Выбрана начальная станция: ${station.name}`);
    handleSelectStationObject(station);
  }, [handleSelectStationObject]);

  const handleSelectVirtualDestination = useCallback((station) => {
    setVirtualDestinationStation(station);
    setVirtualRouteError('');
    setVirtualRouteMessage(`Выбрана конечная станция: ${station.name}`);
    handleSelectStationObject(station);
  }, [handleSelectStationObject]);

  const handleSelectRoute = useCallback(async (routeId) => {
    try {
      setSidebarMode('routes');
      setSelectedStation(null);
      setStationRoutes([]);
      setRoutesLoading(true);
      setRoutesError('');
      startRouteLoadingOverlay();
      routeDebug('Route select request', { routeId });
      const response = await fetch(`${BACKEND_URL}/api/routes/${routeId}`);
      if (!response.ok) throw new Error('Не удалось загрузить маршрут');
      const data = await response.json();
      routeDebug('Route select response', { routeId, geometry_source: data.geometry_source || data.item?.geometry_source, geometry_ready: Boolean(data.geometry || data.item?.geometry), network_segments_count: (data.network_segments || data.item?.network_segments || []).length, summary: data.summary, diagnostics: data.diagnostics || data.item?.diagnostics });
      const normalizedRoute = {
        ...(data.item || data.route || {}),
        id: data.item?.id || data.route?.id || data.route_id || routeId,
        stops: data.stops || data.item?.stops || [],
        geometry: data.geometry || data.item?.geometry || data.route?.geometry || null,
        geometry_source: data.geometry_source || data.item?.geometry_source || null,
        network_segments: data.network_segments || data.item?.network_segments || [],
        diagnostics: data.diagnostics || data.item?.diagnostics || null,
      };
      setSelectedRoute(normalizedRoute);
      const nextRunId = Date.now();
      analysisRunIdRef.current = nextRunId;
      setAnalysisRunId(nextRunId);
      setRouteAlternatives(null);
      setAlternativesError('');
      setSelectedAlternativeId(null);
      setSelectedAnalysisRouteId('original');
      setHeatmapData(null);
      setHeatmapSettlements([]);
      setPopulationStatsByRouteId({});
      setHeatmapRouteId(null);
      setShowAnalyticsHeatmap(false);
      setShowAnalyticsPoints(false);
      finishRouteLoadingOverlay();
    } catch (err) {
      console.error(err);
      if (routeLoadingTimerRef.current) {
        window.clearInterval(routeLoadingTimerRef.current);
        routeLoadingTimerRef.current = null;
      }
      setRouteLoadingVisible(false);
      setRouteLoadingProgress(0);
      setRouteLoadingMessage('');
      setRoutesError(err instanceof Error ? err.message : 'Ошибка загрузки маршрута');
    } finally {
      setRoutesLoading(false);
    }
  }, [finishRouteLoadingOverlay, startRouteLoadingOverlay]);

  const handleClearRoute = useCallback(() => {
    setSelectedRoute(null);
    setMapMode('research');
    setAnalyticsResult(null);
    setSelectedAnalyticsCandidate(null);
    setAnalyticsError('');
    const nextRunId = Date.now();
    analysisRunIdRef.current = nextRunId;
    setAnalysisRunId(nextRunId);
    setRouteAlternatives(null);
    setAlternativesError('');
    setSelectedAlternativeId(null);
    setSelectedAnalysisRouteId('original');
    setHeatmapData(null);
    setHeatmapSettlements([]);
    setPopulationStatsByRouteId({});
    setHeatmapRouteId(null);
    setShowAnalyticsHeatmap(false);
    setShowAnalyticsPoints(false);
  }, []);

  const handleSearchRzdRoutes = useCallback(async () => {
    if (!rzdOriginStation?.id) {
      setRzdError('Выберите станцию отправления.');
      return;
    }
    if (!rzdDestinationStation?.id) {
      setRzdError('Выберите станцию назначения.');
      return;
    }

    try {
      setRzdSearchLoading(true);
      setRzdError('');
      setRzdMessage('');
      setRzdTrains([]);
      setRzdCalendarDebug(null);
      startRzdSearchProgress();
      const requestPayload = {
        origin_station_id: rzdOriginStation.id,
        destination_station_id: rzdDestinationStation.id,
        days_ahead: RZD_SEARCH_DAYS_AHEAD,
        check_seats: false,
        nearby_radius_km: 5,
        nearby_station_limit: 5,
        max_code_pair_attempts: 10,
      };
      routeDebug('RZD A-B search request', requestPayload);
      const response = await fetch(`${BACKEND_URL}/api/rzd/routes/search-calendar-by-stations`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(requestPayload) });
      if (!response.ok) throw new Error(await readApiError(response, 'Не удалось найти поезда через РЖД API'));
      const data = await response.json();
      routeDebug('RZD A-B search response', { status: data.status, total: data.total, message: data.message, exact_found: data.exact_found, similar_used: data.similar_used, code_attempts_count: data.code_attempts?.length, dates_checked: data.dates_checked, dates_with_trains: data.dates_with_trains, items_preview: (data.items || []).slice(0, 5) });
      setRzdCalendarDebug(data);
      const items = data.items || [];
      setRzdTrains(items);
      if (items.length === 0) {
        const attemptsCount = (data.code_attempts || []).length;
        const checkedCount = data.dates_checked ?? 0;
        setRzdMessage(data.message || `За ближайшие ${checkedCount || RZD_SEARCH_DAYS_AHEAD} дней поездов не найдено. Проверено пар кодов: ${attemptsCount}.`);
      } else {
        setRzdMessage(data.message || `Найдено поездов: ${items.length}`);
      }
      finishRzdSearchProgress(items.length > 0 ? 'Поезда найдены' : 'Поиск завершён');
    } catch (err) {
      console.error(err);
      setRzdError(err instanceof Error ? err.message : 'Ошибка поиска поездов');
      finishRzdSearchProgress('Поиск завершён с ошибкой');
    } finally {
      setRzdSearchLoading(false);
    }
  }, [rzdOriginStation, rzdDestinationStation, startRzdSearchProgress, finishRzdSearchProgress]);

  const ensureTopologyForRegions = useCallback(async (regionCodes) => {
    if (!regionCodes.length) throw new Error('Не удалось определить округа для topology graph.');
    const regionCodesParam = regionCodes.join(',');
    setTopologyProgress(8);
    setTopologyProgressMessage('Проверяем topology graph...');
    const statusResponse = await fetch(`${BACKEND_URL}/api/topology/status?region_codes=${encodeURIComponent(regionCodesParam)}`);
    if (!statusResponse.ok) throw new Error('Не удалось проверить topology graph');
    const statusData = await statusResponse.json();
    const statusItem = statusData.item;
    if (statusItem?.is_built) {
      setTopologyProgress(100);
      setTopologyProgressMessage('Topology graph готов');
      window.setTimeout(() => { setTopologyProgress(0); setTopologyProgressMessage(''); }, 600);
      return statusItem;
    }

    setTopologyProgress(18);
    setTopologyProgressMessage('Topology graph не найден. Запускаем построение...');
    const buildResponse = await fetch(`${BACKEND_URL}/api/topology/build`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ region_codes: regionCodes, force_rebuild: false }) });
    if (!buildResponse.ok) throw new Error(await readApiError(buildResponse, 'Не удалось запустить построение topology graph'));
    const buildData = await buildResponse.json();
    const jobId = buildData.job_id;
    if (!jobId) throw new Error('Backend не вернул job_id для построения topology graph');
    const startedAt = Date.now();

    return await new Promise((resolve, reject) => {
      const timer = window.setInterval(async () => {
        try {
          const jobResponse = await fetch(`${BACKEND_URL}/api/topology/jobs/${jobId}`);
          if (!jobResponse.ok) throw new Error('Не удалось получить статус построения topology graph');
          const jobData = await jobResponse.json();
          const elapsedSeconds = Math.round((Date.now() - startedAt) / 1000);
          setTopologyProgress(Math.max(5, Math.min(100, jobData.progress_percent || 0)));
          let message = jobData.stage_label || 'Строим topology graph...';
          if (elapsedSeconds > 60 && jobData.status === 'running') message = `${message} Это занимает дольше обычного.`;
          if (elapsedSeconds > 180 && jobData.status === 'running') message = `${message} Обрабатывается крупный граф, можно подождать или попробовать меньший набор округов.`;
          setTopologyProgressMessage(message);
          if (jobData.status === 'done') {
            window.clearInterval(timer);
            setTopologyProgress(100);
            setTopologyProgressMessage('Topology graph построен');
            window.setTimeout(() => { setTopologyProgress(0); setTopologyProgressMessage(''); }, 700);
            resolve(jobData.result || jobData);
          }
          if (jobData.status === 'failed') {
            window.clearInterval(timer);
            reject(new Error(jobData.error_text || 'Ошибка построения topology graph'));
          }
        } catch (err) {
          window.clearInterval(timer);
          reject(err);
        }
      }, 1000);
    });
  }, []);

  const handleBuildVirtualRoute = useCallback(async () => {
    if (!virtualOriginStation?.id) {
      setVirtualRouteError('Выберите станцию отправления.');
      return;
    }
    if (!virtualDestinationStation?.id) {
      setVirtualRouteError('Выберите станцию назначения.');
      return;
    }

    const virtualScopeRegionCodes = buildVirtualScopeRegionCodes(virtualOriginStation, virtualDestinationStation);
    const missingRegionCodes = virtualScopeRegionCodes.filter((code) => !loadedRegionCodes.includes(code));
    if (missingRegionCodes.length > 0) {
      setVirtualRouteError(`Для маршрута нужно загрузить округа: ${missingRegionCodes.map(formatRegionShortCode).join(', ')}.`);
      return;
    }

    try {
      setVirtualRouteLoading(true);
      setVirtualRouteError('');
      setVirtualRouteMessage('Подготавливаем topology graph...');
      routeDebug('Virtual route request', { origin_station_id: virtualOriginStation?.id, destination_station_id: virtualDestinationStation?.id, scope_region_codes: virtualScopeRegionCodes });
      await ensureTopologyForRegions(virtualScopeRegionCodes);
      setVirtualRouteMessage('Строим виртуальный путь по OSM...');
      const response = await fetch(`${BACKEND_URL}/api/virtual-routes/path`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ origin_station_id: virtualOriginStation.id, destination_station_id: virtualDestinationStation.id, scope_region_codes: virtualScopeRegionCodes }) });
      if (!response.ok) throw new Error(await readApiError(response, 'Не удалось построить виртуальный OSM-маршрут'));
      const data = await response.json();
      routeDebug('Virtual route response', { status: data.status, message: data.message, geometry_ready: Boolean(data.geometry || data.item?.geometry), network_segments_count: (data.network_segments || data.item?.network_segments || []).length, summary: data.summary, diagnostics: data.diagnostics || data.item?.diagnostics });
      if (data.status !== 'ok') {
        setVirtualRouteMessage(data.message || 'Виртуальный путь не построен.');
        return;
      }
      const rawNetworkSegments = data.network_segments || data.item?.network_segments || [];
      const virtualNetworkSegments = rawNetworkSegments.map((segment) => ({ ...segment, segment_source: segment.segment_source || 'virtual_osm_path' }));
      const normalizedVirtualRoute = {
        ...(data.item || {}),
        id: data.route?.id || `virtual-${virtualOriginStation.id}-${virtualDestinationStation.id}`,
        source_system: 'virtual_osm',
        route_name: 'Теоретический путь по OSM',
        origin_station_id: virtualOriginStation.id,
        destination_station_id: virtualDestinationStation.id,
        origin_station_name: virtualOriginStation.name,
        destination_station_name: virtualDestinationStation.name,
        stops: data.stops || data.item?.stops || [],
        geometry: data.geometry || data.item?.geometry || null,
        geometry_source: 'virtual_osm_path',
        network_segments: virtualNetworkSegments,
        diagnostics: data.diagnostics || data.item?.diagnostics || null,
        stops_count: 2,
        matched_stops_count: 2,
        unresolved_stops_count: 0,
        notes: 'Виртуальный маршрут построен по OSM topology graph. Это не расписание РЖД.',
      };
      setSelectedRoute(normalizedVirtualRoute);
      setSidebarMode('virtual');
      setVirtualRouteMessage('Виртуальный путь построен и отображен на карте.');
    } catch (err) {
      console.error(err);
      setVirtualRouteError(err instanceof Error ? err.message : 'Ошибка построения виртуального маршрута');
    } finally {
      setVirtualRouteLoading(false);
    }
  }, [virtualOriginStation, virtualDestinationStation, loadedRegionCodes, ensureTopologyForRegions]);

  const handleImportRzdTrain = useCallback(async (train) => {
    if (!train?.train_number) {
      setRzdError('Не выбран номер поезда.');
      return;
    }
    try {
      setRzdImportLoading(true);
      setRzdError('');
      setRzdMessage('Импортируем выбранный поезд...');
      const response = await fetch(`${BACKEND_URL}/api/rzd/trains/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          train_number: train.train_number,
          dep_date: train.search_date || rzdDepDate,
          origin_code: train.used_origin_code || null,
          destination_code: train.used_destination_code || null,
          origin_station_name: rzdOriginStation?.name || train.origin_name || null,
          destination_station_name: rzdDestinationStation?.name || train.destination_name || null,
          route_name: train.brand || `Поезд ${train.train_number}`,
          notes: 'Imported from RZD API via frontend A-B flow',
        }),
      });
      if (!response.ok) throw new Error(await readApiError(response, 'Не удалось импортировать выбранный поезд'));
      const data = await response.json();
      routeDebug('RZD train import response', data);
      const routeId = data.route_id || data.item?.id || data.route?.id;
      if (!routeId) throw new Error('Импорт выполнен, но backend не вернул route_id');
      setRzdMessage(data.message || 'Маршрут импортирован. Загружаем его на карту...');
      await handleSelectRoute(routeId);
      setSidebarMode('routes');
    } catch (err) {
      console.error(err);
      setRzdError(err instanceof Error ? err.message : 'Ошибка импорта поезда');
    } finally {
      setRzdImportLoading(false);
    }
  }, [rzdDepDate, rzdOriginStation, rzdDestinationStation, handleSelectRoute]);

  const runRouteAnalytics = useCallback(async () => {
    if (!selectedRoute) {
      setAnalyticsError('Сначала выберите маршрут.');
      return;
    }
    try {
      setAnalyticsLoading(true);
      setAnalyticsError('');
      setSelectedAnalyticsCandidate(null);
      let result;
      const routeGeometry = selectedRoute.geometry || null;
      if (routeGeometry) {
        const stopsWithStationIds = (selectedRoute.stops || []).filter((stop) => stop.station_id);
        const startStationId = selectedRoute.origin_station_id || stopsWithStationIds[0]?.station_id || virtualOriginStation?.id || rzdOriginStation?.id || null;
        const endStationId = selectedRoute.destination_station_id || stopsWithStationIds[stopsWithStationIds.length - 1]?.station_id || virtualDestinationStation?.id || rzdDestinationStation?.id || null;
        result = await analyzeVirtualRouteCorridor({ routeGeojson: routeGeometry, startStationId, endStationId, params: analyticsParams });
      } else {
        result = await analyzeRealRouteCorridor(selectedRoute.id, analyticsParams);
      }
      setAnalyticsResult(result);
    } catch (err) {
      console.error(err);
      setAnalyticsError(err instanceof Error ? err.message : 'Ошибка аналитики маршрута');
    } finally {
      setAnalyticsLoading(false);
    }
  }, [selectedRoute, virtualOriginStation, virtualDestinationStation, rzdOriginStation, rzdDestinationStation, analyticsParams]);

  const runRouteAlternatives = useCallback(async () => {
    if (!selectedRoute) {
      setAlternativesError('Сначала выберите маршрут.');
      return;
    }

    const nextRunId = Date.now();
    analysisRunIdRef.current = nextRunId;
    setAnalysisRunId(nextRunId);

    const isVirtualRoute = selectedRoute.source_system === 'virtual_osm' || selectedRoute.geometry_source === 'virtual_osm_path' || String(selectedRoute.id || '').startsWith('virtual-');

    try {
      setAlternativesLoading(true);
      setAlternativesProgress(5);
      setAlternativesError('');
      setRouteAlternatives(null);
      setSelectedAlternativeId(null);
      setSelectedAnalysisRouteId('original');
      setPopulationStatsByRouteId({});
      setHeatmapData(null);
      setHeatmapSettlements([]);
      setHeatmapRouteId(null);
      setShowAnalyticsHeatmap(false);
      setShowAnalyticsPoints(false);
      setSelectedAnalyticsCandidate(null);

      let result;

      if (isVirtualRoute) {
        const originStationId = selectedRoute.origin_station_id || selectedRoute.originStationId || selectedRoute.stops?.[0]?.station_id || virtualOriginStation?.id || rzdOriginStation?.id || null;
        const destinationStationId = selectedRoute.destination_station_id || selectedRoute.destinationStationId || selectedRoute.stops?.[selectedRoute.stops.length - 1]?.station_id || virtualDestinationStation?.id || rzdDestinationStation?.id || null;

        if (!originStationId || !destinationStationId) throw new Error('Для построения альтернатив виртуального маршрута нужны station_id начальной и конечной станции.');

        result = await buildAlternativesByStations(originStationId, destinationStationId, alternativesParams);
      } else {
        result = await buildRouteAlternatives(selectedRoute.id, alternativesParams);
      }

      if (analysisRunIdRef.current !== nextRunId) {
        return;
      }

      console.log('[ANALYTICS REBUILD]', {
        analysisRunId: nextRunId,
        alternativesFromApi: result?.alternatives?.length ?? 0,
        alternativeIds: result?.alternatives?.map((item) => item.id),
      });

      const alternativesWithoutBase = {
        ...result,
        alternatives: (result.alternatives || [])
          .map((item, index) => ({ ...item, display_rank: index + 1 })),
      };

      setRouteAlternatives(alternativesWithoutBase);
      setAlternativesProgress(100);

      setSelectedAlternativeId(null);
      setSelectedAnalysisRouteId('original');
    } catch (err) {
      console.error(err);
      setAlternativesError(err instanceof Error ? err.message : 'Ошибка построения альтернативных маршрутов');
    } finally {
      window.setTimeout(() => {
        if (analysisRunIdRef.current !== nextRunId) return;
        setAlternativesLoading(false);
        setAlternativesProgress(0);
      }, 600);
    }
  }, [selectedRoute, virtualOriginStation, virtualDestinationStation, rzdOriginStation, rzdDestinationStation, alternativesParams]);

  const handleEnterAnalyticsMode = useCallback(async () => {
    if (!selectedRoute) return;
    setMapMode('analytics');
    setSidebarMode('routes');
    const nextRunId = Date.now();
    analysisRunIdRef.current = nextRunId;
    setAnalysisRunId(nextRunId);
    setHeatmapData(null);
    setHeatmapSettlements([]);
    setPopulationStatsByRouteId({});
    setHeatmapRouteId(null);
    setShowAnalyticsHeatmap(false);
    setShowAnalyticsPoints(false);
    await runRouteAnalytics();
  }, [selectedRoute, runRouteAnalytics]);

  const handleExitAnalyticsMode = useCallback(() => {
    setMapMode('research');
    setAnalyticsResult(null);
    setSelectedAnalyticsCandidate(null);
    setAnalyticsError('');
    const nextRunId = Date.now();
    analysisRunIdRef.current = nextRunId;
    setAnalysisRunId(nextRunId);
    setRouteAlternatives(null);
    setAlternativesError('');
    setSelectedAlternativeId(null);
    setSelectedAnalysisRouteId('original');
    setHeatmapData(null);
    setHeatmapSettlements([]);
    setPopulationStatsByRouteId({});
    setHeatmapRouteId(null);
    setShowAnalyticsHeatmap(false);
    setShowAnalyticsPoints(false);
  }, []);

  const handleShowHeatmapForSelectedRoute = useCallback(async () => {
    if (!selectedAnalysisRoute?.geometry) return;

    try {
      setHeatmapLoading(true);
      setShowAnalyticsHeatmap(true);
      setShowAnalyticsPoints(true);
      setAnalyticsError('');
      setHeatmapData(null);
      setHeatmapSettlements([]);

      const currentRouteId = selectedAnalysisRoute.id;
      const currentRunId = analysisRunIdRef.current;

      const payload = await buildPopulationHeatmapByGeometry({
        geometry: selectedAnalysisRoute.geometry,
        params: analyticsParams,
      });

      if (analysisRunIdRef.current !== currentRunId || selectedAnalysisRouteIdAppRef.current !== currentRouteId) {
        return;
      }

      const settlements = extractHeatmapSettlements(payload);

      setHeatmapData(payload);
      setHeatmapSettlements(settlements);
      setHeatmapRouteId(currentRouteId);
    } catch (err) {
      console.error('Heatmap build failed:', err);
      setAnalyticsError(err instanceof Error ? err.message : 'Ошибка построения тепловой карты');
    } finally {
      setHeatmapLoading(false);
    }
  }, [selectedAnalysisRoute, selectedAnalysisRouteId, analyticsParams]);

  const handleHideHeatmap = useCallback(() => {
    setHeatmapData(null);
    setHeatmapSettlements([]);
    setHeatmapRouteId(null);
    setShowAnalyticsHeatmap(false);
    setShowAnalyticsPoints(false);
    setSelectedAnalyticsCandidate(null);
  }, []);

  const handleSelectAnalysisRoute = useCallback((routeId) => {
    setSelectedAnalysisRouteId(routeId);
    setSelectedAlternativeId(routeId === 'original' ? null : routeId);
    setHeatmapData(null);
    setHeatmapSettlements([]);
    setHeatmapRouteId(null);
    setShowAnalyticsHeatmap(false);
    setShowAnalyticsPoints(false);
    setSelectedAnalyticsCandidate(null);
  }, []);

  const handleSelectAlternative = useCallback((alternativeId) => {
    if (!alternativeId) return;
    handleSelectAnalysisRoute(alternativeId);
  }, [handleSelectAnalysisRoute]);

  const handleCheckRegionUpdates = useCallback(async (regionCode) => {
    if (!regionCode) return;
    setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'checking', message: 'Проверяем наличие обновлений...', can_update: false } }));
    try {
      const response = await fetch(`${BACKEND_URL}/api/updates/check?region_code=${encodeURIComponent(regionCode)}`);
      if (!response.ok) throw new Error('Не удалось проверить наличие обновлений');
      const data = await response.json();
      const item = data.item || DEFAULT_UPDATE_STATE;
      setUpdateStates((prev) => ({ ...prev, [regionCode]: item }));
    } catch (err) {
      console.error(err);
      setUpdateStates((prev) => ({ ...prev, [regionCode]: { status: 'check_failed', message: 'Не удалось проверить наличие обновлений', error: err instanceof Error ? err.message : 'Неизвестная ошибка', can_update: false } }));
    }
  }, []);

  const handleRunRegionUpdate = useCallback(async (regionCode) => {
    if (!regionCode) return;
    setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'starting', message: 'Запускаем обновление...', can_update: false } }));
    try {
      const response = await fetch(`${BACKEND_URL}/api/updates/run`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ region_code: regionCode }) });
      if (!response.ok) throw new Error('Не удалось запустить обновление');
      const data = await response.json();
      if (data.status === 'started' || data.status === 'already_running') {
        setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'running', message: 'Обновление выполняется...', can_update: false } }));
        return;
      }
      if (data.status === 'not_required') {
        setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(data.item || DEFAULT_UPDATE_STATE), status: 'up_to_date', message: 'Обновление не требуется', can_update: false } }));
        return;
      }
      if (data.status === 'check_failed') {
        setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(data.item || DEFAULT_UPDATE_STATE), status: 'check_failed', message: 'Не удалось проверить наличие обновлений', can_update: false } }));
      }
    } catch (err) {
      console.error(err);
      setUpdateStates((prev) => ({ ...prev, [regionCode]: { ...(prev[regionCode] || DEFAULT_UPDATE_STATE), status: 'failed', message: 'Не удалось запустить обновление', notes: err instanceof Error ? err.message : 'Неизвестная ошибка', can_update: false } }));
    }
  }, []);

  const handleCheckAllUpdates = useCallback(async () => {
    setCheckingAllUpdates(true);
    try {
      const response = await fetch(`${BACKEND_URL}/api/updates/check-all`);
      if (!response.ok) throw new Error('Не удалось проверить обновления по всем округам');
      const data = await response.json();
      const items = data.items || [];
      const nextStates = {};
      for (const item of items) nextStates[item.region_code] = item;
      setUpdateStates((prev) => ({ ...prev, ...nextStates }));
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Ошибка проверки обновлений');
    } finally {
      setCheckingAllUpdates(false);
    }
  }, []);

  const handleRunAllAvailableUpdates = useCallback(async () => {
    setRunningAllUpdates(true);
    try {
      const response = await fetch(`${BACKEND_URL}/api/updates/run-all-available`, { method: 'POST' });
      if (!response.ok) throw new Error('Не удалось запустить обновление для всех доступных округов');
      const data = await response.json();
      const startedRegions = data.regions_started || [];
      if (startedRegions.length > 0) {
        setUpdateStates((prev) => {
          const next = { ...prev };
          for (const regionCode of startedRegions) next[regionCode] = { ...(next[regionCode] || DEFAULT_UPDATE_STATE), status: 'running', message: 'Обновление выполняется...', can_update: false };
          return next;
        });
      }
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Ошибка запуска обновлений');
    } finally {
      setRunningAllUpdates(false);
    }
  }, []);

  return (
    <div className="app">
      <Header mode={appMode} />
      <main className="main" style={{ display: 'flex', gap: 16, minHeight: 0 }}>
        {!selectionMode && mapMode === 'analytics' ? (
          <AnalyticsPanel
            selectedRoute={selectedRoute}
            params={analyticsParams}
            setParams={setAnalyticsParams}
            analyticsResult={analyticsResult}
            analyticsLoading={analyticsLoading}
            analyticsError={analyticsError}
            onRunAnalysis={runRouteAnalytics}
            onExitAnalytics={handleExitAnalyticsMode}
            selectedCandidate={selectedAnalyticsCandidate}
            onSelectCandidate={setSelectedAnalyticsCandidate}
            showPoints={showAnalyticsPoints}
            setShowPoints={setShowAnalyticsPoints}
            alternativesParams={alternativesParams}
            setAlternativesParams={setAlternativesParams}
            routeAlternatives={routeAlternatives}
            alternativesLoading={alternativesLoading}
            alternativesProgress={alternativesProgress}
            alternativesError={alternativesError}
            onRunAlternatives={runRouteAlternatives}
            selectedAlternativeId={selectedAlternativeId}
            onSelectAlternative={handleSelectAlternative}
            showAlternatives={showAlternatives}
            setShowAlternatives={setShowAlternatives}
          />
        ) : !selectionMode && (
          <LoadedSidebar
            panelMode={sidebarMode}
            setPanelMode={setSidebarMode}
            loading={loading}
            error={error}
            stations={stations}
            linesCount={linesCount}
            onSearch={handleSearch}
            selectedStation={selectedStation}
            onSelectStation={handleSelectStation}
            loadedRegionCodes={loadedRegionCodes}
            showServiceLines={showServiceLines}
            setShowServiceLines={setShowServiceLines}
            onBackToSelection={handleBackToSelection}
            isSearchMode={isSearchMode}
            searchSections={searchSections}
            routes={routes}
            routesLoading={routesLoading}
            routesError={routesError}
            routeSearchQuery={routeSearchQuery}
            setRouteSearchQuery={setRouteSearchQuery}
            selectedRoute={selectedRoute}
            onSelectRoute={handleSelectRoute}
            onClearRoute={handleClearRoute}
            stationRoutes={stationRoutes}
            rzdOriginStation={rzdOriginStation}
            rzdDestinationStation={rzdDestinationStation}
            onSelectRzdOrigin={handleSelectRzdOrigin}
            onSelectRzdDestination={handleSelectRzdDestination}
            rzdDepDate={rzdDepDate}
            setRzdDepDate={setRzdDepDate}
            rzdTrains={rzdTrains}
            rzdSearchLoading={rzdSearchLoading}
            rzdImportLoading={rzdImportLoading}
            rzdError={rzdError}
            rzdMessage={rzdMessage}
            rzdCalendarDebug={rzdCalendarDebug}
            onSearchRzdRoutes={handleSearchRzdRoutes}
            onImportRzdTrain={handleImportRzdTrain}
            rzdSearchProgress={rzdSearchProgress}
            rzdSearchProgressMessage={rzdSearchProgressMessage}
            virtualOriginStation={virtualOriginStation}
            virtualDestinationStation={virtualDestinationStation}
            onSelectVirtualOrigin={handleSelectVirtualOrigin}
            onSelectVirtualDestination={handleSelectVirtualDestination}
            onBuildVirtualRoute={handleBuildVirtualRoute}
            virtualRouteLoading={virtualRouteLoading}
            virtualRouteError={virtualRouteError}
            virtualRouteMessage={virtualRouteMessage}
            topologyProgress={topologyProgress}
            topologyProgressMessage={topologyProgressMessage}
            onEnterAnalytics={handleEnterAnalyticsMode}
          />
        )}

        <div style={{ position: 'relative', flex: 1, minWidth: 0, minHeight: 0, height: '100%', display: 'flex' }}>
          <MapPanel
            federalDistrictsData={federalDistrictsData}
            russiaFeatureData={russiaFeatureData}
            selectionMode={selectionMode}
            activeRegionCode={activeRegionCode}
            pendingRegionCodes={pendingRegionCodes}
            loadedRegionCodes={loadedRegionCodes}
            onActiveRegionChange={setActiveRegionCode}
            stations={stations}
            lines={lines}
            selectedStation={selectedStation}
            onSelectStation={handleSelectStation}
            selectedRoute={selectedRoute}
            loading={loading}
            mapMode={mapMode}
            analyticsResult={analyticsResult}
            selectedAnalyticsCandidate={selectedAnalyticsCandidate}
            onSelectAnalyticsCandidate={setSelectedAnalyticsCandidate}
            showAnalyticsHeatmap={showAnalyticsHeatmap}
            showAnalyticsPoints={showAnalyticsPoints}
            showAlternatives={showAlternatives}
            analyticsParams={analyticsParams}
            heatmapData={heatmapData}
            heatmapSettlements={heatmapSettlements}
            analysisRoutes={analysisRoutes}
            selectedAnalysisRouteId={selectedAnalysisRouteId}
            onSelectAnalysisRoute={handleSelectAnalysisRoute}
          />

          {!selectionMode && mapMode !== 'analytics' && selectedRoute && (
            <div style={{ position: 'absolute', left: '50%', bottom: 18, transform: 'translateX(-50%)', zIndex: 20, width: 'min(520px, calc(100% - 32px))' }}>
              <ActionButton variant="primary" onClick={handleEnterAnalyticsMode} fullWidth large>
                Перейти в режим аналитики
              </ActionButton>
            </div>
          )}

          {mapMode === 'analytics' && (
            <AnalyticsRightPanel
              routes={analysisRoutes}
              selectedRouteId={selectedAnalysisRouteId}
              heatmapRouteId={heatmapRouteId}
              heatmapLoading={heatmapLoading}
              populationStatsByRouteId={populationStatsByRouteId}
              analysisRunId={analysisRunId}
              onSelectRoute={handleSelectAnalysisRoute}
              onShowHeatmap={handleShowHeatmapForSelectedRoute}
              onHideHeatmap={handleHideHeatmap}
            />
          )}

          {selectionMode && (
            <RegionSelectionOverlay
              activeRegionCode={activeRegionCode}
              pendingRegionCodes={pendingRegionCodes}
              regionSummaries={regionSummaries}
              onToggleRegion={togglePendingRegion}
              onLoadSelected={handleLoadSelected}
              updateStates={updateStates}
              onCheckRegionUpdates={handleCheckRegionUpdates}
              onRunRegionUpdate={handleRunRegionUpdate}
              onCheckAllUpdates={handleCheckAllUpdates}
              onRunAllAvailableUpdates={handleRunAllAvailableUpdates}
              checkingAllUpdates={checkingAllUpdates}
              runningAllUpdates={runningAllUpdates}
            />
          )}

          {mapMode === 'analytics' && alternativesLoading && (
            <div
              style={{
                position: 'absolute',
                left: 18,
                bottom: 18,
                zIndex: 25,
                width: 'min(380px, calc(100% - 36px))',
                background: '#ffffff',
                border: '1px solid #e5e7eb',
                borderRadius: 14,
                padding: 12,
                boxShadow: '0 8px 24px rgba(15, 23, 42, 0.08)',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 10,
                  fontSize: 13,
                  fontWeight: 700,
                  color: '#0f172a',
                  marginBottom: 8,
                }}
              >
                <span>Построение альтернатив</span>
                <span>{Math.round(alternativesProgress)}%</span>
              </div>

              <div style={{ height: 8, background: '#e5e7eb', borderRadius: 999, overflow: 'hidden' }}>
                <div
                  style={{
                    width: `${Math.max(0, Math.min(100, alternativesProgress))}%`,
                    height: '100%',
                    background: '#2563eb',
                    borderRadius: 999,
                    transition: 'width 0.25s ease',
                  }}
                />
              </div>

              <div style={{ marginTop: 8, fontSize: 12, color: '#64748b' }}>
                Анализируется topology graph и подбираются отличающиеся маршруты...
              </div>
            </div>
          )}

          {loading && <LoadingOverlay title="Загрузка данных" progress={loadingProgress} message={loadingMessage} />}
          {routeLoadingVisible && <LoadingOverlay title="Выбранный маршрут прогружается" progress={routeLoadingProgress} message={routeLoadingMessage} />}
        </div>
      </main>
    </div>
  );
}
