export const REGION_LABELS = {
  central_fd: 'Центральный федеральный округ',
  northwestern_fd: 'Северо-Западный федеральный округ',
  south_fd: 'Южный федеральный округ',
  north_caucasus_fd: 'Северо-Кавказский федеральный округ',
  volga_fd: 'Приволжский федеральный округ',
  ural_fd: 'Уральский федеральный округ',
  siberian_fd: 'Сибирский федеральный округ',
  far_eastern_fd: 'Дальневосточный федеральный округ',
};

export const REGION_SHORT_LABELS = {
  central_fd: 'Центральный ФО',
  northwestern_fd: 'Северо-Западный ФО',
  south_fd: 'Южный ФО',
  north_caucasus_fd: 'СКФО',
  volga_fd: 'Приволжский ФО',
  ural_fd: 'Уральский ФО',
  siberian_fd: 'Сибирский ФО',
  far_eastern_fd: 'Дальневосточный ФО',
};

export function formatRegionCode(value) {
  if (!value) {
    return 'не указан';
  }

  return REGION_LABELS[value] ?? value;
}

export function formatRegionShortCode(value) {
  if (!value) {
    return 'не указан';
  }

  return REGION_SHORT_LABELS[value] ?? REGION_LABELS[value] ?? value;
}

export const STATION_TYPE_LABELS = {
  station: 'Станция',
  halt: 'Остановочный пункт',
  stop: 'Точка остановки поезда',
  platform: 'Платформа',
  tram_stop: 'Трамвайная остановка',
  subway_entrance: 'Вход в метро',
  train_station_entrance: 'Вход на станцию',
  yard: 'Сортировочная / грузовая станция',
};

export function formatStationType(value) {
  if (!value) {
    return 'не указан';
  }

  return STATION_TYPE_LABELS[value] ?? value;
}

export const MODE_LABELS = {
  infrastructure: 'Режим инфраструктуры',
  analytics: 'Режим аналитики',
  routes: 'Режим РЖД',
  virtual: 'Виртуальный маршрут по OSM',
  virtual_route: 'Виртуальный маршрут по OSM',
  rzd_route: 'Поиск поезда РЖД',
  region_selection: 'Выбор округов',
  research: 'Режим инфраструктуры',
};

export function formatModeLabel(mode) {
  if (!mode) {
    return 'Режим инфраструктуры';
  }

  return MODE_LABELS[mode] ?? mode;
}
