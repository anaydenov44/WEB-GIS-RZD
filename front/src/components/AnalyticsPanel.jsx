function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }
  return new Intl.NumberFormat('ru-RU').format(Number(value));
}

function formatKm(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }
  return `${Number(value).toFixed(1)} км`;
}

function formatMoney(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }

  const billion = Number(value) / 1_000_000_000;
  if (billion >= 1) {
    return `${billion.toFixed(2)} млрд ₽`;
  }

  return `${(Number(value) / 1_000_000).toFixed(0)} млн ₽`;
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return '—';
  }
  return `${Math.round(Number(value) * 100)}%`;
}

function attentionLabel(level) {
  switch (level) {
    case 'high':
      return 'высокий';
    case 'medium':
      return 'средний';
    default:
      return 'низкий';
  }
}

function RangeControl({ label, value, min, max, step = 1, suffix = '', onChange }) {
  return (
    <label style={{ display: 'grid', gap: 6 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 10 }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: '#475569' }}>{label}</span>
        <strong style={{ fontSize: 13, color: '#111827' }}>{value}{suffix}</strong>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

export default function AnalyticsPanel({
  selectedRoute,
  params,
  setParams,
  analyticsResult,
  analyticsLoading,
  analyticsError,
  onRunAnalysis,
  onExitAnalytics,
  selectedCandidate,
  onSelectCandidate,
  showHeatmap,
  setShowHeatmap,
  showPoints,
  setShowPoints,
  alternativesParams,
  setAlternativesParams,
  routeAlternatives,
  alternativesLoading,
  alternativesError,
  onRunAlternatives,
  selectedAlternativeId,
  onSelectAlternative,
  showAlternatives,
  setShowAlternatives,
}) {
  const settlements = analyticsResult?.settlements || [];
  const summary = analyticsResult?.summary || null;

  const visibleSettlements = settlements.filter((item) => {
    const population = Number(item.population ?? 0);
    const minPopulation = Number(params.min_population ?? 0);
    const maxPopulation = Number(params.max_population ?? Number.POSITIVE_INFINITY);

    if (population < minPopulation) {
      return false;
    }

    if (population > maxPopulation) {
      return false;
    }

    return true;
  });

  const topSettlements = visibleSettlements.slice(0, 40);

  return (
    <aside className="sidebar">
      <section className="card">
        <div style={{ fontSize: 13, color: '#64748b', marginBottom: 6 }}>
          Режим аналитики
        </div>
        <h2 style={{ marginTop: 0 }}>Аналитика маршрута</h2>
        <p style={{ marginTop: 0, color: '#475569', lineHeight: 1.45 }}>
          Анализ населённых пунктов в транспортном коридоре выбранного маршрута.
        </p>

        <div
          style={{
            background: '#f8fafc',
            border: '1px solid #e2e8f0',
            borderRadius: 14,
            padding: 12,
            marginBottom: 12,
          }}
        >
          <div style={{ fontSize: 13, color: '#64748b', marginBottom: 4 }}>Маршрут</div>
          <div style={{ fontSize: 15, fontWeight: 800, color: '#111827' }}>
            {selectedRoute?.route_name || selectedRoute?.train_number || 'Выбранный маршрут'}
          </div>
        </div>

        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
          <button className="subtle-button" onClick={onRunAnalysis} disabled={analyticsLoading}>
            {analyticsLoading ? 'Считаем...' : 'Пересчитать'}
          </button>
          <button className="subtle-button" onClick={onExitAnalytics}>
            Выйти из аналитики
          </button>
        </div>

        {analyticsError && (
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
            {analyticsError}
          </div>
        )}
      </section>

      <section className="card">
        <h2>Параметры анализа</h2>

        <div style={{ display: 'grid', gap: 14 }}>
          <RangeControl
            label="Радиус поиска"
            value={params.corridor_km}
            min={5}
            max={100}
            step={5}
            suffix=" км"
            onChange={(value) => setParams((prev) => ({ ...prev, corridor_km: value }))}
          />

          <RangeControl
            label="Мин. население"
            value={params.min_population}
            min={3000}
            max={100000}
            step={1000}
            onChange={(value) => setParams((prev) => ({ ...prev, min_population: value }))}
          />

          <RangeControl
            label="Макс. население"
            value={params.max_population ?? 500000}
            min={10000}
            max={2000000}
            step={10000}
            onChange={(value) => setParams((prev) => ({ ...prev, max_population: value }))}
          />

          <RangeControl
            label="Макс. объектов"
            value={params.max_results}
            min={50}
            max={2000}
            step={50}
            onChange={(value) => setParams((prev) => ({ ...prev, max_results: value }))}
          />

          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={params.exclude_aggregate_like_names}
              onChange={(event) =>
                setParams((prev) => ({
                  ...prev,
                  exclude_aggregate_like_names: event.target.checked,
                }))
              }
            />
            <span>Скрыть агрегированные записи</span>
          </label>
        </div>
      </section>

      <section className="card">
        <h2>Слои аналитики</h2>
        <div style={{ display: 'grid', gap: 10 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={showHeatmap}
              onChange={(event) => setShowHeatmap(event.target.checked)}
            />
            <span>Тепловая карта score</span>
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={showPoints}
              onChange={(event) => setShowPoints(event.target.checked)}
            />
            <span>Точки населённых пунктов</span>
          </label>
        </div>
      </section>

      <section className="card">
        <h2>Альтернативные маршруты</h2>
        <p style={{ marginTop: 0, color: '#475569', lineHeight: 1.45 }}>
          Система строит несколько виртуальных путей между начальной и конечной станцией маршрута
          по topology graph. Повторно использованные рёбра штрафуются, чтобы варианты отличались.
        </p>

        <div style={{ display: 'grid', gap: 12, marginBottom: 14 }}>
          <RangeControl
            label="Кол-во альтернатив"
            value={alternativesParams.max_alternatives}
            min={1}
            max={6}
            step={1}
            onChange={(value) =>
              setAlternativesParams((prev) => ({ ...prev, max_alternatives: value }))
            }
          />

          <RangeControl
            label="Макс. удлинение"
            value={alternativesParams.max_length_ratio}
            min={1}
            max={3}
            step={0.1}
            suffix="×"
            onChange={(value) =>
              setAlternativesParams((prev) => ({ ...prev, max_length_ratio: value }))
            }
          />

          <RangeControl
            label="Мин. отличие"
            value={Math.round(alternativesParams.min_difference_ratio * 100)}
            min={0}
            max={80}
            step={5}
            suffix="%"
            onChange={(value) =>
              setAlternativesParams((prev) => ({
                ...prev,
                min_difference_ratio: value / 100,
              }))
            }
          />

          <RangeControl
            label="Penalty factor"
            value={alternativesParams.penalty_factor}
            min={1.2}
            max={8}
            step={0.2}
            suffix="×"
            onChange={(value) =>
              setAlternativesParams((prev) => ({ ...prev, penalty_factor: value }))
            }
          />

          <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={showAlternatives}
              onChange={(event) => setShowAlternatives(event.target.checked)}
            />
            <span>Показывать альтернативы на карте</span>
          </label>
        </div>

        <button className="subtle-button" onClick={onRunAlternatives} disabled={alternativesLoading}>
          {alternativesLoading ? 'Строим альтернативы...' : 'Построить альтернативы'}
        </button>

        {alternativesError && (
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
            {alternativesError}
          </div>
        )}

        {routeAlternatives?.alternatives?.length > 0 && (
          <div style={{ display: 'grid', gap: 8, marginTop: 12 }}>
            {routeAlternatives.alternatives.map((alternative) => (
              <button
                key={alternative.id}
                className={
                  selectedAlternativeId === alternative.id
                    ? 'station-list-item active'
                    : 'station-list-item'
                }
                onClick={() => onSelectAlternative(alternative.id)}
              >
                <div className="station-list-name">
                  Альтернатива {alternative.display_rank || alternative.rank - 1} · {formatKm(alternative.length_km)}
                </div>
                <div className="station-list-meta">
                  Удлинение {alternative.length_ratio.toFixed(2)}× • отличие {formatPercent(alternative.difference_ratio)} • совпадение {formatPercent(alternative.overlap_ratio)} • рёбер {formatNumber(alternative.edges_count)}
                </div>
              </button>
            ))}
          </div>
        )}
      </section>

      {summary && (
        <section className="card">
          <h2>Сводка</h2>
          <div className="route-details-grid">
            <div className="route-chip">
              <span>Пунктов</span>
              <strong>{formatNumber(summary.settlements_in_corridor)}</strong>
            </div>
            <div className="route-chip">
              <span>Кандидатов</span>
              <strong>{formatNumber(summary.candidate_settlements)}</strong>
            </div>
            <div className="route-chip">
              <span>Недообслужено</span>
              <strong>{formatNumber(summary.underserved_population)}</strong>
            </div>
            <div className="route-chip">
              <span>Max score</span>
              <strong>{summary.max_attention_score}</strong>
            </div>
          </div>
        </section>
      )}

      {selectedCandidate && (
        <section className="card">
          <h2>Выбранный кандидат</h2>
          <div style={{ display: 'grid', gap: 7, fontSize: 14, color: '#334155' }}>
            <div><strong>{selectedCandidate.name}</strong></div>
            <div>Регион: {selectedCandidate.region || '—'}</div>
            <div>Население: {formatNumber(selectedCandidate.population)}</div>
            <div>До маршрута: {formatKm(selectedCandidate.distance_to_route_km)}</div>
            <div>До станции маршрута: {formatKm(selectedCandidate.distance_to_nearest_route_station_km)}</div>
            <div>Score: <strong>{selectedCandidate.score}</strong> ({attentionLabel(selectedCandidate.attention_level)})</div>
            <div>Оценка подключения: {formatMoney(selectedCandidate.estimated_connection_cost)}</div>
            <div>Стоимость на 1000 жителей: {formatMoney(selectedCandidate.cost_per_1000_people)}</div>
          </div>
        </section>
      )}

      <section className="card">
        <h2>Кандидаты</h2>
        {analyticsLoading ? (
          <p>Идёт расчёт...</p>
        ) : topSettlements.length === 0 ? (
          <p>Нет населённых пунктов под заданные параметры.</p>
        ) : (
          <div style={{ display: 'grid', gap: 8, maxHeight: 420, overflowY: 'auto', paddingRight: 4 }}>
            {topSettlements.map((item) => (
              <button
                key={item.id}
                className={
                  selectedCandidate?.id === item.id
                    ? 'station-list-item active'
                    : 'station-list-item'
                }
                onClick={() => onSelectCandidate(item)}
              >
                <div className="station-list-name">
                  {item.name} · score {item.score}
                </div>
                <div className="station-list-meta">
                  {formatNumber(item.population)} чел. • до маршрута {formatKm(item.distance_to_route_km)} • {attentionLabel(item.attention_level)}
                </div>
              </button>
            ))}
          </div>
        )}
      </section>
    </aside>
  );
}
