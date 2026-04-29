import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import './index.css';
import 'ol/ol.css';

import Map from 'ol/Map.js';
import View from 'ol/View.js';
import TileLayer from 'ol/layer/Tile.js';
import VectorLayer from 'ol/layer/Vector.js';
import VectorImageLayer from 'ol/layer/VectorImage.js';
import OSM from 'ol/source/OSM.js';
import VectorSource from 'ol/source/Vector.js';
import Feature from 'ol/Feature.js';
import Point from 'ol/geom/Point.js';
import GeoJSON from 'ol/format/GeoJSON.js';
import { fromLonLat, transformExtent } from 'ol/proj.js';
import { Style, Stroke, Fill, Circle as CircleStyle, Text } from 'ol/style.js';
import { getCenter } from 'ol/extent.js';

const BACKEND_URL = 'http://127.0.0.1:8000';
const RZD_SEARCH_DAYS_AHEAD = 2;
const DEBUG_ROUTE_FLOW = true;

function routeDebug(label, payload = null) {
  if (!DEBUG_ROUTE_FLOW) {
    return;
  }

  const time = new Date().toISOString();

  if (payload === null || payload === undefined) {
    console.log(`[route-flow ${time}] ${label}`);
    return;
  }

  console.groupCollapsed(`[route-flow ${time}] ${label}`);
  console.log(payload);
  console.groupEnd();
}

const RUSSIA_EXTENT = transformExtent(
  [19, 35, 205, 82],
  'EPSG:4326',
  'EPSG:3857'
);

const REGION_META = [
  {
    code: 'central_fd',
    label: 'Центральный федеральный округ',
    mapLabel: 'Центральный ФО',
  },
  {
    code: 'northwestern_fd',
    label: 'Северо-Западный федеральный округ',
    mapLabel: 'Северо-Западный ФО',
  },
  {
    code: 'south_fd',
    label: 'Южный федеральный округ',
    mapLabel: 'Южный ФО',
  },
  {
    code: 'north_caucasus_fd',
    label: 'Северо-Кавказский федеральный округ',
    mapLabel: 'СКФО',
  },
  {
    code: 'volga_fd',
    label: 'Приволжский федеральный округ',
    mapLabel: 'Приволжский ФО',
  },
  {
    code: 'ural_fd',
    label: 'Уральский федеральный округ',
    mapLabel: 'Уральский ФО',
  },
  {
    code: 'siberian_fd',
    label: 'Сибирский федеральный округ',
    mapLabel: 'Сибирский ФО',
  },
  {
    code: 'far_eastern_fd',
    label: 'Дальневосточный федеральный округ',
    mapLabel: 'Дальневосточный ФО',
  },
];

const REGION_META_BY_CODE = Object.fromEntries(
  REGION_META.map((item) => [item.code, item])
);

const REGION_LABEL_POINTS_LONLAT = {
  northwestern_fd: [42.5, 63.7],
  ural_fd: [67.5, 59.6],
  siberian_fd: [91.2, 59.5],
  far_eastern_fd: [135.0, 62.7],
};

const REGION_FOCUS_CONFIG = {
  central_fd: {
    zoom: 5.6,
  },
  northwestern_fd: {
    centerLonLat: [43.0, 63.0],
    zoom: 4.9,
  },
  south_fd: {
    zoom: 5.8,
  },
  north_caucasus_fd: {
    zoom: 6.1,
  },
  volga_fd: {
    zoom: 5.3,
  },
  ural_fd: {
    centerLonLat: [67.0, 59.5],
    zoom: 5.0,
  },
  siberian_fd: {
    centerLonLat: [92.0, 60.0],
    zoom: 4.8,
  },
  far_eastern_fd: {
    centerLonLat: [135.0, 61.5],
    zoom: 4.4,
  },
};

const DEFAULT_UPDATE_STATE = {
  status: 'not_checked',
  message: 'Проверка обновлений ещё не выполнялась',
  can_update: false,
};

function formatFieldValue(value) {
  if (value === null || value === undefined) {
    return 'не указано';
  }

  if (typeof value === 'string' && value.trim() === '') {
    return 'не указано';
  }

  return value;
}

function formatRouteTitle(route) {
  if (!route) {
    return 'Маршрут не выбран';
  }

  if (route.train_number && route.route_name) {
    return `№ ${route.train_number} · ${route.route_name}`;
  }

  if (route.train_number) {
    return `№ ${route.train_number}`;
  }

  return route.route_name || 'Без названия';
}

function formatRouteDirection(route) {
  if (!route) {
    return 'не указано';
  }

  return `${formatFieldValue(route.origin_station_name)} → ${formatFieldValue(
    route.destination_station_name
  )}`;
}

function formatRouteDate(value) {
  if (!value) {
    return 'не указано';
  }

  return value;
}

function buildRouteMetaLine(route) {
  const stopsCount = route?.stops_count ?? route?.total_stops_count ?? 0;
  const matchedCount = route?.matched_stops_count ?? 0;
  const unresolvedCount =
    route?.unresolved_stops_count ??
    Math.max(0, stopsCount - matchedCount);

  return `Остановок: ${stopsCount} • Смэтчено: ${matchedCount} • Без match: ${unresolvedCount}`;
}

function pickPreferredRzdCode(codeCandidates) {
  if (!Array.isArray(codeCandidates) || codeCandidates.length === 0) {
    return null;
  }

  return (
    codeCandidates.find((item) => item.source === 'esr_user') ||
    codeCandidates.find((item) => item.source === 'uic_ref') ||
    codeCandidates[0]
  );
}

function pickPreferredRzdCodeFromStation(station) {
  if (!station) {
    return null;
  }

  if (station.esr_user && String(station.esr_user).trim()) {
    return {
      source: 'esr_user',
      code: String(station.esr_user).trim(),
    };
  }

  if (station.uic_ref && String(station.uic_ref).trim()) {
    return {
      source: 'uic_ref',
      code: String(station.uic_ref).trim(),
    };
  }

  return null;
}

function getDefaultRzdDate() {
  return new Date().toISOString().slice(0, 10);
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

function buildVirtualScopeRegionCodes(originStation, destinationStation) {
  const result = [];
  const seen = new Set();

  for (const code of [originStation?.region_code, destinationStation?.region_code]) {
    if (!code || seen.has(code)) {
      continue;
    }

    seen.add(code);
    result.push(code);
  }

  return result;
}

function getUpdateStatusPresentation(status) {
  switch (status) {
    case 'update_available':
      return {
        label: 'Найдены обновления',
        background: '#fff7ed',
        border: '#fdba74',
        color: '#9a3412',
      };
    case 'up_to_date':
      return {
        label: 'Обновление не требуется',
        background: '#f0fdf4',
        border: '#86efac',
        color: '#166534',
      };
    case 'check_failed':
      return {
        label: 'Не удалось проверить',
        background: '#fef2f2',
        border: '#fca5a5',
        color: '#991b1b',
      };
    case 'running':
      return {
        label: 'Обновление выполняется',
        background: '#eff6ff',
        border: '#93c5fd',
        color: '#1d4ed8',
      };
    case 'finished':
      return {
        label: 'Обновление завершено',
        background: '#f0fdf4',
        border: '#86efac',
        color: '#166534',
      };
    case 'failed':
      return {
        label: 'Ошибка обновления',
        background: '#fef2f2',
        border: '#fca5a5',
        color: '#991b1b',
      };
    case 'checking':
      return {
        label: 'Проверка обновлений...',
        background: '#eff6ff',
        border: '#93c5fd',
        color: '#1d4ed8',
      };
    case 'starting':
      return {
        label: 'Запуск обновления...',
        background: '#eff6ff',
        border: '#93c5fd',
        color: '#1d4ed8',
      };
    case 'already_running':
      return {
        label: 'Обновление уже выполняется',
        background: '#eff6ff',
        border: '#93c5fd',
        color: '#1d4ed8',
      };
    default:
      return {
        label: 'Не проверялось',
        background: '#f8fafc',
        border: '#cbd5e1',
        color: '#475569',
      };
  }
}

function deriveSearchSections(items, loadedRegionCodes) {
  const selected = [];
  const other = [];

  for (const item of items) {
    if (loadedRegionCodes.includes(item.region_code)) {
      selected.push(item);
    } else {
      other.push(item);
    }
  }

  return { selected, other };
}

function Header({ selectionMode, sidebarMode }) {
  const badgeText = selectionMode
    ? 'Режим выбора федеральных округов'
    : sidebarMode === 'routes'
      ? 'Режим маршрутов'
      : sidebarMode === 'virtual'
        ? 'Режим виртуальных маршрутов'
        : 'Режим инфраструктуры';

  return (
    <header className="header">
      <div>
        <h1>Прототип Web-GIS железнодорожной инфраструктуры</h1>
        <p>React + OpenLayers + FastAPI + PostgreSQL/PostGIS</p>
      </div>
      <div className="header-badge">{badgeText}</div>
    </header>
  );
}

function ActionButton({
  children,
  variant = 'secondary',
  disabled = false,
  onClick,
  fullWidth = false,
  large = false,
}) {
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
        <div style={{ fontSize: 18, fontWeight: 700, color: '#111827', marginBottom: 10 }}>
          {title}
        </div>
        <div style={{ fontSize: 14, color: '#475569', marginBottom: 14 }}>{message}</div>
        <div
          style={{
            width: '100%',
            height: 10,
            background: '#e5e7eb',
            borderRadius: 999,
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              width: `${Math.max(0, Math.min(100, progress))}%`,
              height: '100%',
              background: '#374151',
              transition: 'width 220ms ease',
            }}
          />
        </div>
        <div style={{ marginTop: 10, fontSize: 13, color: '#64748b' }}>
          {Math.round(progress)}%
        </div>
      </div>
    </div>
  );
}

function UpdateStatusBox({ item }) {
  const presentation = getUpdateStatusPresentation(item?.status);

  return (
    <div
      style={{
        background: presentation.background,
        border: `1px solid ${presentation.border}`,
        color: presentation.color,
        borderRadius: 14,
        padding: '12px 14px',
        marginBottom: 12,
      }}
    >
      <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 4 }}>
        {presentation.label}
      </div>
      <div style={{ fontSize: 13, lineHeight: 1.45 }}>
        {item?.message || 'Статус недоступен'}
      </div>
      {item?.error && (
        <div style={{ fontSize: 12, lineHeight: 1.45, marginTop: 8, opacity: 0.9 }}>
          {item.error}
        </div>
      )}
      {item?.notes && (
        <div style={{ fontSize: 12, lineHeight: 1.45, marginTop: 8, opacity: 0.9, whiteSpace: 'pre-wrap' }}>
          {item.notes}
        </div>
      )}
    </div>
  );
}

function UpdateStatusBadge({ item }) {
  const presentation = getUpdateStatusPresentation(item?.status);

  return (
    <span
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        padding: '4px 8px',
        borderRadius: 999,
        fontSize: 12,
        fontWeight: 700,
        background: presentation.background,
        border: `1px solid ${presentation.border}`,
        color: presentation.color,
        whiteSpace: 'nowrap',
      }}
    >
      {presentation.label}
    </span>
  );
}

function RegionUpdatesCompactList({ updateStates }) {
  return (
    <div
      style={{
        display: 'grid',
        gap: 8,
        maxHeight: 220,
        overflowY: 'auto',
        paddingRight: 2,
      }}
    >
      {REGION_META.map((region) => {
        const item = updateStates[region.code] || DEFAULT_UPDATE_STATE;

        return (
          <div
            key={region.code}
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              gap: 12,
              padding: '8px 10px',
              borderRadius: 12,
              background: '#f8fafc',
              border: '1px solid rgba(148,163,184,0.18)',
            }}
          >
            <div style={{ fontSize: 13, color: '#111827', lineHeight: 1.3 }}>
              {region.mapLabel}
            </div>
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
  const updateItem = activeRegionCode
    ? (updateStates[activeRegionCode] || DEFAULT_UPDATE_STATE)
    : DEFAULT_UPDATE_STATE;

  const updatesAvailableCount = Object.values(updateStates).filter(
    (item) => item.status === 'update_available'
  ).length;

  const checkingFailedCount = Object.values(updateStates).filter(
    (item) => item.status === 'check_failed'
  ).length;

  return (
    <>
      <div
        style={{
          position: 'absolute',
          top: 16,
          right: 16,
          zIndex: 20,
          width: 400,
          maxWidth: 'calc(100% - 32px)',
          background: 'rgba(255,255,255,0.96)',
          backdropFilter: 'blur(8px)',
          border: '1px solid rgba(148,163,184,0.35)',
          borderRadius: 20,
          boxShadow: '0 14px 34px rgba(15, 23, 42, 0.12)',
          padding: 18,
        }}
      >
        {!regionMeta ? (
          <>
            <div style={{ fontSize: 13, color: '#64748b', marginBottom: 8 }}>
              Стартовый экран
            </div>
            <h2 style={{ margin: '0 0 10px 0', fontSize: 22, lineHeight: 1.2 }}>
              Выбери федеральный округ на карте
            </h2>
            <p style={{ margin: '0 0 14px 0', color: '#475569', lineHeight: 1.5 }}>
              Наведи курсор на округ, кликни по нему и отметь чекбокс в карточке.
            </p>
          </>
        ) : (
          <>
            <div style={{ fontSize: 13, color: '#64748b', marginBottom: 6 }}>
              Выбор округа
            </div>
            <h2 style={{ margin: '0 0 10px 0', fontSize: 22, lineHeight: 1.2 }}>
              {regionMeta.label}
            </h2>

            <div
              style={{
                background: '#f8fafc',
                borderRadius: 14,
                padding: '12px 14px',
                border: '1px solid rgba(148,163,184,0.2)',
                marginBottom: 14,
              }}
            >
              <div style={{ fontSize: 13, color: '#64748b', marginBottom: 4 }}>
                Станций в округе
              </div>
              <div style={{ fontSize: 26, fontWeight: 700, color: '#111827' }}>
                {summary?.stations_count ?? 'не указано'}
              </div>
            </div>

            <label
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 10,
                cursor: 'pointer',
                fontSize: 15,
                color: '#111827',
                marginBottom: 18,
              }}
            >
              <input
                type="checkbox"
                checked={isChecked}
                onChange={() => onToggleRegion(activeRegionCode)}
              />
              <span>Добавить округ к загрузке</span>
            </label>

            <section
              style={{
                borderTop: '1px solid rgba(148,163,184,0.25)',
                paddingTop: 16,
              }}
            >
              <div
                style={{
                  fontSize: 14,
                  fontWeight: 700,
                  color: '#111827',
                  marginBottom: 10,
                }}
              >
                Обновление данных
              </div>

              <UpdateStatusBox item={updateItem} />

              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                <ActionButton
                  variant="secondary"
                  onClick={() => onCheckRegionUpdates(activeRegionCode)}
                  disabled={
                    updateItem.status === 'checking' ||
                    updateItem.status === 'starting' ||
                    updateItem.status === 'running'
                  }
                >
                  Проверить обновления
                </ActionButton>

                {updateItem.status === 'update_available' && (
                  <ActionButton
                    variant="primary"
                    onClick={() => onRunRegionUpdate(activeRegionCode)}
                    disabled={updateItem.status === 'running' || updateItem.status === 'starting'}
                  >
                    Обновить округ
                  </ActionButton>
                )}
              </div>
            </section>
          </>
        )}

        <section
          style={{
            borderTop: '1px solid rgba(148,163,184,0.25)',
            paddingTop: 16,
            marginTop: 18,
          }}
        >
          <div
            style={{
              fontSize: 14,
              fontWeight: 700,
              color: '#111827',
              marginBottom: 10,
            }}
          >
            Обновления по всем округам
          </div>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
            <ActionButton
              variant="secondary"
              onClick={onCheckAllUpdates}
              disabled={checkingAllUpdates || runningAllUpdates}
            >
              {checkingAllUpdates
                ? 'Проверка...'
                : 'Проверить наличие обновлений для всех округов'}
            </ActionButton>

            {updatesAvailableCount > 0 && (
              <ActionButton
                variant="primary"
                onClick={onRunAllAvailableUpdates}
                disabled={runningAllUpdates}
              >
                {runningAllUpdates
                  ? 'Запуск...'
                  : `Обновить все округа с найденными обновлениями (${updatesAvailableCount})`}
              </ActionButton>
            )}
          </div>

          <div
            style={{
              fontSize: 13,
              color: '#64748b',
              lineHeight: 1.5,
              marginBottom: 12,
            }}
          >
            Найдено округов с обновлениями: {updatesAvailableCount}
            <br />
            Ошибок проверки: {checkingFailedCount}
          </div>

          <RegionUpdatesCompactList updateStates={updateStates} />
        </section>
      </div>

      {pendingRegionCodes.length > 0 && (
        <div
          style={{
            position: 'absolute',
            left: '50%',
            bottom: 18,
            transform: 'translateX(-50%)',
            zIndex: 20,
            width: 'min(520px, calc(100% - 32px))',
          }}
        >
          <ActionButton
            variant="primary"
            onClick={onLoadSelected}
            fullWidth
            large
          >
            Загрузить выбранное ({pendingRegionCodes.length})
          </ActionButton>
        </div>
      )}
    </>
  );
}

function StationListBlock({
  title,
  stations,
  selectedStation,
  onSelectStation,
}) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div
        style={{
          fontSize: 13,
          fontWeight: 700,
          color: '#475569',
          marginBottom: 8,
          textTransform: 'uppercase',
          letterSpacing: '0.04em',
        }}
      >
        {title}
      </div>

      {stations.length === 0 ? (
        <p style={{ margin: 0, color: '#64748b' }}>Нет совпадений.</p>
      ) : (
        stations.map((station) => (
          <button
            key={`${station.region_code}-${station.id}`}
            className={
              selectedStation?.id === station.id
                ? 'station-list-item active'
                : 'station-list-item'
            }
            onClick={() => onSelectStation(station.id)}
          >
            <div className="station-list-name">{station.name || 'Без названия'}</div>
            <div className="station-list-meta">
              {station.station_type || 'не указано'} • {station.region || 'не указано'} •{' '}
              {REGION_META_BY_CODE[station.region_code]?.mapLabel || station.region_code}
            </div>
          </button>
        ))
      )}
    </div>
  );
}

