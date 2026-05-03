import { useEffect, useState } from 'react';

export function StationSearchSelect({
  label,
  placeholder = 'Введите название станции',
  selectedStation,
  onSelect,
  disabled = false,
  apiBaseUrl = 'http://127.0.0.1:8000',
}) {
  const [query, setQuery] = useState(selectedStation?.name ?? '');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(false);
  const [opened, setOpened] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    setQuery(selectedStation?.name ?? '');
    setItems([]);
    setOpened(false);
    setError(null);
  }, [selectedStation?.id, selectedStation?.name]);

  async function runSearch() {
    const normalizedQuery = query.trim();

    if (disabled || normalizedQuery.length < 2) {
      setItems([]);
      setOpened(false);
      setError('Введите минимум 2 символа');
      return;
    }

    try {
      setLoading(true);
      setError(null);
      setOpened(false);

      const response = await fetch(
        `${apiBaseUrl}/api/search/stations?q=${encodeURIComponent(normalizedQuery)}&limit=20`
      );

      if (!response.ok) {
        throw new Error(`Station search failed: ${response.status}`);
      }

      const payload = await response.json();
      const nextItems = Array.isArray(payload) ? payload : payload.items ?? payload.stations ?? [];

      setItems(nextItems);
      setOpened(true);

      if (nextItems.length === 0) {
        setError('Станции не найдены');
      }
    } catch (err) {
      console.error(err);
      setItems([]);
      setOpened(false);
      setError('Ошибка поиска станции');
    } finally {
      setLoading(false);
    }
  }

  function handleSelect(station) {
    setQuery(station.name || '');
    setItems([]);
    setOpened(false);
    setError(null);
    onSelect(station);
  }

  return (
    <div className="station-search-select">
      {label && <label className="station-search-select__label">{label}</label>}

      <div className="station-search-select__row">
        <input
          className="station-search-select__input"
          value={query}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(event) => {
            setQuery(event.target.value);
            setItems([]);
            setOpened(false);
            setError(null);
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              runSearch();
            }
          }}
        />

        <button
          type="button"
          className="station-search-select__button"
          disabled={disabled || loading}
          onClick={runSearch}
        >
          {loading ? 'Ищу...' : 'Найти'}
        </button>
      </div>

      {error && <div className="station-search-select__hint">{error}</div>}

      {opened && items.length > 0 && (
        <div className="station-search-select__results">
          {items.map((station) => (
            <button
              key={station.id}
              type="button"
              className="station-search-select__item"
              onClick={() => handleSelect(station)}
            >
              <span className="station-search-select__name">
                {station.name || 'Без названия'}
              </span>

              <span className="station-search-select__meta">
                {station.region_code || 'округ не указан'}
                {station.station_type || station.type
                  ? ` · ${station.station_type || station.type}`
                  : ''}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