function SidebarModeSwitch({ panelMode, setPanelMode }) {
  return (
    <section className="card sidebar-mode-card">
      <div className="sidebar-mode-title">Режим боковой панели</div>
      <div className="panel-mode-switch">
        <button
          className={
            panelMode === 'infrastructure'
              ? 'panel-mode-button active'
              : 'panel-mode-button'
          }
          onClick={() => setPanelMode('infrastructure')}
        >
          Инфраструктура
        </button>
        <button
          className={
            panelMode === 'routes'
              ? 'panel-mode-button active'
              : 'panel-mode-button'
          }
          onClick={() => setPanelMode('routes')}
        >
          Маршруты
        </button>
        <button
          className={
            panelMode === 'virtual'
              ? 'panel-mode-button active'
              : 'panel-mode-button'
          }
          onClick={() => setPanelMode('virtual')}
        >
          Виртуальные маршруты
        </button>
      </div>
    </section>
  );
}

function RouteListCard({
  routes,
  routesLoading,
  routesError,
  routeSearchQuery,
  setRouteSearchQuery,
  onSearchRoutes,
  onResetRoutes,
  selectedRoute,
  onSelectRoute,
}) {
  return (
    <section className="card route-list-card">
      <h2>Маршруты</h2>

      <div className="search-row">
        <input
          value={routeSearchQuery}
          onChange={(e) => setRouteSearchQuery(e.target.value)}
          placeholder="Номер поезда или название маршрута"
        />
        <button onClick={onSearchRoutes}>Найти</button>
      </div>

      <div style={{ display: 'flex', gap: 8, marginTop: 10, marginBottom: 12 }}>
        <button className="subtle-button" onClick={onResetRoutes}>
          Сбросить
        </button>
      </div>

      <p style={{ marginTop: 0 }}>
        В списке показаны импортированные маршруты РЖД. Выбери маршрут, чтобы увидеть остановки
        и его трассу на карте.
      </p>

      {routesError && <p className="error-text">Ошибка: {routesError}</p>}
      {routesLoading ? (
        <p>Загрузка маршрутов...</p>
      ) : routes.length === 0 ? (
        <p>Маршруты не найдены.</p>
      ) : (
        <div
          className="route-list"
          style={{
            maxHeight: 360,
            overflowY: 'auto',
            paddingRight: 4,
          }}
        >
          {routes.map((route) => (
            <button
              key={route.id}
              className={
                selectedRoute?.id === route.id
                  ? 'route-list-item active'
                  : 'route-list-item'
              }
              onClick={() => onSelectRoute(route.id)}
            >
              <div className="route-list-title">{formatRouteTitle(route)}</div>
              <div className="route-list-subtitle">{formatRouteDirection(route)}</div>
              <div className="route-list-meta">{buildRouteMetaLine(route)}</div>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function RouteDetailsCard({
  selectedRoute,
  routesLoading,
  onClearRoute,
  onSelectStation,
}) {
  return (
    <section className="card route-details-card">
      <h2>Карточка маршрута</h2>

      {!selectedRoute ? (
        <p>Выбери маршрут в списке, чтобы увидеть его остановки и отрисовку на карте.</p>
      ) : (
        <>
          <div className="route-details-header">
            <div>
              <div className="route-details-title">{formatRouteTitle(selectedRoute)}</div>
              <div className="route-details-direction">{formatRouteDirection(selectedRoute)}</div>
            </div>
            <button className="subtle-button" onClick={onClearRoute}>
              Снять выбор
            </button>
          </div>

          <div className="route-details-grid">
            <div className="route-chip">
              <span>Дата</span>
              <strong>{formatRouteDate(selectedRoute.snapshot_date)}</strong>
            </div>
            <div className="route-chip">
              <span>Остановок</span>
              <strong>{selectedRoute.stops_count ?? selectedRoute.stops?.length ?? 0}</strong>
            </div>
            <div className="route-chip">
              <span>Смэтчено</span>
              <strong>{selectedRoute.matched_stops_count ?? 0}</strong>
            </div>
            <div className="route-chip">
              <span>Без match</span>
              <strong>{selectedRoute.unresolved_stops_count ?? 0}</strong>
            </div>
          </div>

          {selectedRoute.notes && (
            <div className="route-notes">
              <strong>Примечание:</strong> {selectedRoute.notes}
            </div>
          )}

          {selectedRoute.geometry_source === 'graph_path' && (
            <div className="route-notes">
              <strong>Маршрут:</strong> зелёным выделены реальные OSM-линии железнодорожной сети,
              использованные для построения пути.
            </div>
          )}

          {selectedRoute.geometry_source === 'fallback_station_chain' && (
            <div className="route-notes">
              <strong>Маршрут:</strong> сейчас показана fallback-линия между станциями, потому что
              точный путь по OSM-сегментам не был собран полностью.
            </div>
          )}

          <div className="route-stops-block">
            <div className="route-stops-title">Остановки маршрута</div>

            {routesLoading ? (
              <p>Загрузка маршрута...</p>
            ) : !selectedRoute.stops || selectedRoute.stops.length === 0 ? (
              <p>Остановки отсутствуют.</p>
            ) : (
              <div
                className="route-stop-list"
                style={{
                  maxHeight: 420,
                  overflowY: 'auto',
                  paddingRight: 4,
                }}
              >
                {selectedRoute.stops.map((stop) => {
                  const isMatched = Boolean(stop.station_id);

                  return (
                    <button
                      key={`${selectedRoute.id}-${stop.stop_sequence}`}
                      className={
                        isMatched ? 'route-stop-item matched' : 'route-stop-item unresolved'
                      }
                      onClick={() => {
                        if (stop.station_id) {
                          onSelectStation(stop.station_id);
                        }
                      }}
                      disabled={!stop.station_id}
                    >
                      <div className="route-stop-sequence">{stop.stop_sequence}</div>
                      <div className="route-stop-content">
                        <div className="route-stop-name">
                          {stop.station_name_raw || stop.station_name_matched || 'Без названия'}
                        </div>
                        <div className="route-stop-meta">
                          {stop.station_code_rzd || 'без кода'} •{' '}
                          {stop.arrival_time || '—'} / {stop.departure_time || '—'}
                        </div>
                        <div className="route-stop-status">
                          {isMatched
                            ? `Связано с OSM: ${stop.station_name_matched || `station_id=${stop.station_id}`}`
                            : 'Пока без связи с stations'}
                        </div>
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

function StationRoutesBlock({
  stationRoutes,
  onSelectRoute,
}) {
  if (!stationRoutes || stationRoutes.length === 0) {
    return <p style={{ marginTop: 12 }}>Маршруты через эту станцию пока не найдены.</p>;
  }

  return (
    <div className="station-routes-list">
      {stationRoutes.map((route) => (
        <button
          key={`${route.id}-${route.stop_sequence}`}
          className="station-route-item"
          onClick={() => onSelectRoute(route.id)}
        >
          <div className="station-route-title">{formatRouteTitle(route)}</div>
          <div className="station-route-meta">
            {formatRouteDirection(route)} • остановка № {route.stop_sequence}
          </div>
        </button>
      ))}
    </div>
  );
}

function InfrastructureSidebar({
  loading,
  error,
  stations,
  linesCount,
  searchQuery,
  setSearchQuery,
  onSearch,
  selectedStation,
  onSelectStation,
  loadedRegionCodes,
  showServiceLines,
  setShowServiceLines,
  onBackToSelection,
  isSearchMode,
  searchSections,
}) {
  return (
    <>
      <section className="card">
        <h2>Загруженные округа</h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
          {loadedRegionCodes.map((code) => (
            <span
              key={code}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                padding: '6px 10px',
                borderRadius: 999,
                background: '#e5e7eb',
                color: '#111827',
                fontSize: 13,
                fontWeight: 600,
              }}
            >
              {REGION_META_BY_CODE[code]?.mapLabel || code}
            </span>
          ))}
        </div>
        <button onClick={onBackToSelection}>Изменить выбор округов</button>
      </section>

      <section className="card">
        <h2>Слои</h2>
        <label
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            cursor: 'pointer',
          }}
        >
          <input
            type="checkbox"
            checked={showServiceLines}
            onChange={(e) => setShowServiceLines(e.target.checked)}
          />
          <span>Показывать специальные / служебные пути</span>
        </label>
      </section>

      <section className="card">
        <h2>Поиск станции</h2>
        <div className="search-row">
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Введите название станции"
          />
          <button onClick={onSearch}>Найти</button>
        </div>
        <p style={{ marginTop: '10px' }}>
          {isSearchMode
            ? 'Поиск показывает совпадения по выбранным и остальным зонам.'
            : 'Сейчас показаны все загруженные станции.'}
        </p>
      </section>

      <section className="card">
        <h2>Статус данных</h2>
        <p>Станций в текущем списке: {stations.length}</p>
        <p>Линий: {linesCount}</p>
        <p>Состояние: {loading ? 'загрузка...' : 'готово'}</p>
        {error && <p className="error-text">Ошибка: {error}</p>}
      </section>

      <section className="card">
        <h2>Карточка станции</h2>
        {selectedStation ? (
          <div className="station-details">
            <p><strong>ID:</strong> {formatFieldValue(selectedStation.id)}</p>
            <p><strong>Округ:</strong> {formatFieldValue(selectedStation.region_code)}</p>
            <p><strong>Название:</strong> {formatFieldValue(selectedStation.name)}</p>
            <p><strong>Тип:</strong> {formatFieldValue(selectedStation.station_type)}</p>
            <p>
              <strong>Главная станция:</strong>{' '}
              {selectedStation.is_main_rail_station ? 'да' : 'нет'}
            </p>
            <p>
              <strong>Видимость по умолчанию:</strong>{' '}
              {selectedStation.is_visible_default === false ? 'скрыта' : 'показывается'}
            </p>
            <p>
              <strong>Причина исключения:</strong>{' '}
              {formatFieldValue(selectedStation.exclude_reason)}
            </p>
            <p><strong>Регион:</strong> {formatFieldValue(selectedStation.region)}</p>
            <p><strong>Оператор:</strong> {formatFieldValue(selectedStation.operator_name)}</p>
            <p><strong>Филиал:</strong> {formatFieldValue(selectedStation.operator_branch)}</p>
            <p><strong>UIC:</strong> {formatFieldValue(selectedStation.uic_ref)}</p>
            <p><strong>ESR:</strong> {formatFieldValue(selectedStation.esr_user)}</p>
            <p>
              <strong>Координаты:</strong>{' '}
              {selectedStation.lat !== null && selectedStation.lat !== undefined
                ? selectedStation.lat?.toFixed?.(6) ?? selectedStation.lat
                : 'не указано'}
              ,{' '}
              {selectedStation.lon !== null && selectedStation.lon !== undefined
                ? selectedStation.lon?.toFixed?.(6) ?? selectedStation.lon
                : 'не указано'}
            </p>
          </div>
        ) : (
          <p>Выбери станцию на карте или в списке.</p>
        )}
      </section>

      <section className="card station-list-card">
        <h2>Станции</h2>

        {!isSearchMode ? (
          <div className="station-list">
            {stations.length === 0 ? (
              <p>Нет данных для отображения.</p>
            ) : (
              stations.map((station) => (
                <button
                  key={`${station.region_code}-${station.id}`}
                  className={
                    selectedStation?.id === station.id
                      ? 'station-list-item active'
                      : 'station-list-item'
                  }
                  onClick={() => onSelectStation(station.id)}
                >
                  <div className="station-list-name">{station.name || 'Без названия'}</div>
                  <div className="station-list-meta">
                    {station.station_type || 'не указано'} • {station.region || 'не указано'}
                  </div>
                </button>
              ))
            )}
          </div>
        ) : (
          <div className="station-list">
            <StationListBlock
              title="Результаты по выбранным зонам"
              stations={searchSections.selected}
              selectedStation={selectedStation}
              onSelectStation={onSelectStation}
            />

            <StationListBlock
              title="Результаты по остальным зонам"
              stations={searchSections.other}
              selectedStation={selectedStation}
              onSelectStation={onSelectStation}
            />
          </div>
        )}
      </section>
    </>
  );
}

function RzdRouteSearchCard({
  selectedStation,
  rzdOriginProfile,
  rzdOriginCode,
  setRzdOriginCode,
  rzdDestinationQuery,
  setRzdDestinationQuery,
  rzdDestinationOptions,
  selectedRzdDestination,
  onSearchRzdDestination,
  onSelectRzdDestination,
  rzdDepDate,
  setRzdDepDate,
  rzdIncludeTransfers,
  setRzdIncludeTransfers,
  rzdTrains,
  rzdSearchLoading,
  rzdImportLoading,
  rzdError,
  rzdMessage,
  rzdCalendarDebug,
  onSearchRzdRoutes,
  onImportRzdTrain,
  onBuildVirtualRoute,
  virtualRouteLoading,
  virtualRouteError,
  virtualRouteMessage,
  destinationNearbyRoutes,
  destinationNearbyRoutesLoading,
  onSelectRoute,
  rzdSearchProgress,
  rzdSearchProgressMessage,
}) {
  const canSearch =
    Boolean(selectedStation?.id) &&
    Boolean(selectedRzdDestination?.id) &&
    !rzdSearchLoading;

  return (
    <section className="card">
      <h2>Поиск реального поезда А→Б</h2>

      <p>
        Станция А выбирается из OSM-слоя на карте. Станция Б ищется в локальной OSM-базе,
        а поезд между ними проверяется через РЖД API. После выбора поезда маршрут
        импортируется и отображается на карте.
      </p>

      <div
        style={{
          background: '#f8fafc',
          border: '1px solid rgba(148,163,184,0.25)',
          borderRadius: 14,
          padding: 12,
          marginBottom: 14,
        }}
      >
        <div style={{ fontSize: 13, color: '#64748b', marginBottom: 4 }}>
          Откуда
        </div>

        {selectedStation ? (
          <>
            <div style={{ fontSize: 16, fontWeight: 800, color: '#111827' }}>
              {selectedStation.name || 'Без названия'}
            </div>

            <div style={{ fontSize: 13, color: '#64748b', marginTop: 4 }}>
              {selectedStation.is_main_rail_station ? 'главная станция' : 'обычная станция'}
            </div>

            <div
              style={{
                marginTop: 10,
                fontSize: 13,
                color: '#64748b',
                background: '#f8fafc',
                border: '1px solid #e2e8f0',
                borderRadius: 12,
                padding: 10,
              }}
            >
              Код РЖД API будет подобран автоматически на backend.
            </div>
          </>
        ) : (
          <div style={{ fontSize: 14, color: '#64748b' }}>
            Выбери станцию А на карте или в списке станций.
          </div>
        )}
      </div>

      <div style={{ marginBottom: 14 }}>
        <label
          style={{
            display: 'block',
            fontSize: 13,
            fontWeight: 700,
            color: '#475569',
            marginBottom: 6,
          }}
        >
          Куда
        </label>

        <div className="search-row">
          <input
            value={rzdDestinationQuery}
            onChange={(event) => setRzdDestinationQuery(event.target.value)}
            placeholder="Введите станцию назначения"
          />
          <button onClick={onSearchRzdDestination} disabled={rzdDestinationQuery.trim().length < 2}>
            Найти
          </button>
        </div>

        {selectedRzdDestination && (
          <div
            style={{
              marginTop: 8,
              fontSize: 13,
              color: '#166534',
              background: '#f0fdf4',
              border: '1px solid #bbf7d0',
              borderRadius: 12,
              padding: 10,
            }}
          >
            Выбрано: <strong>{selectedRzdDestination.name}</strong>
          </div>
        )}

        {rzdDestinationOptions.length > 0 && (
          <div
            style={{
              marginTop: 10,
              display: 'grid',
              gap: 8,
              maxHeight: 180,
              overflowY: 'auto',
            }}
          >
            {rzdDestinationOptions.map((option) => (
              <button
                key={option.id}
                className="station-list-item"
                onClick={() => onSelectRzdDestination(option)}
              >
                <div className="station-list-name">{option.name || 'Без названия'}</div>
                <div className="station-list-meta">
                  {option.region || option.region_code || 'регион не указан'} •{' '}
                  {option.is_main_rail_station ? 'главная станция' : 'обычная станция'}
                </div>
              </button>
            ))}
          </div>
        )}

        {selectedRzdDestination && (
          <div
            style={{
              marginTop: 12,
              background: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: 12,
              padding: 10,
            }}
          >
            <div
              style={{
                fontSize: 13,
                fontWeight: 800,
                color: '#334155',
                marginBottom: 8,
              }}
            >
              Уже известные маршруты из зоны назначения
            </div>

            {destinationNearbyRoutesLoading ? (
              <div style={{ fontSize: 13, color: '#64748b' }}>
                Загружаем маршруты...
              </div>
            ) : destinationNearbyRoutes.length === 0 ? (
              <div style={{ fontSize: 13, color: '#64748b' }}>
                В зоне выбранной станции пока нет импортированных маршрутов.
              </div>
            ) : (
              <div
                style={{
                  display: 'grid',
                  gap: 6,
                  maxHeight: 180,
                  overflowY: 'auto',
                  paddingRight: 4,
                }}
              >
                {destinationNearbyRoutes.map((route) => (
                  <button
                    key={`${route.id}-${route.zone_station_id}`}
                    className="station-route-item"
                    onClick={() => onSelectRoute(route.id)}
                  >
                    <div className="station-route-title">
                      {formatRouteTitle(route)}
                    </div>
                    <div className="station-route-meta">
                      {formatRouteDirection(route)} • через {route.zone_station_name}
                      {route.zone_station_distance_km !== null &&
                      route.zone_station_distance_km !== undefined
                        ? ` • ${route.zone_station_distance_km} км`
                        : ''}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </div>

      <div
        style={{
          marginBottom: 14,
          fontSize: 13,
          color: '#64748b',
          background: '#f8fafc',
          border: '1px solid #e2e8f0',
          borderRadius: 12,
          padding: 10,
        }}
      >
        Система проверит ближайшие 2 дня и покажет даты, на которые найдены поезда.
      </div>

      <ActionButton
        variant="primary"
        fullWidth
        onClick={onSearchRzdRoutes}
        disabled={!canSearch}
      >
        {rzdSearchLoading ? 'Ищем поезда...' : 'Найти поезда'}
      </ActionButton>

      {rzdSearchLoading && (
        <div
          style={{
            marginTop: 12,
            background: '#f8fafc',
            border: '1px solid #e2e8f0',
            borderRadius: 12,
            padding: 10,
          }}
        >
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              gap: 8,
              fontSize: 12,
              color: '#475569',
              marginBottom: 7,
            }}
          >
            <span>{rzdSearchProgressMessage || 'Идёт поиск...'}</span>
            <strong>{Math.round(rzdSearchProgress || 0)}%</strong>
          </div>

          <div
            style={{
              width: '100%',
              height: 7,
              background: '#e5e7eb',
              borderRadius: 999,
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                width: `${Math.max(0, Math.min(100, rzdSearchProgress || 0))}%`,
                height: '100%',
                background: '#374151',
                transition: 'width 240ms ease',
              }}
            />
          </div>

          <div
            style={{
              marginTop: 10,
              fontSize: 12,
              color: '#64748b',
              lineHeight: 1.4,
            }}
          >
            Проверяем варианты между выбранными зонами станций...
          </div>
        </div>
      )}

      {rzdError && (
        <div
          style={{
            marginTop: 12,
            fontSize: 13,
            color: '#991b1b',
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: 12,
            padding: 10,
          }}
        >
          {rzdError}
        </div>
      )}

      {rzdMessage && !rzdError && (
        <div
          style={{
            marginTop: 12,
            fontSize: 13,
            color: '#475569',
            background: '#f8fafc',
            border: '1px solid #e2e8f0',
            borderRadius: 12,
            padding: 10,
          }}
        >
          {rzdMessage}
        </div>
      )}

      {rzdCalendarDebug?.fallback_message && (
        <div
          style={{
            marginTop: 12,
            fontSize: 13,
            color: '#92400e',
            background: '#fffbeb',
            border: '1px solid #fcd34d',
            borderRadius: 12,
            padding: 10,
            lineHeight: 1.45,
          }}
        >
          {rzdCalendarDebug.fallback_message}
        </div>
      )}

      {rzdCalendarDebug?.date_summaries?.length > 0 && (
        <div
          style={{
            marginTop: 12,
            background: '#f8fafc',
            border: '1px solid #e2e8f0',
            borderRadius: 12,
            padding: 10,
          }}
        >
          <div
            style={{
              fontSize: 13,
              fontWeight: 800,
              color: '#334155',
              marginBottom: 8,
            }}
          >
            Проверенные даты
          </div>

          <div style={{ display: 'grid', gap: 6, maxHeight: 180, overflowY: 'auto' }}>
            {rzdCalendarDebug.date_summaries.map((item) => (
              <div
                key={item.date}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  gap: 8,
                  fontSize: 12,
                  color: item.status === 'failed' ? '#991b1b' : '#475569',
                  background: item.trains_count > 0 ? '#f0fdf4' : '#ffffff',
                  border: item.status === 'failed'
                    ? '1px solid #fecaca'
                    : '1px solid #e2e8f0',
                  borderRadius: 10,
                  padding: '7px 8px',
                }}
                title={item.error || ''}
              >
                <span>{item.date_rzd || item.date}</span>
                <strong>
                  {item.status === 'failed'
                    ? 'ошибка'
                    : `${item.trains_count || 0} поездов`}
                </strong>
              </div>
            ))}
          </div>
        </div>
      )}

      {rzdCalendarDebug?.code_attempts?.length > 0 && (
        <div
          style={{
            marginTop: 12,
            background: '#f8fafc',
            border: '1px solid #e2e8f0',
            borderRadius: 12,
            padding: 10,
          }}
        >
          <div
            style={{
              fontSize: 13,
              fontWeight: 800,
              color: '#334155',
              marginBottom: 8,
            }}
          >
            Проверенные пары кодов
          </div>

          <div style={{ display: 'grid', gap: 6, maxHeight: 180, overflowY: 'auto' }}>
            {rzdCalendarDebug.code_attempts.map((item, index) => (
              <div
                key={`${item.origin_code}-${item.destination_code}-${index}`}
                style={{
                  fontSize: 12,
                  color: item.status === 'failed' ? '#991b1b' : '#475569',
                  background: item.trains_count > 0 ? '#f0fdf4' : '#ffffff',
                  border: item.status === 'failed'
                    ? '1px solid #fecaca'
                    : '1px solid #e2e8f0',
                  borderRadius: 10,
                  padding: '7px 8px',
                }}
                title={item.error || ''}
              >
                <div>
                  {item.origin_source}: <strong>{item.origin_code}</strong>
                  {' → '}
                  {item.destination_source}: <strong>{item.destination_code}</strong>
                </div>
                <div style={{ marginTop: 3 }}>
                  {item.status === 'failed'
                    ? 'ошибка'
                    : `${item.trains_count || 0} поездов`}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {rzdTrains.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <div
            style={{
              fontSize: 13,
              fontWeight: 700,
              color: '#475569',
              marginBottom: 8,
              textTransform: 'uppercase',
              letterSpacing: '0.04em',
            }}
          >
            Найденные поезда
          </div>

          <div
            className="rzd-train-list"
            style={{
              display: 'grid',
              gap: 8,
              maxHeight: 360,
              overflowY: 'auto',
              paddingRight: 4,
            }}
          >
            {rzdTrains.map((train, index) => (
              <div
                key={`${train.train_number}-${train.search_date || train.departure_date}-${train.departure_time}-${index}`}
                style={{
                  border: '1px solid rgba(148,163,184,0.35)',
                  borderRadius: 14,
                  padding: 12,
                  background: '#ffffff',
                }}
              >
                <div style={{ fontSize: 16, fontWeight: 800, color: '#111827' }}>
                  № {train.train_number}
                  {train.brand ? ` · ${train.brand}` : ''}
                </div>

                <div style={{ fontSize: 13, color: '#475569', marginTop: 4 }}>
                  {train.origin_name || 'откуда не указано'} →{' '}
                  {train.destination_name || 'куда не указано'}
                </div>

                <div style={{ fontSize: 13, color: '#64748b', marginTop: 4 }}>
                  {train.search_date_rzd || train.search_date || train.departure_date || 'дата не указана'} •{' '}
                  {train.departure_time || '—'} → {train.arrival_time || '—'}
                  {train.time_in_way ? ` • в пути ${train.time_in_way}` : ''}
                </div>

                {train.similar_used && (
                  <div
                    style={{
                      marginTop: 6,
                      fontSize: 12,
                      color: '#92400e',
                      background: '#fffbeb',
                      border: '1px solid #fde68a',
                      borderRadius: 10,
                      padding: 8,
                      lineHeight: 1.4,
                    }}
                  >
                    Найдено через похожие станции:{' '}
                    {train.used_origin_station_name || 'станция отправления'} →{' '}
                    {train.used_destination_station_name || 'станция назначения'}
                  </div>
                )}

                <div style={{ marginTop: 10 }}>
                  <ActionButton
                    variant="success"
                    onClick={() => onImportRzdTrain(train)}
                    disabled={rzdImportLoading}
                  >
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

function RoutesSidebar({
  routes,
  routesLoading,
  routesError,
  routeSearchQuery,
  setRouteSearchQuery,
  onSearchRoutes,
  onResetRoutes,
  selectedRoute,
  onSelectRoute,
  onClearRoute,
  onSelectStation,
  selectedStation,
  stationRoutes,

  rzdOriginProfile,
  rzdOriginCode,
  setRzdOriginCode,
  rzdDestinationQuery,
  setRzdDestinationQuery,
  rzdDestinationOptions,
  selectedRzdDestination,
  onSearchRzdDestination,
  onSelectRzdDestination,
  rzdDepDate,
  setRzdDepDate,
  rzdIncludeTransfers,
  setRzdIncludeTransfers,
  rzdTrains,
  rzdSearchLoading,
  rzdImportLoading,
  rzdError,
  rzdMessage,
  rzdCalendarDebug,
  onSearchRzdRoutes,
  onImportRzdTrain,
  onBuildVirtualRoute,
  virtualRouteLoading,
  virtualRouteError,
  virtualRouteMessage,
  destinationNearbyRoutes,
  destinationNearbyRoutesLoading,
  rzdSearchProgress,
  rzdSearchProgressMessage,
}) {
  return (
    <>
      <section className="card">
        <h2>Режим маршрутов</h2>
        <p>
          Здесь собраны только функции, относящиеся к маршрутам.
        </p>
        <p>
          Выбранный маршрут отображается на карте целиком, даже если выходит за пределы
          загруженных федеральных округов.
        </p>
        <p>
          Общий список сохранённых маршрутов скрыт из пользовательского интерфейса. БД
          используется как внутренний cache; на карте показывается только выбранный или
          только что импортированный маршрут.
        </p>
      </section>

      <RzdRouteSearchCard
        selectedStation={selectedStation}
        rzdOriginProfile={rzdOriginProfile}
        rzdOriginCode={rzdOriginCode}
        setRzdOriginCode={setRzdOriginCode}
        rzdDestinationQuery={rzdDestinationQuery}
        setRzdDestinationQuery={setRzdDestinationQuery}
        rzdDestinationOptions={rzdDestinationOptions}
        selectedRzdDestination={selectedRzdDestination}
        onSearchRzdDestination={onSearchRzdDestination}
        onSelectRzdDestination={onSelectRzdDestination}
        rzdDepDate={rzdDepDate}
        setRzdDepDate={setRzdDepDate}
        rzdIncludeTransfers={rzdIncludeTransfers}
        setRzdIncludeTransfers={setRzdIncludeTransfers}
        rzdTrains={rzdTrains}
        rzdSearchLoading={rzdSearchLoading}
        rzdImportLoading={rzdImportLoading}
        rzdError={rzdError}
        rzdMessage={rzdMessage}
        rzdCalendarDebug={rzdCalendarDebug}
        onSearchRzdRoutes={onSearchRzdRoutes}
        onImportRzdTrain={onImportRzdTrain}
        onBuildVirtualRoute={onBuildVirtualRoute}
        virtualRouteLoading={virtualRouteLoading}
        virtualRouteError={virtualRouteError}
        virtualRouteMessage={virtualRouteMessage}
        destinationNearbyRoutes={destinationNearbyRoutes}
        destinationNearbyRoutesLoading={destinationNearbyRoutesLoading}
        onSelectRoute={onSelectRoute}
        rzdSearchProgress={rzdSearchProgress}
        rzdSearchProgressMessage={rzdSearchProgressMessage}
      />


      <RouteDetailsCard
        selectedRoute={selectedRoute}
        routesLoading={routesLoading}
        onClearRoute={onClearRoute}
        onSelectStation={onSelectStation}
      />
    </>
  );
}


function VirtualRoutesSidebar({
  selectedStation,
  virtualDestinationQuery,
  setVirtualDestinationQuery,
  virtualDestinationOptions,
  selectedVirtualDestination,
  onSearchVirtualDestination,
  onSelectVirtualDestination,
  onBuildVirtualRoute,
  virtualRouteLoading,
  virtualRouteError,
  virtualRouteMessage,
  topologyProgress,
  topologyProgressMessage,
  loadedRegionCodes,
}) {
  return (
    <>
      <section className="card">
        <h2>Виртуальные маршруты</h2>
        <p>
          Этот режим строит теоретический путь по OSM topology graph. Это не расписание РЖД.
        </p>
      </section>

      <section className="card">
        <h2>Точки маршрута</h2>

        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#475569', marginBottom: 6 }}>
            Точка А
          </div>

          {selectedStation ? (
            <div
              style={{
                background: '#f8fafc',
                border: '1px solid #e2e8f0',
                borderRadius: 12,
                padding: 10,
                fontSize: 14,
              }}
            >
              <strong>{selectedStation.name || 'Без названия'}</strong>
              <div style={{ marginTop: 4, color: '#64748b', fontSize: 13 }}>
                {selectedStation.region_code}
              </div>
            </div>
          ) : (
            <p style={{ margin: 0, color: '#64748b' }}>
              Выберите станцию отправления на карте.
            </p>
          )}
        </div>

        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: '#475569', marginBottom: 6 }}>
            Точка Б
          </div>

          <div className="search-row">
            <input
              value={virtualDestinationQuery}
              onChange={(event) => setVirtualDestinationQuery(event.target.value)}
              placeholder="Введите станцию назначения"
            />
            <button onClick={onSearchVirtualDestination}>Найти</button>
          </div>

          {virtualDestinationOptions.length > 0 && (
            <div
              style={{
                display: 'grid',
                gap: 6,
                marginTop: 10,
                maxHeight: 220,
                overflowY: 'auto',
                paddingRight: 4,
              }}
            >
              {virtualDestinationOptions.map((station) => {
                const isLoaded = loadedRegionCodes.includes(station.region_code);

                return (
                  <button
                    key={station.id}
                    className="station-list-item"
                    onClick={() => onSelectVirtualDestination(station)}
                  >
                    <div className="station-list-name">
                      {station.name || 'Без названия'}
                    </div>
                    <div className="station-list-meta">
                      {station.region || station.region_code || 'регион не указан'} •{' '}
                      {station.is_main_rail_station ? 'главная станция' : 'обычная станция'} •{' '}
                      {isLoaded ? 'округ загружен' : 'округ не загружен'}
                    </div>
                  </button>
                );
              })}
            </div>
          )}

          {selectedVirtualDestination && (
            <div
              style={{
                marginTop: 10,
                background: '#f8fafc',
                border: '1px solid #e2e8f0',
                borderRadius: 12,
                padding: 10,
                fontSize: 14,
              }}
            >
              <strong>{selectedVirtualDestination.name}</strong>
              <div style={{ marginTop: 4, color: '#64748b', fontSize: 13 }}>
                {selectedVirtualDestination.region_code}
              </div>
            </div>
          )}
        </div>

        <ActionButton
          variant="primary"
          fullWidth
          onClick={onBuildVirtualRoute}
          disabled={
            virtualRouteLoading ||
            !selectedStation?.id ||
            !selectedVirtualDestination?.id
          }
        >
          {virtualRouteLoading
            ? 'Строим виртуальный путь...'
            : 'Построить виртуальный путь по OSM'}
        </ActionButton>

        {topologyProgress > 0 && (
          <div
            style={{
              marginTop: 12,
              background: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: 12,
              padding: 10,
            }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                gap: 8,
                fontSize: 12,
                color: '#475569',
                marginBottom: 7,
              }}
            >
              <span>{topologyProgressMessage || 'Подготавливаем topology graph...'}</span>
              <strong>{Math.round(topologyProgress)}%</strong>
            </div>

            <div
              style={{
                width: '100%',
                height: 7,
                background: '#e5e7eb',
                borderRadius: 999,
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  width: `${Math.max(0, Math.min(100, topologyProgress))}%`,
                  height: '100%',
                  background: '#7c3aed',
                  transition: 'width 240ms ease',
                }}
              />
            </div>
          </div>
        )}

        {virtualRouteError && (
          <div
            style={{
              marginTop: 12,
              fontSize: 13,
              color: '#991b1b',
              background: '#fef2f2',
              border: '1px solid #fecaca',
              borderRadius: 12,
              padding: 10,
            }}
          >
            {virtualRouteError}
          </div>
        )}

        {virtualRouteMessage && !virtualRouteError && (
          <div
            style={{
              marginTop: 12,
              fontSize: 13,
              color: '#475569',
              background: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: 12,
              padding: 10,
            }}
          >
            {virtualRouteMessage}
          </div>
        )}
      </section>
    </>
  );
}

function LoadedSidebar({
  panelMode,
  setPanelMode,
  loading,
  error,
  stations,
  linesCount,
  searchQuery,
  setSearchQuery,
  onSearch,
  selectedStation,
  onSelectStation,
  loadedRegionCodes,
  showServiceLines,
  setShowServiceLines,
  onBackToSelection,
  isSearchMode,
  searchSections,
  routes,
  routesLoading,
  routesError,
  routeSearchQuery,
  setRouteSearchQuery,
  onSearchRoutes,
  onResetRoutes,
  selectedRoute,
  onSelectRoute,
  onClearRoute,
  stationRoutes,

  rzdOriginProfile,
  rzdOriginCode,
  setRzdOriginCode,
  rzdDestinationQuery,
  setRzdDestinationQuery,
  rzdDestinationOptions,
  selectedRzdDestination,
  onSearchRzdDestination,
  onSelectRzdDestination,
  rzdDepDate,
  setRzdDepDate,
  rzdIncludeTransfers,
  setRzdIncludeTransfers,
  rzdTrains,
  rzdSearchLoading,
  rzdImportLoading,
  rzdError,
  rzdMessage,
  rzdCalendarDebug,
  onSearchRzdRoutes,
  onImportRzdTrain,
  destinationNearbyRoutes,
  destinationNearbyRoutesLoading,
  rzdSearchProgress,
  rzdSearchProgressMessage,

  virtualDestinationQuery,
  setVirtualDestinationQuery,
  virtualDestinationOptions,
  selectedVirtualDestination,
  onSearchVirtualDestination,
  onSelectVirtualDestination,
  onBuildVirtualRoute,
  virtualRouteLoading,
  virtualRouteError,
  virtualRouteMessage,
  topologyProgress,
  topologyProgressMessage,
}) {
  return (
    <aside className="sidebar">
      <SidebarModeSwitch panelMode={panelMode} setPanelMode={setPanelMode} />

      {panelMode === 'infrastructure' ? (
        <InfrastructureSidebar
          loading={loading}
          error={error}
          stations={stations}
          linesCount={linesCount}
          searchQuery={searchQuery}
          setSearchQuery={setSearchQuery}
          onSearch={onSearch}
          selectedStation={selectedStation}
          onSelectStation={onSelectStation}
          loadedRegionCodes={loadedRegionCodes}
          showServiceLines={showServiceLines}
          setShowServiceLines={setShowServiceLines}
          onBackToSelection={onBackToSelection}
          isSearchMode={isSearchMode}
          searchSections={searchSections}
        />
      ) : panelMode === 'routes' ? (
        <RoutesSidebar
          routes={routes}
          routesLoading={routesLoading}
          routesError={routesError}
          routeSearchQuery={routeSearchQuery}
          setRouteSearchQuery={setRouteSearchQuery}
          onSearchRoutes={onSearchRoutes}
          onResetRoutes={onResetRoutes}
          selectedRoute={selectedRoute}
          onSelectRoute={onSelectRoute}
          onClearRoute={onClearRoute}
          onSelectStation={onSelectStation}
          selectedStation={selectedStation}
          stationRoutes={stationRoutes}
          rzdOriginProfile={rzdOriginProfile}
          rzdOriginCode={rzdOriginCode}
          setRzdOriginCode={setRzdOriginCode}
          rzdDestinationQuery={rzdDestinationQuery}
          setRzdDestinationQuery={setRzdDestinationQuery}
          rzdDestinationOptions={rzdDestinationOptions}
          selectedRzdDestination={selectedRzdDestination}
          onSearchRzdDestination={onSearchRzdDestination}
          onSelectRzdDestination={onSelectRzdDestination}
          rzdDepDate={rzdDepDate}
          setRzdDepDate={setRzdDepDate}
          rzdIncludeTransfers={rzdIncludeTransfers}
          setRzdIncludeTransfers={setRzdIncludeTransfers}
          rzdTrains={rzdTrains}
          rzdSearchLoading={rzdSearchLoading}
          rzdImportLoading={rzdImportLoading}
          rzdError={rzdError}
          rzdMessage={rzdMessage}
          rzdCalendarDebug={rzdCalendarDebug}
          onSearchRzdRoutes={onSearchRzdRoutes}
          onImportRzdTrain={onImportRzdTrain}
          onBuildVirtualRoute={onBuildVirtualRoute}
          virtualRouteLoading={virtualRouteLoading}
          virtualRouteError={virtualRouteError}
          virtualRouteMessage={virtualRouteMessage}
          destinationNearbyRoutes={destinationNearbyRoutes}
          destinationNearbyRoutesLoading={destinationNearbyRoutesLoading}
          rzdSearchProgress={rzdSearchProgress}
          rzdSearchProgressMessage={rzdSearchProgressMessage}
        />
      ) : (
        <VirtualRoutesSidebar
          selectedStation={selectedStation}
          virtualDestinationQuery={virtualDestinationQuery}
          setVirtualDestinationQuery={setVirtualDestinationQuery}
          virtualDestinationOptions={virtualDestinationOptions}
          selectedVirtualDestination={selectedVirtualDestination}
          onSearchVirtualDestination={onSearchVirtualDestination}
          onSelectVirtualDestination={onSelectVirtualDestination}
          onBuildVirtualRoute={onBuildVirtualRoute}
          virtualRouteLoading={virtualRouteLoading}
          virtualRouteError={virtualRouteError}
          virtualRouteMessage={virtualRouteMessage}
          topologyProgress={topologyProgress}
          topologyProgressMessage={topologyProgressMessage}
          loadedRegionCodes={loadedRegionCodes}
        />
      )}
    </aside>
  );
}

function createStationFeatures(stations, selectedStation) {
  const combinedStations = [...stations];

  if (
    selectedStation &&
    Number.isFinite(selectedStation.lon) &&
    Number.isFinite(selectedStation.lat) &&
    !combinedStations.some((item) => item.id === selectedStation.id)
  ) {
    combinedStations.push(selectedStation);
  }

  return combinedStations
    .filter((station) => Number.isFinite(station.lon) && Number.isFinite(station.lat))
    .map((station) => {
      const feature = new Feature({
        geometry: new Point(fromLonLat([station.lon, station.lat])),
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

function getStationVisualPriority(feature) {
  const isMain = Boolean(feature.get('is_main_rail_station'));
  const isVisibleDefault = feature.get('is_visible_default');
  const excludeReason = feature.get('exclude_reason');
  const stationType = String(feature.get('station_type') || '').toLowerCase();
  const hasRailwayCode = Boolean(feature.get('esr_user') || feature.get('uic_ref'));

  if (isMain) {
    return 'main';
  }

  if (isVisibleDefault === false || excludeReason) {
    return 'hidden_default';
  }

  if (
    stationType.includes('platform') ||
    stationType.includes('halt') ||
    stationType.includes('stop') ||
    stationType.includes('останов') ||
    stationType.includes('платформ')
  ) {
    return 'minor';
  }

  if (hasRailwayCode) {
    return 'coded';
  }

  return 'normal';
}

function createLineFeatures(lines) {
  const format = new GeoJSON();

  return lines.flatMap((line) => {
    try {
      const geometryObject = JSON.parse(line.geometry);

      const featureCollection = {
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
      };

      return format.readFeatures(featureCollection, {
        dataProjection: 'EPSG:4326',
        featureProjection: 'EPSG:3857',
      });
    } catch (error) {
      console.error('Ошибка разбора геометрии линии:', line, error);
      return [];
    }
  });
}

function createRouteGeometryFeature(geometry, geometrySource = null) {
  if (!geometry) {
    return null;
  }

  try {
    const format = new GeoJSON();
    return format.readFeature(
      {
        type: 'Feature',
        geometry,
        properties: {
          geometry_source: geometrySource,
        },
      },
      {
        dataProjection: 'EPSG:4326',
        featureProjection: 'EPSG:3857',
      }
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
      if (!segment.geometry) {
        return [];
      }

      const featureCollection = {
        type: 'FeatureCollection',
        features: [
          {
            type: 'Feature',
            geometry: segment.geometry,
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
      };

      const features = format.readFeatures(featureCollection, {
        dataProjection: 'EPSG:4326',
        featureProjection: 'EPSG:3857',
      });

      for (const feature of features) {
        feature.setId(`route-network-segment-${index}`);
      }

      return features;
    } catch (error) {
      console.error('Ошибка разбора route network segment:', segment, error);
      return [];
    }
  });
}

function createRouteStopFeatures(stops) {
  return (stops || [])
    .filter(
      (stop) =>
        Number.isFinite(stop.lon) &&
        Number.isFinite(stop.lat)
    )
    .map((stop) => {
      const feature = new Feature({
        geometry: new Point(fromLonLat([stop.lon, stop.lat])),
        stationId: stop.station_id || null,
        routeStopSequence: stop.stop_sequence,
        routeStopKind: stop.is_origin
          ? 'origin'
          : stop.is_destination
            ? 'destination'
            : 'intermediate',
      });

      feature.setId(`route-stop-${stop.stop_sequence}`);
      return feature;
    });
}

function buildDistrictLabelFeatures(federalDistrictsData) {
  if (!federalDistrictsData) {
    return [];
  }

  const format = new GeoJSON();
  const districtFeatures = format.readFeatures(federalDistrictsData, {
    dataProjection: 'EPSG:4326',
    featureProjection: 'EPSG:3857',
  });

  return districtFeatures.map((feature) => {
    const code = feature.get('code');
    const manualLonLat = REGION_LABEL_POINTS_LONLAT[code];

    const coordinate = manualLonLat
      ? fromLonLat(manualLonLat)
      : getCenter(feature.getGeometry().getExtent());

    return new Feature({
      geometry: new Point(coordinate),
      code,
      mapLabel: REGION_META_BY_CODE[code]?.mapLabel || feature.get('name') || code,
    });
  });
}

function buildCombinedExtentForRegions(federalDistrictsData, regionCodes) {
  if (!federalDistrictsData || regionCodes.length === 0) {
    return null;
  }

  const format = new GeoJSON();
  const districtFeatures = format.readFeatures(federalDistrictsData, {
    dataProjection: 'EPSG:4326',
    featureProjection: 'EPSG:3857',
  });

  const selectedFeatures = districtFeatures.filter((feature) =>
    regionCodes.includes(feature.get('code'))
  );

  if (selectedFeatures.length === 0) {
    return null;
  }

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
  if (!federalDistrictsData || !regionCode) {
    return null;
  }

  const format = new GeoJSON();
  const districtFeatures = format.readFeatures(federalDistrictsData, {
    dataProjection: 'EPSG:4326',
    featureProjection: 'EPSG:3857',
  });

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

  const districtSourceRef = useRef(null);
  const districtLabelSourceRef = useRef(null);
  const russiaBoundarySourceRef = useRef(null);
  const stationSourceRef = useRef(null);
  const lineSourceRef = useRef(null);
  const routeLineSourceRef = useRef(null);
  const routeNetworkSegmentsSourceRef = useRef(null);
  const routeStopsSourceRef = useRef(null);

  const hoverRegionCodeRef = useRef(null);
  const activeRegionCodeRef = useRef(activeRegionCode);
  const pendingRegionCodesRef = useRef(pendingRegionCodes);
  const loadedRegionCodesRef = useRef(loadedRegionCodes);
  const selectionModeRef = useRef(selectionMode);
  const selectedStationIdRef = useRef(selectedStation?.id ?? null);

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
    } else {
      if (isLoaded) {
        strokeColor = '#6b7280';
        strokeWidth = 1.8;
      } else {
        strokeColor = '#9ca3af';
        strokeWidth = 1.2;
      }
    }

    return new Style({
      stroke: new Stroke({
        color: strokeColor,
        width: strokeWidth,
      }),
      fill: new Fill({
        color: fillColor,
      }),
      zIndex: isHovered || isActive || isPending ? 12 : 8,
    });
  }, []);

  const districtLabelStyleFunction = useCallback((feature) => {
    if (!selectionModeRef.current) {
      return null;
    }

    const code = feature.get('code');
    const fontSize = code === 'far_eastern_fd' ? 12 : 13;

    return new Style({
      text: new Text({
        text: feature.get('mapLabel'),
        font: `600 ${fontSize}px Inter, Arial, sans-serif`,
        fill: new Fill({
          color: '#1f2937',
        }),
        backgroundFill: new Fill({
          color: 'rgba(255,255,255,0.78)',
        }),
        padding: [3, 5, 3, 5],
        overflow: true,
      }),
      zIndex: 30,
    });
  }, []);

  const routeStopsStyleFunction = useCallback((feature) => {
    const kind = feature.get('routeStopKind');

    let radius = 4.5;
    let fillColor = '#16a34a';

    if (kind === 'origin' || kind === 'destination') {
      radius = 6.5;
      fillColor = '#15803d';
    }

    return new Style({
      image: new CircleStyle({
        radius,
        fill: new Fill({ color: fillColor }),
        stroke: new Stroke({ color: '#ffffff', width: 1.4 }),
      }),
      zIndex: 90,
    });
  }, []);

  useEffect(() => {
    activeRegionCodeRef.current = activeRegionCode;
    districtLayerRef.current?.changed();
  }, [activeRegionCode]);

  useEffect(() => {
    pendingRegionCodesRef.current = pendingRegionCodes;
    districtLayerRef.current?.changed();
  }, [pendingRegionCodes]);

  useEffect(() => {
    loadedRegionCodesRef.current = loadedRegionCodes;
    districtLayerRef.current?.changed();
  }, [loadedRegionCodes]);

  useEffect(() => {
    selectionModeRef.current = selectionMode;
    districtLayerRef.current?.changed();
    districtLabelLayerRef.current?.setVisible(selectionMode);
    russiaBoundaryLayerRef.current?.changed();
  }, [selectionMode]);

  useEffect(() => {
    selectedStationIdRef.current = selectedStation?.id ?? null;
    stationLayerRef.current?.changed();
  }, [selectedStation]);

  useEffect(() => {
    if (!mapElementRef.current || mapRef.current) {
      return;
    }

    const baseLayer = new TileLayer({
      preload: 2,
      source: new OSM({
        crossOrigin: 'anonymous',
        transition: 0,
      }),
    });

    const districtSource = new VectorSource();
    const districtLabelSource = new VectorSource();
    const russiaBoundarySource = new VectorSource();
    const stationSource = new VectorSource();
    const lineSource = new VectorSource();
    const routeLineSource = new VectorSource();
    const routeNetworkSegmentsSource = new VectorSource();
    const routeStopsSource = new VectorSource();

    const districtLayer = new VectorLayer({
      source: districtSource,
      style: districtStyleFunction,
      declutter: false,
    });

    const districtLabelLayer = new VectorLayer({
      source: districtLabelSource,
      style: districtLabelStyleFunction,
      declutter: true,
    });

    const russiaBoundaryLayer = new VectorLayer({
      source: russiaBoundarySource,
      style: () =>
        new Style({
          stroke: new Stroke({
            color: selectionModeRef.current
              ? 'rgba(55, 65, 81, 0.65)'
              : 'rgba(55, 65, 81, 0.32)',
            width: selectionModeRef.current ? 2.2 : 1.4,
            lineDash: selectionModeRef.current ? undefined : [12, 8],
          }),
          zIndex: 10,
        }),
    });

    const lineLayer = new VectorImageLayer({
      source: lineSource,
      imageRatio: 1.3,
      style: (feature) => {
        const isService = Boolean(feature.get('is_service_line'));
        const isMainPassenger = Boolean(feature.get('is_main_passenger_line'));
        const isVisibleDefault = feature.get('is_visible_default');
        const excludeReason = feature.get('exclude_reason');

        if (isVisibleDefault === false || excludeReason) {
          return null;
        }

        if (isService) {
          return new Style({
            stroke: new Stroke({
              color: 'rgba(100, 116, 139, 0.45)',
              width: 1.2,
              lineDash: [6, 6],
            }),
            zIndex: 12,
          });
        }

        if (isMainPassenger) {
          return new Style({
            stroke: new Stroke({
              color: 'rgba(37, 99, 235, 0.72)',
              width: 2.4,
            }),
            zIndex: 18,
          });
        }

        return new Style({
          stroke: new Stroke({
            color: 'rgba(37, 99, 235, 0.45)',
            width: 1.6,
          }),
          zIndex: 14,
        });
      },
    });

    const routeLineLayer = new VectorLayer({
      source: routeLineSource,
      style: (feature) => {
        const geometrySource = feature.get('geometry_source');

        if (geometrySource === 'virtual_osm_path') {
          return [
            new Style({
              stroke: new Stroke({
                color: 'rgba(255,255,255,0.96)',
                width: 9,
              }),
              zIndex: 78,
            }),
            new Style({
              stroke: new Stroke({
                color: '#7c3aed',
                width: 4.8,
                lineDash: [10, 8],
              }),
              zIndex: 79,
            }),
          ];
        }

        if (geometrySource === 'fallback_station_chain') {
          return [
            new Style({
              stroke: new Stroke({
                color: 'rgba(255,255,255,0.95)',
                width: 10,
                lineDash: [14, 10],
              }),
              zIndex: 78,
            }),
            new Style({
              stroke: new Stroke({
                color: '#22c55e',
                width: 5,
                lineDash: [14, 10],
              }),
              zIndex: 79,
            }),
          ];
        }

        return [
          new Style({
            stroke: new Stroke({
              color: 'rgba(255,255,255,0.96)',
              width: 8,
            }),
            zIndex: 78,
          }),
          new Style({
            stroke: new Stroke({
              color: '#16a34a',
              width: 4.5,
            }),
            zIndex: 79,
          }),
        ];
      },
    });

    const routeNetworkSegmentsLayer = new VectorLayer({
      source: routeNetworkSegmentsSource,
      style: (feature) => {
        const segmentSource = feature.get('segment_source');
        const isVirtual = segmentSource === 'virtual_osm_path';

        return [
          new Style({
            stroke: new Stroke({
              color: 'rgba(255,255,255,0.98)',
              width: 11,
            }),
            zIndex: 84,
          }),
          new Style({
            stroke: new Stroke({
              color: isVirtual ? '#7c3aed' : '#16a34a',
              width: isVirtual ? 6 : 7,
              lineDash: isVirtual ? [10, 8] : undefined,
            }),
            zIndex: 85,
          }),
        ];
      },
    });

    const routeStopsLayer = new VectorLayer({
      source: routeStopsSource,
      style: routeStopsStyleFunction,
      declutter: false,
    });

    const stationLayer = new VectorImageLayer({
      source: stationSource,
      imageRatio: 1.3,
      style: (feature) => {
        const isSelected = feature.get('stationId') === selectedStationIdRef.current;

        return new Style({
          image: new CircleStyle({
            radius: isSelected ? 8 : 5,
            fill: new Fill({ color: isSelected ? '#16a34a' : '#dc2626' }),
            stroke: new Stroke({ color: '#ffffff', width: 1.5 }),
          }),
          zIndex: isSelected ? 36 : 30,
        });
      },
    });

    const view = new View({
      center: fromLonLat([90, 61]),
      zoom: 3.8,
      minZoom: 3.5,
      maxZoom: 16,
      extent: RUSSIA_EXTENT,
      smoothExtentConstraint: false,
      smoothResolutionConstraint: false,
      multiWorld: false,
      enableRotation: false,
    });

    const map = new Map({
      target: mapElementRef.current,
      layers: [
        baseLayer,
        districtLayer,
        russiaBoundaryLayer,
        districtLabelLayer,
        lineLayer,
        stationLayer,
        routeLineLayer,
        routeNetworkSegmentsLayer,
        routeStopsLayer,
      ],
      view,
      loadTilesWhileAnimating: true,
      loadTilesWhileInteracting: true,
    });

    map.on('pointermove', (event) => {
      if (!selectionModeRef.current) {
        if (hoverRegionCodeRef.current !== null) {
          hoverRegionCodeRef.current = null;
          districtLayer.changed();
        }
        return;
      }

      const districtFeature = map.forEachFeatureAtPixel(
        event.pixel,
        (foundFeature, layer) => {
          if (layer === districtLayerRef.current) {
            return foundFeature;
          }
          return null;
        },
        { hitTolerance: 4 }
      );

      const nextHoverCode = districtFeature?.get('code') || null;
      if (nextHoverCode !== hoverRegionCodeRef.current) {
        hoverRegionCodeRef.current = nextHoverCode;
        districtLayer.changed();
      }
    });

    map.on('singleclick', (event) => {
      if (selectionModeRef.current) {
        const districtFeature = map.forEachFeatureAtPixel(
          event.pixel,
          (foundFeature, layer) => {
            if (layer === districtLayerRef.current) {
              return foundFeature;
            }
            return null;
          },
          { hitTolerance: 4 }
        );

        if (districtFeature) {
          onActiveRegionChange(districtFeature.get('code'));
        }
        return;
      }

      const stationFeature = map.forEachFeatureAtPixel(
        event.pixel,
        (foundFeature) => {
          if (foundFeature.get('stationId')) {
            return foundFeature;
          }
          return null;
        },
        { hitTolerance: 4 }
      );

      if (stationFeature) {
        onSelectStation(stationFeature.get('stationId'));
      }
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

    districtSourceRef.current = districtSource;
    districtLabelSourceRef.current = districtLabelSource;
    russiaBoundarySourceRef.current = russiaBoundarySource;
    stationSourceRef.current = stationSource;
    lineSourceRef.current = lineSource;
    routeLineSourceRef.current = routeLineSource;
    routeNetworkSegmentsSourceRef.current = routeNetworkSegmentsSource;
    routeStopsSourceRef.current = routeStopsSource;

    setTimeout(() => {
      map.updateSize();
    }, 200);

    return () => {
      map.setTarget(undefined);
      mapRef.current = null;
    };
  }, [districtLabelStyleFunction, districtStyleFunction, onActiveRegionChange, onSelectStation, routeStopsStyleFunction]);

  useEffect(() => {
    if (!mapRef.current) {
      return;
    }

    const updateMapSize = () => {
      mapRef.current?.updateSize();
    };

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

  useEffect(() => {
    mapRef.current?.updateSize();
  }, [selectionMode, loading, loadedRegionCodes.length]);

  useEffect(() => {
    if (!federalDistrictsData || !districtSourceRef.current || !districtLabelSourceRef.current) {
      return;
    }

    const format = new GeoJSON();
    const districtFeatures = format.readFeatures(federalDistrictsData, {
      dataProjection: 'EPSG:4326',
      featureProjection: 'EPSG:3857',
    });

    districtSourceRef.current.clear(true);
    districtSourceRef.current.addFeatures(districtFeatures);

    const labelFeatures = buildDistrictLabelFeatures(federalDistrictsData);
    districtLabelSourceRef.current.clear(true);
    districtLabelSourceRef.current.addFeatures(labelFeatures);
  }, [federalDistrictsData]);

  useEffect(() => {
    if (!russiaFeatureData || !russiaBoundarySourceRef.current) {
      return;
    }

    const format = new GeoJSON();
    const feature = format.readFeature(russiaFeatureData, {
      dataProjection: 'EPSG:4326',
      featureProjection: 'EPSG:3857',
    });

    russiaBoundarySourceRef.current.clear(true);
    russiaBoundarySourceRef.current.addFeature(feature);
  }, [russiaFeatureData]);

  useEffect(() => {
    if (!stationSourceRef.current) {
      return;
    }

    const features = createStationFeatures(stations, selectedStation);
    stationSourceRef.current.clear(true);
    if (features.length > 0) {
      stationSourceRef.current.addFeatures(features);
    }
  }, [stations, selectedStation]);

  useEffect(() => {
    if (!lineSourceRef.current) {
      return;
    }

    const features = createLineFeatures(lines);
    lineSourceRef.current.clear(true);
    if (features.length > 0) {
      lineSourceRef.current.addFeatures(features);
    }
  }, [lines]);

  useEffect(() => {
    if (
      !routeLineSourceRef.current ||
      !routeNetworkSegmentsSourceRef.current ||
      !routeStopsSourceRef.current
    ) {
      return;
    }

    routeLineSourceRef.current.clear(true);
    routeNetworkSegmentsSourceRef.current.clear(true);
    routeStopsSourceRef.current.clear(true);

    if (!selectedRoute) {
      return;
    }

    routeDebug('Map route render', {
      route_id: selectedRoute?.id,
      route_name: selectedRoute?.route_name,
      geometry_source: selectedRoute?.geometry_source,
      has_geometry: Boolean(selectedRoute?.geometry),
      network_segments_count: selectedRoute?.network_segments?.length || 0,
      stops_count: selectedRoute?.stops?.length || 0,
    });

    if (selectedRoute.geometry) {
      const routeFeature = createRouteGeometryFeature(
        selectedRoute.geometry,
        selectedRoute.geometry_source || null
      );
      if (routeFeature) {
        routeLineSourceRef.current.addFeature(routeFeature);
      }
    }

    const networkSegmentFeatures = createRouteNetworkSegmentFeatures(
      selectedRoute.network_segments || []
    );
    if (networkSegmentFeatures.length > 0) {
      routeNetworkSegmentsSourceRef.current.addFeatures(networkSegmentFeatures);
    }

    const stopFeatures = createRouteStopFeatures(selectedRoute.stops);
    if (stopFeatures.length > 0) {
      routeStopsSourceRef.current.addFeatures(stopFeatures);
    }
  }, [selectedRoute]);

  useEffect(() => {
    if (!mapRef.current || !federalDistrictsData) {
      return;
    }

    if (selectionMode && activeRegionCode) {
      const districtFeature = getDistrictFeatureByCode(federalDistrictsData, activeRegionCode);
      const geometry = districtFeature?.getGeometry();

      if (geometry) {
        const focusConfig = REGION_FOCUS_CONFIG[activeRegionCode] || {};
        const center = focusConfig.centerLonLat
          ? fromLonLat(focusConfig.centerLonLat)
          : getCenter(geometry.getExtent());
        const zoom = focusConfig.zoom ?? 5.3;

        mapRef.current.getView().animate({
          center,
          zoom,
          duration: 320,
        });
      }
      return;
    }

    if (!selectionMode && loadedRegionCodes.length > 0) {
      const combinedExtent = buildCombinedExtentForRegions(federalDistrictsData, loadedRegionCodes);

      if (combinedExtent) {
        mapRef.current.getView().fit(combinedExtent, {
          padding: [60, 60, 60, 60],
          duration: 320,
          maxZoom: 7,
        });
      }
    }
  }, [selectionMode, activeRegionCode, loadedRegionCodes, federalDistrictsData]);

  useEffect(() => {
    if (!mapRef.current || !selectedStation) {
      return;
    }

    if (!Number.isFinite(selectedStation.lon) || !Number.isFinite(selectedStation.lat)) {
      return;
    }

    mapRef.current.getView().animate({
      center: fromLonLat([selectedStation.lon, selectedStation.lat]),
      zoom: Math.max(mapRef.current.getView().getZoom() ?? 6, 8),
      duration: 320,
    });
  }, [selectedStation]);

  useEffect(() => {
    if (!mapRef.current || !selectedRoute) {
      return;
    }

    const routeNetworkSegmentsSource = routeNetworkSegmentsSourceRef.current;
    const routeLineSource = routeLineSourceRef.current;
    const routeStopsSource = routeStopsSourceRef.current;

    const segmentFeatures = routeNetworkSegmentsSource?.getFeatures() || [];
    const lineFeatures = routeLineSource?.getFeatures() || [];
    const stopFeatures = routeStopsSource?.getFeatures() || [];

    if (segmentFeatures.length > 0) {
      const extent = routeNetworkSegmentsSource.getExtent();
      if (extent && Number.isFinite(extent[0])) {
        mapRef.current.getView().fit(extent, {
          padding: [70, 70, 70, 70],
          duration: 420,
          maxZoom: 8,
        });
        return;
      }
    }

    if (lineFeatures.length > 0) {
      const extent = routeLineSource.getExtent();
      if (extent && Number.isFinite(extent[0])) {
        mapRef.current.getView().fit(extent, {
          padding: [70, 70, 70, 70],
          duration: 420,
          maxZoom: 8,
        });
        return;
      }
    }

    if (stopFeatures.length > 0) {
      const extent = routeStopsSource.getExtent();
      if (extent && Number.isFinite(extent[0])) {
        mapRef.current.getView().fit(extent, {
          padding: [70, 70, 70, 70],
          duration: 420,
          maxZoom: 8,
        });
      }
    }
  }, [selectedRoute]);

  return (
    <section
      className="map-section"
      style={{
        position: 'relative',
        flex: 1,
        minWidth: 0,
        minHeight: 0,
        height: '100%',
        display: 'flex',
      }}
    >
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
  const [searchSections, setSearchSections] = useState({
    selected: [],
    other: [],
  });

  const [routes, setRoutes] = useState([]);
  const [routesLoading, setRoutesLoading] = useState(false);
  const [routesError, setRoutesError] = useState('');
  const [routeSearchQuery, setRouteSearchQuery] = useState('');
  const [selectedRoute, setSelectedRoute] = useState(null);
  const [stationRoutes, setStationRoutes] = useState([]);
  const [sidebarMode, setSidebarMode] = useState('infrastructure');

  const [rzdOriginProfile, setRzdOriginProfile] = useState(null);
  const [rzdOriginCode, setRzdOriginCode] = useState('');

  const [rzdDestinationQuery, setRzdDestinationQuery] = useState('');
  const [rzdDestinationOptions, setRzdDestinationOptions] = useState([]);
  const [selectedRzdDestination, setSelectedRzdDestination] = useState(null);

  const [rzdDepDate, setRzdDepDate] = useState(getDefaultRzdDate);

  const [rzdIncludeTransfers, setRzdIncludeTransfers] = useState(false);
  const [rzdTrains, setRzdTrains] = useState([]);
  const [rzdSearchLoading, setRzdSearchLoading] = useState(false);
  const [rzdImportLoading, setRzdImportLoading] = useState(false);
  const [rzdError, setRzdError] = useState('');
  const [rzdMessage, setRzdMessage] = useState('');
  const [rzdCalendarDebug, setRzdCalendarDebug] = useState(null);
  const [rzdSearchProgress, setRzdSearchProgress] = useState(0);
  const [rzdSearchProgressMessage, setRzdSearchProgressMessage] = useState('');

  const [destinationNearbyRoutes, setDestinationNearbyRoutes] = useState([]);
  const [destinationNearbyRoutesLoading, setDestinationNearbyRoutesLoading] = useState(false);

  const [virtualDestinationQuery, setVirtualDestinationQuery] = useState('');
  const [virtualDestinationOptions, setVirtualDestinationOptions] = useState([]);
  const [selectedVirtualDestination, setSelectedVirtualDestination] = useState(null);

  const [virtualRouteLoading, setVirtualRouteLoading] = useState(false);
  const [virtualRouteError, setVirtualRouteError] = useState('');
  const [virtualRouteMessage, setVirtualRouteMessage] = useState('');

  const [topologyProgress, setTopologyProgress] = useState(0);
  const [topologyProgressMessage, setTopologyProgressMessage] = useState('');

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
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedStation, setSelectedStation] = useState(null);
  const [showServiceLines, setShowServiceLines] = useState(false);
  const [isSearchMode, setIsSearchMode] = useState(false);

  const hasLoadedData = loadedRegionCodes.length > 0;
  const selectionMode = !hasLoadedData;
  const linesCount = useMemo(() => lines.length, [lines]);

  const initialLoadCompletedRef = useRef(false);
  const routeLoadingTimerRef = useRef(null);
  const rzdSearchProgressTimerRef = useRef(null);

  const loadRegionSummaries = useCallback(async () => {
    try {
      const response = await fetch(`${BACKEND_URL}/api/regions/summary`);
      if (!response.ok) {
        throw new Error('Не удалось загрузить сводку по округам');
      }

      const data = await response.json();
      const summariesByCode = Object.fromEntries(
        (data.items || []).map((item) => [item.code, item])
      );
      setRegionSummaries(summariesByCode);
    } catch (err) {
      console.error('Ошибка загрузки сводки по округам:', err);
    }
  }, []);

  const loadRoutes = useCallback(async (query = '') => {
    setRoutesLoading(true);
    setRoutesError('');

    try {
      const params = new URLSearchParams();
      params.set('limit', '300');
      params.set('active_only', 'true');

      const trimmed = query.trim();
      if (trimmed) {
        params.set('q', trimmed);
      }

      const response = await fetch(`${BACKEND_URL}/api/routes?${params.toString()}`);

      if (!response.ok) {
        throw new Error('Не удалось загрузить маршруты');
      }

      const data = await response.json();
      setRoutes(data.items || []);
    } catch (err) {
      console.error(err);
      setRoutesError(err instanceof Error ? err.message : 'Ошибка загрузки маршрутов');
    } finally {
      setRoutesLoading(false);
    }
  }, []);

  const startRouteLoadingOverlay = useCallback(() => {
    if (routeLoadingTimerRef.current) {
      window.clearInterval(routeLoadingTimerRef.current);
    }

    setRouteLoadingVisible(true);
    setRouteLoadingProgress(8);
    setRouteLoadingMessage('Запрашиваем маршрут у сервера...');

    let currentProgress = 8;

    routeLoadingTimerRef.current = window.setInterval(() => {
      currentProgress = Math.min(currentProgress + 4, 92);

      if (currentProgress < 25) {
        setRouteLoadingMessage('Запрашиваем маршрут у сервера...');
      } else if (currentProgress < 45) {
        setRouteLoadingMessage('Получаем список остановок...');
      } else if (currentProgress < 65) {
        setRouteLoadingMessage('Подбираем кандидаты станций...');
      } else if (currentProgress < 82) {
        setRouteLoadingMessage('Проверяем достижимость по графу...');
      } else {
        setRouteLoadingMessage('Подготавливаем отображение маршрута на карте...');
      }

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
    if (rzdSearchProgressTimerRef.current) {
      window.clearInterval(rzdSearchProgressTimerRef.current);
    }

    setRzdSearchProgress(8);
    setRzdSearchProgressMessage('Подбираем станции и коды РЖД...');

    let progress = 8;
    const startedAt = Date.now();

    rzdSearchProgressTimerRef.current = window.setInterval(() => {
      progress = Math.min(progress + 3, 92);

      const elapsedSeconds = Math.round((Date.now() - startedAt) / 1000);

      if (elapsedSeconds > 30) {
        setRzdSearchProgressMessage('РЖД API отвечает медленно. Продолжаем ждать результат...');
      } else if (elapsedSeconds > 15) {
        setRzdSearchProgressMessage('Поиск занимает дольше обычного. Проверяем дополнительные пары станций...');
      } else if (progress < 25) {
        setRzdSearchProgressMessage('Подбираем ближайшие станции...');
      } else if (progress < 45) {
        setRzdSearchProgressMessage('Проверяем коды РЖД...');
      } else if (progress < 70) {
        setRzdSearchProgressMessage('Проверяем даты отправления...');
      } else {
        setRzdSearchProgressMessage('Собираем найденные варианты...');
      }

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

  useEffect(() => {
    return () => {
      if (routeLoadingTimerRef.current) {
        window.clearInterval(routeLoadingTimerRef.current);
        routeLoadingTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    return () => {
      if (rzdSearchProgressTimerRef.current) {
        window.clearInterval(rzdSearchProgressTimerRef.current);
        rzdSearchProgressTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    async function loadFederalDistricts() {
      try {
        const response = await fetch('/federal-districts.geojson');
        if (!response.ok) {
          throw new Error('Не удалось загрузить federal-districts.geojson');
        }

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
        if (!response.ok) {
          throw new Error('Не удалось загрузить countries.geojson');
        }

        const data = await response.json();

        const russiaFeature =
          data.features?.find(
            (feature) =>
              feature?.properties?.['ISO3166-1-Alpha-3'] === 'RUS' ||
              feature?.properties?.ISO_A3 === 'RUS' ||
              feature?.properties?.name === 'Russia'
          ) || null;

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
    const runningRegionCodes = Object.entries(updateStates)
      .filter(([, item]) => item.status === 'running' || item.status === 'starting')
      .map(([code]) => code);

    if (runningRegionCodes.length === 0) {
      return undefined;
    }

    const timer = setInterval(async () => {
      for (const regionCode of runningRegionCodes) {
        try {
          const response = await fetch(
            `${BACKEND_URL}/api/dataset-runs/latest?region_code=${encodeURIComponent(regionCode)}`
          );

          if (!response.ok) {
            continue;
          }

          const data = await response.json();
          const latest = data.item;

          if (!latest) {
            continue;
          }

          if (latest.status === 'running') {
            setUpdateStates((prev) => ({
              ...prev,
              [regionCode]: {
                ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
                status: 'running',
                message: 'Обновление выполняется...',
                notes: latest.notes || '',
              },
            }));
            continue;
          }

          if (latest.status === 'finished') {
            setUpdateStates((prev) => ({
              ...prev,
              [regionCode]: {
                ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
                status: 'finished',
                message: 'Обновление завершено. Чтобы увидеть новые данные на карте, заново выбери округа.',
                notes: latest.notes || '',
                can_update: false,
              },
            }));
            await loadRegionSummaries();
            continue;
          }

          if (latest.status === 'failed') {
            setUpdateStates((prev) => ({
              ...prev,
              [regionCode]: {
                ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
                status: 'failed',
                message: 'Во время обновления произошла ошибка',
                notes: latest.notes || '',
                can_update: false,
              },
            }));
            continue;
          }

          if (latest.status === 'skipped') {
            setUpdateStates((prev) => ({
              ...prev,
              [regionCode]: {
                ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
                status: 'up_to_date',
                message: 'Обновление не требуется',
                notes: latest.notes || '',
                can_update: false,
              },
            }));
          }
        } catch (err) {
          console.error('Ошибка polling статуса обновления:', regionCode, err);
        }
      }
    }, 4000);

    return () => clearInterval(timer);
  }, [updateStates, loadRegionSummaries]);

  const togglePendingRegion = useCallback((regionCode) => {
    if (!regionCode) {
      return;
    }

    setPendingRegionCodes((prev) => {
      if (prev.includes(regionCode)) {
        return prev.filter((code) => code !== regionCode);
      }
      return [...prev, regionCode];
    });
  }, []);

  const loadAllSelectedData = useCallback(
    async ({ regionCodes, includeServiceLines }) => {
      if (!regionCodes.length) {
        return;
      }

      const regionCodesParam = regionCodes.join(',');

      setLoading(true);
      setLoadingProgress(8);
      setLoadingMessage('Подготавливаем загрузку выбранных округов...');
      setError('');

      try {
        const stationsUrl =
          `${BACKEND_URL}/api/stations` +
          `?region_codes=${encodeURIComponent(regionCodesParam)}` +
          `&limit=100000`;

        const linesUrl =
          `${BACKEND_URL}/api/lines` +
          `?region_codes=${encodeURIComponent(regionCodesParam)}` +
          `&limit=400000` +
          `&include_service=${includeServiceLines ? 'true' : 'false'}`;

        setLoadingProgress(18);
        setLoadingMessage('Загружаем станции выбранных округов...');
        const stationsResponse = await fetch(stationsUrl);

        if (!stationsResponse.ok) {
          throw new Error('Не удалось загрузить станции');
        }

        const stationsData = await stationsResponse.json();

        setLoadingProgress(52);
        setLoadingMessage('Загружаем линии выбранных округов...');
        const linesResponse = await fetch(linesUrl);

        if (!linesResponse.ok) {
          throw new Error('Не удалось загрузить линии');
        }

        const linesData = await linesResponse.json();

        setLoadingProgress(86);
        setLoadingMessage('Подготавливаем данные для отображения...');

        const stationsItems = stationsData.items || [];
        const linesItems = linesData.items || [];

        setAllStations(stationsItems);
        setStations(stationsItems);
        setLines(linesItems);
        setSearchSections({
          selected: stationsItems,
          other: [],
        });
        setSelectedStation(null);
        setStationRoutes([]);
        setSearchQuery('');
        setIsSearchMode(false);
        setLoadedRegionCodes(regionCodes);
        setSidebarMode('infrastructure');

        initialLoadCompletedRef.current = true;

        setLoadingProgress(100);
        setLoadingMessage('Готово');

        setTimeout(() => {
          setLoading(false);
          setLoadingProgress(0);
          setLoadingMessage('');
        }, 180);
      } catch (err) {
        console.error(err);
        setLoading(false);
        setLoadingProgress(0);
        setLoadingMessage('');
        setError(err instanceof Error ? err.message : 'Ошибка загрузки данных');
      }
    },
    []
  );

  const handleLoadSelected = useCallback(async () => {
    if (pendingRegionCodes.length === 0) {
      return;
    }

    await loadAllSelectedData({
      regionCodes: pendingRegionCodes,
      includeServiceLines: showServiceLines,
    });
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
    setSearchSections({
      selected: [],
      other: [],
    });
    setSelectedStation(null);
    setSearchQuery('');
    setRouteSearchQuery('');
    setIsSearchMode(false);
    setSidebarMode('infrastructure');
    setError('');
    initialLoadCompletedRef.current = false;
  }, [loadedRegionCodes]);

  useEffect(() => {
    if (!initialLoadCompletedRef.current || !loadedRegionCodes.length) {
      return;
    }

    async function reloadLinesOnly() {
      try {
        setLoading(true);
        setLoadingProgress(18);
        setLoadingMessage('Обновляем линии для загруженных округов...');
        setError('');

        const regionCodesParam = loadedRegionCodes.join(',');
        const linesUrl =
          `${BACKEND_URL}/api/lines` +
          `?region_codes=${encodeURIComponent(regionCodesParam)}` +
          `&limit=400000` +
          `&include_service=${showServiceLines ? 'true' : 'false'}`;

        const response = await fetch(linesUrl);

        if (!response.ok) {
          throw new Error('Не удалось загрузить линии');
        }

        const data = await response.json();

        setLoadingProgress(85);
        setLoadingMessage('Применяем изменения...');

        setLines(data.items || []);

        setLoadingProgress(100);
        setLoadingMessage('Готово');

        setTimeout(() => {
          setLoading(false);
          setLoadingProgress(0);
          setLoadingMessage('');
        }, 160);
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

  const handleSearch = useCallback(async () => {
    const trimmed = searchQuery.trim();

    if (!trimmed) {
      setStations(allStations);
      setSearchSections({
        selected: allStations,
        other: [],
      });
      setIsSearchMode(false);
      return;
    }

    try {
      setLoading(true);
      setError('');

      const response = await fetch(
        `${BACKEND_URL}/api/search/stations?q=${encodeURIComponent(trimmed)}&limit=2000`
      );

      if (!response.ok) {
        throw new Error('Не удалось выполнить поиск');
      }

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
  }, [searchQuery, allStations, loadedRegionCodes]);

  const handleSelectStation = useCallback(async (stationId) => {
    try {
      setError('');

      const stationResponse = await fetch(
        `${BACKEND_URL}/api/stations/${stationId}?include_hidden=true`
      );

      if (!stationResponse.ok) {
        throw new Error('Не удалось загрузить станцию');
      }

      const stationData = await stationResponse.json();

      setSelectedStation(stationData);
    } catch (err) {
      console.error(err);
      setError(err instanceof Error ? err.message : 'Ошибка загрузки станции');
    }
  }, []);

  useEffect(() => {
    if (!selectedStation?.id) {
      setRzdOriginProfile(null);
      setRzdOriginCode('');
      return;
    }

    let cancelled = false;

    async function loadRzdOriginProfile() {
      try {
        setRzdError('');

        const response = await fetch(
          `${BACKEND_URL}/api/rzd/stations/${selectedStation.id}/profile`
        );

        if (!response.ok) {
          throw new Error('Не удалось загрузить РЖД-профиль станции');
        }

        const data = await response.json();
        const item = data.item || null;

        if (cancelled) {
          return;
        }

        setRzdOriginProfile(item);

        const preferredCode = pickPreferredRzdCode(item?.code_candidates || []);
        setRzdOriginCode(preferredCode?.code || '');
      } catch (err) {
        console.error(err);

        if (!cancelled) {
          setRzdOriginProfile(null);
          setRzdOriginCode('');
          setRzdError(err instanceof Error ? err.message : 'Ошибка загрузки РЖД-профиля');
        }
      }
    }

    loadRzdOriginProfile();

    return () => {
      cancelled = true;
    };
  }, [selectedStation]);

  const handleSearchRoutes = useCallback(async () => {
    await loadRoutes(routeSearchQuery);
  }, [loadRoutes, routeSearchQuery]);

  const handleResetRoutes = useCallback(async () => {
    setRouteSearchQuery('');
    await loadRoutes('');
  }, [loadRoutes]);

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

      if (!response.ok) {
        throw new Error('Не удалось загрузить маршрут');
      }

      const data = await response.json();

      routeDebug('Route select response', {
        routeId,
        geometry_source: data.geometry_source || data.item?.geometry_source,
        geometry_ready: Boolean(data.geometry || data.item?.geometry),
        network_segments_count: (data.network_segments || data.item?.network_segments || []).length,
        summary: data.summary,
        diagnostics: data.diagnostics || data.item?.diagnostics,
      });

      const normalizedRoute = {
        ...(data.item || {}),
        stops: data.stops || [],
        geometry: data.geometry || null,
        geometry_source: data.geometry_source || data.item?.geometry_source || null,
        network_segments: data.network_segments || data.item?.network_segments || [],
        diagnostics: data.diagnostics || data.item?.diagnostics || null,
      };

      setSelectedRoute(normalizedRoute);
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
  }, []);

  const handleSearchRzdDestination = useCallback(async () => {
    const query = rzdDestinationQuery.trim();

    if (query.length < 2) {
      setRzdDestinationOptions([]);
      setRzdMessage('Введите минимум 2 символа для поиска станции назначения.');
      return;
    }

    try {
      setRzdError('');
      setRzdMessage('');
      setRzdDestinationOptions([]);
      setSelectedRzdDestination(null);
      setRzdTrains([]);

      const response = await fetch(
        `${BACKEND_URL}/api/search/stations?q=${encodeURIComponent(query)}&limit=50`
      );

      if (!response.ok) {
        throw new Error('Не удалось найти станции в OSM-базе');
      }

      const data = await response.json();
      const stations = data.items || [];

      const options = stations
        .map((station) => ({
          id: station.id,
          name: station.name,
          region_code: station.region_code,
          station_type: station.station_type,
          region: station.region,
          is_main_rail_station: Boolean(station.is_main_rail_station),
          lon: station.lon,
          lat: station.lat,
          raw_station: station,
        }))
        .sort((a, b) => {
          if (a.is_main_rail_station !== b.is_main_rail_station) {
            return Number(b.is_main_rail_station) - Number(a.is_main_rail_station);
          }

          return String(a.name || '').localeCompare(String(b.name || ''), 'ru');
        });

      setRzdDestinationOptions(options);

      if (options.length === 0) {
        setRzdMessage('Станции назначения не найдены в OSM-базе.');
      } else {
        setRzdMessage(`Найдено станций: ${options.length}`);
      }
    } catch (err) {
      console.error(err);
      setRzdError(err instanceof Error ? err.message : 'Ошибка поиска станции назначения');
    }
  }, [rzdDestinationQuery]);

  const loadNearbyRoutesForStation = useCallback(async (stationId) => {
    if (!stationId) {
      return [];
    }

    const response = await fetch(
      `${BACKEND_URL}/api/stations/${stationId}/nearby-routes?radius_km=5&limit=40`
    );

    if (!response.ok) {
      throw new Error('Не удалось загрузить маршруты из зоны станции');
    }

    const data = await response.json();
    return data.items || [];
  }, []);

  const handleSelectRzdDestination = useCallback(async (option) => {
    setSelectedRzdDestination(option);
    setRzdDestinationQuery(option.name || '');
    setRzdDestinationOptions([]);
    setRzdTrains([]);
    setRzdError('');
    setRzdMessage(`Выбрана станция назначения: ${option.name}`);
    setDestinationNearbyRoutes([]);

    try {
      setDestinationNearbyRoutesLoading(true);
      const nearbyRoutes = await loadNearbyRoutesForStation(option.id);
      setDestinationNearbyRoutes(nearbyRoutes);
    } catch (err) {
      console.error(err);
    } finally {
      setDestinationNearbyRoutesLoading(false);
    }
  }, [loadNearbyRoutesForStation]);

  const handleSearchRzdRoutes = useCallback(async () => {
    if (!selectedStation?.id) {
      setRzdError('Выберите станцию отправления на карте.');
      return;
    }

    if (!selectedRzdDestination?.id) {
      setRzdError('Выберите станцию назначения из списка.');
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
        origin_station_id: selectedStation.id,
        destination_station_id: selectedRzdDestination.id,
        days_ahead: RZD_SEARCH_DAYS_AHEAD,
        check_seats: false,
        nearby_radius_km: 5,
        nearby_station_limit: 5,
        max_code_pair_attempts: 10,
      };

      routeDebug('RZD A-B search request', requestPayload);

      const response = await fetch(`${BACKEND_URL}/api/rzd/routes/search-calendar-by-stations`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestPayload),
      });

      if (!response.ok) {
        throw new Error(
          await readApiError(response, 'Не удалось найти поезда через РЖД API')
        );
      }

      const data = await response.json();
      routeDebug('RZD A-B search response', {
        status: data.status,
        total: data.total,
        message: data.message,
        exact_found: data.exact_found,
        similar_used: data.similar_used,
        code_attempts_count: data.code_attempts?.length,
        dates_checked: data.dates_checked,
        dates_with_trains: data.dates_with_trains,
        items_preview: (data.items || []).slice(0, 5),
      });
      setRzdCalendarDebug(data);

      const items = data.items || [];
      setRzdTrains(items);

      if (items.length === 0) {
        const attemptsCount = (data.code_attempts || []).length;
        const checkedCount = data.dates_checked ?? 0;

        setRzdMessage(
          data.message ||
            `За ближайшие ${checkedCount || RZD_SEARCH_DAYS_AHEAD} дней поездов не найдено. Проверено пар кодов: ${attemptsCount}.`
        );
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
  }, [
    selectedStation,
    selectedRzdDestination,
    startRzdSearchProgress,
    finishRzdSearchProgress,
  ]);

  const handleSearchVirtualDestination = useCallback(async () => {
    const query = virtualDestinationQuery.trim();

    if (query.length < 2) {
      setVirtualDestinationOptions([]);
      setVirtualRouteMessage('Введите минимум 2 символа для поиска станции назначения.');
      return;
    }

    try {
      setVirtualRouteError('');
      setVirtualRouteMessage('');
      setVirtualDestinationOptions([]);

      const response = await fetch(
        `${BACKEND_URL}/api/search/stations?q=${encodeURIComponent(query)}&limit=50`
      );

      if (!response.ok) {
        throw new Error('Не удалось найти станции');
      }

      const data = await response.json();
      const items = data.items || [];

      const sorted = [...items].sort((a, b) => {
        const aLoaded = loadedRegionCodes.includes(a.region_code) ? 1 : 0;
        const bLoaded = loadedRegionCodes.includes(b.region_code) ? 1 : 0;

        if (aLoaded !== bLoaded) {
          return bLoaded - aLoaded;
        }

        if (Boolean(a.is_main_rail_station) !== Boolean(b.is_main_rail_station)) {
          return Number(Boolean(b.is_main_rail_station)) - Number(Boolean(a.is_main_rail_station));
        }

        return String(a.name || '').localeCompare(String(b.name || ''), 'ru');
      });

      setVirtualDestinationOptions(sorted);

      if (sorted.length === 0) {
        setVirtualRouteMessage('Станции назначения не найдены.');
      }
    } catch (err) {
      console.error(err);
      setVirtualRouteError(err instanceof Error ? err.message : 'Ошибка поиска станции');
    }
  }, [virtualDestinationQuery, loadedRegionCodes]);

  const handleSelectVirtualDestination = useCallback((station) => {
    setSelectedVirtualDestination(station);
    setVirtualDestinationQuery(station.name || '');
    setVirtualDestinationOptions([]);
    setVirtualRouteError('');

    if (!loadedRegionCodes.includes(station.region_code)) {
      setVirtualRouteMessage(
        'Станция находится в округе, который сейчас не загружен. Загрузите этот округ, чтобы построить виртуальный путь.'
      );
    } else {
      setVirtualRouteMessage(`Выбрана станция назначения: ${station.name}`);
    }
  }, [loadedRegionCodes]);

  const ensureTopologyForRegions = useCallback(async (regionCodes) => {
    if (!regionCodes.length) {
      throw new Error('Не удалось определить округа для topology graph.');
    }

    const regionCodesParam = regionCodes.join(',');

    setTopologyProgress(8);
    setTopologyProgressMessage('Проверяем topology graph...');

    const statusResponse = await fetch(
      `${BACKEND_URL}/api/topology/status?region_codes=${encodeURIComponent(regionCodesParam)}`
    );

    if (!statusResponse.ok) {
      throw new Error('Не удалось проверить topology graph');
    }

    const statusData = await statusResponse.json();
    const statusItem = statusData.item;

    if (statusItem?.is_built) {
      setTopologyProgress(100);
      setTopologyProgressMessage('Topology graph готов');

      window.setTimeout(() => {
        setTopologyProgress(0);
        setTopologyProgressMessage('');
      }, 600);

      return statusItem;
    }

    setTopologyProgress(18);
    setTopologyProgressMessage('Topology graph не найден. Запускаем построение...');

    const buildResponse = await fetch(`${BACKEND_URL}/api/topology/build`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        region_codes: regionCodes,
        force_rebuild: false,
      }),
    });

    if (!buildResponse.ok) {
      throw new Error(
        await readApiError(buildResponse, 'Не удалось запустить построение topology graph')
      );
    }

    const buildData = await buildResponse.json();
    const jobId = buildData.job_id;

    if (!jobId) {
      throw new Error('Backend не вернул job_id для построения topology graph');
    }

    const startedAt = Date.now();

    return await new Promise((resolve, reject) => {
      const timer = window.setInterval(async () => {
        try {
          const jobResponse = await fetch(`${BACKEND_URL}/api/topology/jobs/${jobId}`);

          if (!jobResponse.ok) {
            throw new Error('Не удалось получить статус построения topology graph');
          }

          const jobData = await jobResponse.json();
          const elapsedSeconds = Math.round((Date.now() - startedAt) / 1000);

          setTopologyProgress(Math.max(5, Math.min(100, jobData.progress_percent || 0)));

          let message = jobData.stage_label || 'Строим topology graph...';

          if (elapsedSeconds > 60 && jobData.status === 'running') {
            message = `${message} Это занимает дольше обычного.`;
          }

          if (elapsedSeconds > 180 && jobData.status === 'running') {
            message = `${message} Обрабатывается крупный граф, можно подождать или попробовать меньший набор округов.`;
          }

          setTopologyProgressMessage(message);

          routeDebug('Topology job polling', {
            job_id: jobId,
            elapsed_seconds: elapsedSeconds,
            status: jobData.status,
            progress_percent: jobData.progress_percent,
            stage_code: jobData.stage_code,
            stage_label: jobData.stage_label,
          });

          if (jobData.status === 'done') {
            window.clearInterval(timer);
            setTopologyProgress(100);
            setTopologyProgressMessage('Topology graph построен');

            window.setTimeout(() => {
              setTopologyProgress(0);
              setTopologyProgressMessage('');
            }, 700);

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
    if (!selectedStation?.id) {
      setVirtualRouteError('Выберите станцию отправления на карте.');
      return;
    }

    if (!selectedVirtualDestination?.id) {
      setVirtualRouteError('Выберите станцию назначения.');
      return;
    }

    const virtualScopeRegionCodes = buildVirtualScopeRegionCodes(
      selectedStation,
      selectedVirtualDestination
    );

    for (const code of virtualScopeRegionCodes) {
      if (!loadedRegionCodes.includes(code)) {
        setVirtualRouteError(
          'Один из округов маршрута сейчас не загружен. Загрузите нужный округ и повторите построение.'
        );
        return;
      }
    }

    try {
      setVirtualRouteLoading(true);
      setVirtualRouteError('');
      setVirtualRouteMessage('Подготавливаем topology graph...');

      routeDebug('Virtual route request', {
        origin_station_id: selectedStation?.id,
        destination_station_id: selectedVirtualDestination?.id,
        scope_region_codes: virtualScopeRegionCodes,
      });

      await ensureTopologyForRegions(virtualScopeRegionCodes);

      setVirtualRouteMessage('Строим виртуальный путь по OSM...');

      const response = await fetch(`${BACKEND_URL}/api/virtual-routes/path`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          origin_station_id: selectedStation.id,
          destination_station_id: selectedVirtualDestination.id,
          scope_region_codes: virtualScopeRegionCodes,
        }),
      });

      if (!response.ok) {
        throw new Error(
          await readApiError(response, 'Не удалось построить виртуальный OSM-маршрут')
        );
      }

      const data = await response.json();

      routeDebug('Virtual route response', {
        status: data.status,
        message: data.message,
        geometry_ready: Boolean(data.geometry || data.item?.geometry),
        network_segments_count: (data.network_segments || data.item?.network_segments || []).length,
        summary: data.summary,
        diagnostics: data.diagnostics || data.item?.diagnostics,
      });

      if (data.status !== 'ok') {
        setVirtualRouteMessage(data.message || 'Виртуальный путь не построен.');
        return;
      }

      const rawNetworkSegments = data.network_segments || data.item?.network_segments || [];
      const virtualNetworkSegments = rawNetworkSegments.map((segment) => ({
        ...segment,
        segment_source: segment.segment_source || 'virtual_osm_path',
      }));

      const normalizedVirtualRoute = {
        ...(data.item || {}),
        id: data.route?.id || `virtual-${selectedStation.id}-${selectedVirtualDestination.id}`,
        source_system: 'virtual_osm',
        route_name: 'Теоретический путь по OSM',
        origin_station_name: selectedStation.name,
        destination_station_name: selectedVirtualDestination.name,
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
      setVirtualRouteError(
        err instanceof Error ? err.message : 'Ошибка построения виртуального маршрута'
      );
    } finally {
      setVirtualRouteLoading(false);
    }
  }, [
    selectedStation,
    selectedVirtualDestination,
    loadedRegionCodes,
    ensureTopologyForRegions,
  ]);

  const handleImportRzdTrain = useCallback(async (train) => {
    if (!train?.train_number) {
      setRzdError('Не выбран номер поезда.');
      return;
    }

    try {
      setRzdImportLoading(true);
      setRzdError('');
      setRzdMessage('Импортируем выбранный поезд...');

      routeDebug('RZD train import request', {
        train,
        selectedStation,
        selectedRzdDestination,
      });

      const response = await fetch(`${BACKEND_URL}/api/rzd/trains/import`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          train_number: train.train_number,
          dep_date: train.search_date || rzdDepDate,
          origin_code: train.used_origin_code || null,
          destination_code: train.used_destination_code || null,
          origin_station_name: selectedStation?.name || train.origin_name || null,
          destination_station_name: selectedRzdDestination?.name || train.destination_name || null,
          route_name: train.brand || `Поезд ${train.train_number}`,
          notes: 'Imported from RZD API via frontend A-B flow',
        }),
      });

      if (!response.ok) {
        throw new Error(
          await readApiError(response, 'Не удалось импортировать выбранный поезд')
        );
      }

      const data = await response.json();
      routeDebug('RZD train import response', data);
      const routeId = data.route_id || data.item?.id || data.route?.id;

      if (!routeId) {
        throw new Error('Импорт выполнен, но backend не вернул route_id');
      }

      setRzdMessage(data.message || 'Маршрут импортирован. Загружаем его на карту...');

      await handleSelectRoute(routeId);
      setSidebarMode('routes');
    } catch (err) {
      console.error(err);
      setRzdError(err instanceof Error ? err.message : 'Ошибка импорта поезда');
    } finally {
      setRzdImportLoading(false);
    }
  }, [
    rzdDepDate,
    selectedRzdDestination,
    selectedStation,
    handleSelectRoute,
  ]);

  const handleCheckRegionUpdates = useCallback(async (regionCode) => {
    if (!regionCode) {
      return;
    }

    setUpdateStates((prev) => ({
      ...prev,
      [regionCode]: {
        ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
        status: 'checking',
        message: 'Проверяем наличие обновлений...',
        can_update: false,
      },
    }));

    try {
      const response = await fetch(
        `${BACKEND_URL}/api/updates/check?region_code=${encodeURIComponent(regionCode)}`
      );

      if (!response.ok) {
        throw new Error('Не удалось проверить наличие обновлений');
      }

      const data = await response.json();
      const item = data.item || DEFAULT_UPDATE_STATE;

      setUpdateStates((prev) => ({
        ...prev,
        [regionCode]: item,
      }));
    } catch (err) {
      console.error(err);
      setUpdateStates((prev) => ({
        ...prev,
        [regionCode]: {
          status: 'check_failed',
          message: 'Не удалось проверить наличие обновлений',
          error: err instanceof Error ? err.message : 'Неизвестная ошибка',
          can_update: false,
        },
      }));
    }
  }, []);

  const handleRunRegionUpdate = useCallback(async (regionCode) => {
    if (!regionCode) {
      return;
    }

    setUpdateStates((prev) => ({
      ...prev,
      [regionCode]: {
        ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
        status: 'starting',
        message: 'Запускаем обновление...',
        can_update: false,
      },
    }));

    try {
      const response = await fetch(`${BACKEND_URL}/api/updates/run`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ region_code: regionCode }),
      });

      if (!response.ok) {
        throw new Error('Не удалось запустить обновление');
      }

      const data = await response.json();

      if (data.status === 'started' || data.status === 'already_running') {
        setUpdateStates((prev) => ({
          ...prev,
          [regionCode]: {
            ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
            status: 'running',
            message: 'Обновление выполняется...',
            can_update: false,
          },
        }));
        return;
      }

      if (data.status === 'not_required') {
        setUpdateStates((prev) => ({
          ...prev,
          [regionCode]: {
            ...(data.item || DEFAULT_UPDATE_STATE),
            status: 'up_to_date',
            message: 'Обновление не требуется',
            can_update: false,
          },
        }));
        return;
      }

      if (data.status === 'check_failed') {
        setUpdateStates((prev) => ({
          ...prev,
          [regionCode]: {
            ...(data.item || DEFAULT_UPDATE_STATE),
            status: 'check_failed',
            message: 'Не удалось проверить наличие обновлений',
            can_update: false,
          },
        }));
        return;
      }
    } catch (err) {
      console.error(err);
      setUpdateStates((prev) => ({
        ...prev,
        [regionCode]: {
          ...(prev[regionCode] || DEFAULT_UPDATE_STATE),
          status: 'failed',
          message: 'Не удалось запустить обновление',
          notes: err instanceof Error ? err.message : 'Неизвестная ошибка',
          can_update: false,
        },
      }));
    }
  }, []);

  const handleCheckAllUpdates = useCallback(async () => {
    setCheckingAllUpdates(true);

    try {
      const response = await fetch(`${BACKEND_URL}/api/updates/check-all`);

      if (!response.ok) {
        throw new Error('Не удалось проверить обновления по всем округам');
      }

      const data = await response.json();
      const items = data.items || [];

      const nextStates = {};
      for (const item of items) {
        nextStates[item.region_code] = item;
      }

      setUpdateStates((prev) => ({
        ...prev,
        ...nextStates,
      }));
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
      const response = await fetch(`${BACKEND_URL}/api/updates/run-all-available`, {
        method: 'POST',
      });

      if (!response.ok) {
        throw new Error('Не удалось запустить обновление для всех доступных округов');
      }

      const data = await response.json();
      const startedRegions = data.regions_started || [];

      if (startedRegions.length > 0) {
        setUpdateStates((prev) => {
          const next = { ...prev };

          for (const regionCode of startedRegions) {
            next[regionCode] = {
              ...(next[regionCode] || DEFAULT_UPDATE_STATE),
              status: 'running',
              message: 'Обновление выполняется...',
              can_update: false,
            };
          }

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
      <Header selectionMode={selectionMode} sidebarMode={sidebarMode} />
      <main
        className="main"
        style={{
          display: 'flex',
          gap: 16,
          minHeight: 0,
        }}
      >
        {!selectionMode && (
          <LoadedSidebar
            panelMode={sidebarMode}
            setPanelMode={setSidebarMode}
            loading={loading}
            error={error}
            stations={stations}
            linesCount={linesCount}
            searchQuery={searchQuery}
            setSearchQuery={setSearchQuery}
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
            onSearchRoutes={handleSearchRoutes}
            onResetRoutes={handleResetRoutes}
            selectedRoute={selectedRoute}
            onSelectRoute={handleSelectRoute}
            onClearRoute={handleClearRoute}
            stationRoutes={stationRoutes}
            rzdOriginProfile={rzdOriginProfile}
            rzdOriginCode={rzdOriginCode}
            setRzdOriginCode={setRzdOriginCode}
            rzdDestinationQuery={rzdDestinationQuery}
            setRzdDestinationQuery={setRzdDestinationQuery}
            rzdDestinationOptions={rzdDestinationOptions}
            selectedRzdDestination={selectedRzdDestination}
            onSearchRzdDestination={handleSearchRzdDestination}
            onSelectRzdDestination={handleSelectRzdDestination}
            rzdDepDate={rzdDepDate}
            setRzdDepDate={setRzdDepDate}
            rzdIncludeTransfers={rzdIncludeTransfers}
            setRzdIncludeTransfers={setRzdIncludeTransfers}
            rzdTrains={rzdTrains}
            rzdSearchLoading={rzdSearchLoading}
            rzdImportLoading={rzdImportLoading}
            rzdError={rzdError}
            rzdMessage={rzdMessage}
            rzdCalendarDebug={rzdCalendarDebug}
            onSearchRzdRoutes={handleSearchRzdRoutes}
            onImportRzdTrain={handleImportRzdTrain}
            destinationNearbyRoutes={destinationNearbyRoutes}
            destinationNearbyRoutesLoading={destinationNearbyRoutesLoading}
            rzdSearchProgress={rzdSearchProgress}
            rzdSearchProgressMessage={rzdSearchProgressMessage}
            virtualDestinationQuery={virtualDestinationQuery}
            setVirtualDestinationQuery={setVirtualDestinationQuery}
            virtualDestinationOptions={virtualDestinationOptions}
            selectedVirtualDestination={selectedVirtualDestination}
            onSearchVirtualDestination={handleSearchVirtualDestination}
            onSelectVirtualDestination={handleSelectVirtualDestination}
            onBuildVirtualRoute={handleBuildVirtualRoute}
            virtualRouteLoading={virtualRouteLoading}
            virtualRouteError={virtualRouteError}
            virtualRouteMessage={virtualRouteMessage}
            topologyProgress={topologyProgress}
            topologyProgressMessage={topologyProgressMessage}
          />
        )}

        <div
          style={{
            position: 'relative',
            flex: 1,
            minWidth: 0,
            minHeight: 0,
            height: '100%',
            display: 'flex',
          }}
        >
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
          />

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

          {loading && (
            <LoadingOverlay
              title="Загрузка данных"
              progress={loadingProgress}
              message={loadingMessage}
            />
          )}

          {routeLoadingVisible && (
            <LoadingOverlay
              title="Выбранный маршрут прогружается"
              progress={routeLoadingProgress}
              message={routeLoadingMessage}
            />
          )}
        </div>
      </main>
    </div>
  );
}
