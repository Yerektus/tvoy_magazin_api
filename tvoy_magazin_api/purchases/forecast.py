"""Прогноз спроса по дневным продажам.

Ряд считает StatsForecast: среднее, сглаживание, Хольт, Хольт–Винтерс, AutoETS,
AutoTheta, Кростон и годовой сезонный naïve. Какие алгоритмы доступны, зависит
от длины ряда, лучший выбирается по отложенному хвосту. Если ошибка всё ещё
высокая, дешёвые модели пробуют неделю, Кростона и усадку к обороту магазина,
а короткую историю дополняют более длинной. Акционный всплеск не должен стать
нормой — скидку снимаем с ряда до отбора модели, на графике оставляем факт.
Пустую полку тоже снимаем: обвал продаж при открытом магазине — это дефицит, а
не спрос, иначе прогноз занизит заказ и дефицит продлится.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from math import isfinite, sqrt
from statistics import median

import holidays
from django.db.models import Sum
from django.utils import timezone

from umag.models import UmagDailyDemand

ZERO = Decimal('0')
THREE = Decimal('0.001')
MAX_HOLIDAY_FACTOR = 3.0
MIN_HOLIDAY_FACTOR = 0.5
SERVICE_LEVEL_Z = 1.28  # около 90% при близких к нормальным остатках ошибки
# День считаем акционным, если хотя бы треть объёма ушла дешевле обычной цены.
PROMO_SHARE = 0.3
# День считаем дефицитным, если стабильный товар рухнул при открытом магазине:
# резкий обвал — пустая полка, плавное затухание — настоящий спад спроса.
STOCKOUT_DROP = 0.3
STOCKOUT_STORE_SHARE = 0.5
STOCKOUT_MIN_MEDIAN = 5.0
WEEK = 7
YEAR = 364
# Длиннее двух лет модели почти не выигрывают, а AutoETS/Theta на 4 годах
# карточки начинают считаться секундами.
MAX_FIT_DAYS = YEAR * 2
MODELS = (
    'average',
    'weighted_average',
    'weekly_average',
    'holt',
    'holt_winters_weekly',
    'auto_ets',
    'auto_theta',
    'croston_sba',
    'seasonal_naive_week',
    'seasonal_naive_year',
    'pooled_weekly',
)
# Без StatsForecast: план и список считают тысячи рядов, пакетный ETS — минуты.
LOCAL_MODELS = (
    'average',
    'weighted_average',
    'weekly_average',
    'croston_sba',
    'seasonal_naive_week',
    'seasonal_naive_year',
    'pooled_weekly',
)
PLAN_MODELS = LOCAL_MODELS
PLAN_FIT_DAYS = 90
# Как на карточке: ошибка до 25% — высокая, до 50% — средняя. Выше — пробуем
# другую модель и более длинную историю.
GOOD_ERROR = 0.25
FAIR_ERROR = 0.5
# Редкий товар: ноль в четыре дня из десяти. Дневной WAPE тогда врёт, смотрим
# неделю — столько обычно заказывают.
SPARSE_ZERO_SHARE = 0.4
WEEK_BUCKET = 7
# Пакет StatsForecast на всю номенклатуру собирает миллионы строк в DataFrame.
SF_BATCH = 400


@dataclass(frozen=True)
class Forecast:
    model: str
    quantity: Decimal
    per_day: Decimal
    safety_stock: Decimal
    holiday_factor: Decimal
    error: Decimal
    observations: int
    daily: tuple[Decimal, ...]
    daily_error: Decimal

    def limited_to(self, days: int) -> 'Forecast':
        """Тот же прогноз, но только до срока безопасной продажи товара."""

        days = max(1, min(days, len(self.daily)))

        if days == len(self.daily):
            return self

        daily = self.daily[:days]
        demand = sum(daily, ZERO)
        safety = min(
            SERVICE_LEVEL_Z * float(self.daily_error) * sqrt(days),
            float(demand) * 0.5,
        )

        return Forecast(
            model=self.model,
            quantity=demand.quantize(THREE),
            per_day=(demand / Decimal(days)).quantize(THREE),
            safety_stock=_amount(safety),
            holiday_factor=self.holiday_factor,
            error=self.error,
            observations=self.observations,
            daily=daily,
            daily_error=self.daily_error,
        )


def for_products(
    organization,
    store_id: int,
    barcodes: set[str] | None,
    horizon: int,
    *,
    as_of: date | None = None,
    models: set[str] | tuple[str, ...] | None = None,
    max_days: int | None = None,
) -> dict[str, Forecast]:
    """Строит прогнозы для товаров магазина до начала сегодняшнего дня."""

    if (barcodes is not None and not barcodes) or horizon <= 0:
        return {}

    as_of = as_of or timezone.localdate()
    history_end = as_of - timedelta(days=1)
    window = min(MAX_FIT_DAYS, max_days or MAX_FIT_DAYS)
    fit_start = history_end - timedelta(days=window - 1)
    store_start = history_end - timedelta(days=MAX_FIT_DAYS - 1)
    sparse, promos = _daily_history(
        organization,
        store_id,
        barcodes=barcodes,
        since=fit_start,
    )
    wanted = {barcode: rows for barcode, rows in sparse.items() if rows}

    if not wanted and not barcodes:
        return {}

    store_sparse = _store_daily_totals(organization, store_id, store_start, history_end)
    calendar = _calendar(store_start, as_of + timedelta(days=horizon))
    store_dates, store_values = _series(store_sparse, store_start, history_end)
    future_dates = [as_of + timedelta(days=offset) for offset in range(horizon)]
    store_factor_cache: dict[tuple[str, int], tuple[float, int]] = {}
    prepared = _prepare_many(
        wanted,
        promos,
        fit_start,
        history_end,
        as_of,
        store_dates,
        store_values,
    )
    forecasts = _forecast_prepared(
        prepared,
        horizon,
        future_dates,
        calendar,
        store_dates,
        store_values,
        store_factor_cache,
        models=models,
    )

    if barcodes:
        forecasts = _improve_weak(
            forecasts,
            barcodes,
            organization,
            store_id,
            horizon,
            as_of,
            history_end,
            store_dates,
            store_values,
            calendar,
            future_dates,
            store_factor_cache,
            models=models,
            window=window,
        )

    return forecasts


def _prepare_many(
    sparse: dict[str, dict[date, float]],
    promos: dict[str, dict[date, float]],
    fit_start: date,
    history_end: date,
    as_of: date,
    store_dates: list[date],
    store_values: list[float],
) -> dict[str, tuple[list[date], list[float]]]:
    """Склеивает дни, снимает акцию и пустую полку — одинаково для всех проходов."""

    prepared: dict[str, tuple[list[date], list[float]]] = {}

    for barcode, rows in sparse.items():
        dates, values = _align_series(rows, fit_start, history_end, as_of)

        if not values:
            continue

        promo_rows = promos.get(barcode, {})
        if promo_rows:
            _, promo_values = _series(promo_rows, dates[0], dates[-1])
            if len(promo_values) != len(values):
                promo_values = [promo_rows.get(day, 0.0) for day in dates]
        else:
            promo_values = [0.0] * len(values)

        values = _clip_promos(dates, values, promo_values)
        values = _clip_stockouts(dates, values, store_dates, store_values)
        prepared[barcode] = (dates, values)

    return prepared


def _align_series(
    rows: dict[date, float],
    fit_start: date,
    history_end: date,
    as_of: date,
) -> tuple[list[date], list[float]]:
    """Ряд с первой продажи в окне. Только сегодня — берём сегодня, иначе пусто."""

    if not rows:
        return [], []

    first_day = max(min(rows), fit_start)

    if first_day <= history_end:
        return _series(rows, first_day, history_end)

    today = max(0.0, float(rows.get(as_of, 0.0)))
    if today > 0:
        return [as_of], [today]

    return [], []


def _forecast_prepared(
    prepared: dict[str, tuple[list[date], list[float]]],
    horizon: int,
    future_dates: list[date],
    calendar: dict[date, str],
    store_dates: list[date],
    store_values: list[float],
    store_factor_cache: dict[tuple[str, int], tuple[float, int]],
    *,
    models: set[str] | tuple[str, ...] | None = None,
) -> dict[str, Forecast]:
    """Отбор модели по уже подготовленным рядам."""

    if not prepared:
        return {}

    selected = _select_many(
        {barcode: values for barcode, (_, values) in prepared.items()},
        horizon,
        allowed=set(models) if models else None,
        dates_of={barcode: dates for barcode, (dates, _) in prepared.items()},
        future_dates=future_dates,
        store_dates=store_dates,
        store_values=store_values,
    )
    forecasts = {}

    for barcode, (dates, values) in prepared.items():
        model, base, error, daily_mae = selected[barcode]
        forecasts[barcode] = _result(
            model,
            base,
            error,
            daily_mae,
            values,
            dates,
            horizon,
            future_dates,
            calendar,
            store_dates,
            store_values,
            store_factor_cache,
        )

    return forecasts


def _improve_weak(
    forecasts: dict[str, Forecast],
    barcodes: set[str],
    organization,
    store_id: int,
    horizon: int,
    as_of: date,
    history_end: date,
    store_dates: list[date],
    store_values: list[float],
    calendar: dict[date, str],
    future_dates: list[date],
    store_factor_cache: dict[tuple[str, int], tuple[float, int]],
    *,
    models: set[str] | tuple[str, ...] | None,
    window: int,
) -> dict[str, Forecast]:
    """Низкая точность или дырка в окне — считаем на более длинной истории.

    План смотрит 90 дней, чтобы не тащить все ряды. У кого среднее врёт или
    продаж в окне нет, берём до двух лет и сезонный хвост прошлых лет.
    """

    missing = barcodes - set(forecasts)
    weak = {
        barcode
        for barcode, prediction in forecasts.items()
        if float(prediction.error) > FAIR_ERROR
    }
    retry = missing | weak
    long_start = history_end - timedelta(days=MAX_FIT_DAYS - 1)

    if retry and window < MAX_FIT_DAYS:
        extra_sparse, extra_promos = _daily_history(
            organization,
            store_id,
            barcodes=retry,
            since=long_start,
        )
        extra = _forecast_prepared(
            _prepare_many(
                extra_sparse,
                extra_promos,
                long_start,
                history_end,
                as_of,
                store_dates,
                store_values,
            ),
            horizon,
            future_dates,
            calendar,
            store_dates,
            store_values,
            store_factor_cache,
            models=models,
        )

        for barcode, prediction in extra.items():
            current = forecasts.get(barcode)
            if current is None:
                if prediction.quantity > 0:
                    forecasts[barcode] = prediction
            elif prediction.error < current.error:
                forecasts[barcode] = prediction

    leftover = barcodes - set(forecasts)
    if leftover:
        forecasts.update(for_same_season(organization, store_id, leftover, horizon, as_of=as_of))

    return forecasts


def for_same_season(
    organization,
    store_id: int,
    barcodes: set[str],
    horizon: int,
    *,
    as_of: date | None = None,
) -> dict[str, Forecast]:
    """Спрос того же календаря в прошлые годы — для товаров вне короткого отчёта.

    Обычный отбор модели смотрит на хвост ряда: у ёлки он сейчас нулевой, и
    в план она не попадает. Берём, сколько ушло в эти же дни год и два назад.
    """

    if not barcodes or horizon <= 0:
        return {}

    as_of = as_of or timezone.localdate()
    by_year: dict[int, dict[str, dict[date, float]]] = {
        1: defaultdict(lambda: defaultdict(float)),
        2: defaultdict(lambda: defaultdict(float)),
    }

    for years in (1, 2):
        start = shift_years(as_of, years)
        end = start + timedelta(days=horizon - 1)
        for row in UmagDailyDemand.objects.filter(
            organization=organization,
            store_id=store_id,
            barcode__in=barcodes,
            day__gte=start,
            day__lte=end,
        ).values('barcode', 'day', 'quantity'):
            day = _as_date(row['day'])
            if day is None:
                continue
            by_year[years][row['barcode']][day] += max(0.0, float(row['quantity'] or 0))

    forecasts = {}

    for barcode in set(by_year[1]) | set(by_year[2]):
        primary = by_year[1].get(barcode) or by_year[2].get(barcode)
        if not primary:
            continue

        start = min(primary)
        _, values = _series(primary, start, start + timedelta(days=horizon - 1))
        demand = sum(values)

        if demand <= 0:
            continue

        previous_rows = by_year[2].get(barcode) if barcode in by_year[1] else None
        if previous_rows:
            previous_start = min(previous_rows)
            _, previous = _series(
                previous_rows,
                previous_start,
                previous_start + timedelta(days=horizon - 1),
            )
            scored = _score(values, previous[: len(values)])
            error = scored[0] if scored else FAIR_ERROR
            daily_mae = scored[1] if scored else 0.0
        else:
            error = FAIR_ERROR
            daily_mae = 0.0

        forecasts[barcode] = Forecast(
            model='seasonal_naive_year',
            quantity=_amount(demand),
            per_day=_amount(demand / horizon),
            safety_stock=ZERO,
            holiday_factor=Decimal('1.000'),
            error=_amount(min(99_999.0, max(0.0, error))),
            observations=len(values),
            daily=tuple(_amount(value) for value in values),
            daily_error=_amount(daily_mae),
        )

    return forecasts


def shift_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, month=2, day=28)


def for_barcode(
    organization,
    store_id: int,
    barcode: str,
    horizon: int,
    *,
    history_days: int = 60,
    as_of: date | None = None,
    model: str | None = None,
    forecast: bool = True,
) -> dict | None:
    """История продаж и прогноз одного товара — для карточки на графике.

    История обрезается до `history_days`: модели хватает последних двух лет,
    а на экране длинный хвост только мешает читать ближайшие недели.
    Прогноз считается отдельно: вкладка «Продажи» его не ждёт.
    """

    if not barcode or horizon <= 0:
        return None

    as_of = as_of or timezone.localdate()
    history_end = as_of - timedelta(days=1)
    sparse, promos = _daily_history(organization, store_id, barcode)
    rows = sparse.get(barcode)

    if not rows:
        return None

    first_day = min(rows)
    dates, values = _series(rows, first_day, history_end)

    if not values:
        return None

    chart_dates, chart_values = _series(rows, first_day, history_end)

    if not chart_dates:
        chart_dates, chart_values = dates, values

    if history_days > 0 and len(chart_dates) > history_days:
        chart_dates = chart_dates[-history_days:]
        chart_values = chart_values[-history_days:]

    history = [
        {'date': day, 'sold': _amount(quantity)}
        for day, quantity in zip(chart_dates, chart_values)
    ]

    if not forecast:
        return {
            'history': history,
            'forecast': None,
            'series': [],
            'as_of': as_of,
            'history_end': history_end,
        }

    store_sparse = _store_daily_totals(organization, store_id, min(rows), history_end)

    if not store_sparse:
        store_sparse = dict(rows)

    first_store_day = min(store_sparse)
    calendar = _calendar(first_store_day, as_of + timedelta(days=horizon))
    store_dates, store_values = _series(store_sparse, first_store_day, history_end)
    future_dates = [as_of + timedelta(days=offset) for offset in range(horizon)]
    _, promo_values = _series(promos.get(barcode, {}), first_day, history_end)
    prediction = _build(
        values,
        dates,
        horizon,
        future_dates,
        calendar,
        store_dates,
        store_values,
        {},
        promo_values,
        model,
    )

    return {
        'history': history,
        'forecast': prediction,
        'series': [
            {'date': day, 'sold': quantity}
            for day, quantity in zip(future_dates, prediction.daily)
        ],
        'as_of': as_of,
        'history_end': history_end,
    }


def _build(
    values: list[float],
    dates: list[date],
    horizon: int,
    future_dates: list[date],
    calendar: dict[date, str],
    store_dates: list[date],
    store_values: list[float],
    store_factor_cache: dict[tuple[str, int], tuple[float, int]],
    promo_values: list[float] | None = None,
    model: str | None = None,
) -> Forecast:
    """Одна модель: отбор, скидки, праздники и страховой запас."""

    values = _clip_promos(dates, values, promo_values)
    values = _clip_stockouts(dates, values, store_dates, store_values)
    model, base, error, daily_mae = _select(
        values,
        horizon,
        model,
        dates=dates,
        future_dates=future_dates,
        store_dates=store_dates,
        store_values=store_values,
    )
    return _result(
        model,
        base,
        error,
        daily_mae,
        values,
        dates,
        horizon,
        future_dates,
        calendar,
        store_dates,
        store_values,
        store_factor_cache,
    )


def _result(
    model: str,
    base: list[float],
    error: float,
    daily_mae: float,
    values: list[float],
    dates: list[date],
    horizon: int,
    future_dates: list[date],
    calendar: dict[date, str],
    store_dates: list[date],
    store_values: list[float],
    store_factor_cache: dict[tuple[str, int], tuple[float, int]],
) -> Forecast:
    """Праздники и страховой запас поверх уже выбранного дневного ряда."""

    adjusted, holiday_factor = _with_holidays(
        base,
        dates,
        values,
        future_dates,
        calendar,
        store_dates,
        store_values,
        store_factor_cache,
    )
    demand = max(0.0, sum(adjusted))
    safety = min(
        SERVICE_LEVEL_Z * daily_mae * sqrt(horizon),
        demand * 0.5,
    )

    return Forecast(
        model=model,
        quantity=_amount(demand),
        per_day=_amount(demand / horizon),
        safety_stock=_amount(max(0.0, safety)),
        holiday_factor=Decimal(str(holiday_factor)).quantize(THREE),
        error=Decimal(str(min(99_999.0, max(0.0, error)))).quantize(THREE),
        observations=len(values),
        daily=tuple(_amount(value) for value in adjusted),
        daily_error=_amount(daily_mae),
    )


def predict(
    values: list[float],
    horizon: int,
    *,
    dates: list[date] | None = None,
    future_dates: list[date] | None = None,
    holiday_calendar: dict[date, str] | None = None,
    store_values: list[float] | None = None,
    store_dates: list[date] | None = None,
    promo_values: list[float] | None = None,
    model: str | None = None,
    allowed: set[str] | None = None,
) -> Forecast:
    """Чистая точка входа для тестов и повторного использования без БД."""

    if horizon <= 0:
        raise ValueError('Горизонт должен быть положительным')
    if not values:
        raise ValueError('История продаж пуста')

    clean = [max(0.0, float(value)) for value in values]
    dates = dates or [date(2000, 1, 1) + timedelta(days=index) for index in range(len(clean))]
    clean = _clip_promos(dates, clean, promo_values)
    given_store = store_values is not None and store_dates is not None
    clean = _clip_stockouts(
        dates,
        clean,
        store_dates or dates,
        store_values or clean,
    )
    future_dates = future_dates or [
        dates[-1] + timedelta(days=index + 1) for index in range(horizon)
    ]
    model, base, error, daily_mae = _select(
        clean,
        horizon,
        model,
        dates=dates,
        future_dates=future_dates,
        store_dates=store_dates if given_store else None,
        store_values=store_values if given_store else None,
        allowed=allowed,
    )
    return _result(
        model,
        base,
        error,
        daily_mae,
        clean,
        dates,
        horizon,
        future_dates,
        holiday_calendar or {},
        store_dates or dates,
        store_values or clean,
        {},
    )


def _daily_quantities(organization, store_id: int) -> dict[str, dict[date, float]]:
    quantities, _ = _daily_history(organization, store_id)
    return quantities


def _daily_history(
    organization,
    store_id: int,
    barcode: str | None = None,
    *,
    barcodes: set[str] | None = None,
    since: date | None = None,
) -> tuple[dict[str, dict[date, float]], dict[str, dict[date, float]]]:
    """Net-demand и сколько из него ушло со скидкой."""

    quantities: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    promos: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    query = UmagDailyDemand.objects.filter(
        organization=organization,
        store_id=store_id,
    )

    if barcode:
        query = query.filter(barcode=barcode)
    elif barcodes is not None:
        if not barcodes:
            return {}, {}
        query = query.filter(barcode__in=barcodes)

    if since is not None:
        query = query.filter(day__gte=since)

    for row in query.values('barcode', 'day', 'quantity', 'promo_quantity'):
        day = _as_date(row['day'])
        if day is None:
            continue
        quantities[row['barcode']][day] = max(0.0, float(row['quantity'] or 0))
        promos[row['barcode']][day] = max(0.0, float(row['promo_quantity'] or 0))

    return quantities, promos


def _store_daily_totals(
    organization,
    store_id: int,
    first: date,
    last: date,
) -> dict[date, float]:
    """Оборот магазина по дням — для пустой полки и праздников, без всех SKU."""

    totals: dict[date, float] = {}

    for row in (
        UmagDailyDemand.objects.filter(
            organization=organization,
            store_id=store_id,
            day__gte=first,
            day__lte=last,
        )
        .values('day')
        .annotate(quantity=Sum('quantity'))
    ):
        day = _as_date(row['day'])

        if day is None:
            continue

        totals[day] = max(0.0, float(row['quantity'] or 0))

    return totals


def _as_date(value) -> date | None:
    """День из агрегата или из TruncDate — сводим к date."""

    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value)

    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _series(rows: dict[date, float], first: date, last: date) -> tuple[list[date], list[float]]:
    first = _as_date(first)
    last = _as_date(last)

    if first is None or last is None or first > last:
        return [], []

    keyed = {
        converted: quantity
        for day, quantity in rows.items()
        if (converted := _as_date(day)) is not None
    }
    days = (last - first).days + 1
    dates = [first + timedelta(days=offset) for offset in range(days)]
    return dates, [max(0.0, keyed.get(day, 0.0)) for day in dates]


def _clip_promos(
    dates: list[date],
    values: list[float],
    promo_values: list[float] | None,
) -> list[float]:
    """Акционный всплеск не должен стать нормой после окончания скидки.

    Постоянная «скидка» на все дни (карта лояльности, цена ниже ценника) не
    трогаем: это уже обычный спрос. Режем только дни, где акция выделяется
    на фоне остальных.
    """

    if not promo_values or len(promo_values) != len(values) or len(dates) != len(values):
        return values

    flagged = [
        value > 0 and promo / value >= PROMO_SHARE
        for value, promo in zip(values, promo_values)
    ]

    if not any(flagged) or all(flagged):
        return values

    by_weekday: dict[int, list[float]] = defaultdict(list)
    rest = []

    for day, value, is_promo in zip(dates, values, flagged):
        if not is_promo:
            by_weekday[day.weekday()].append(value)
            rest.append(value)

    typical_all = median(rest) if rest else None
    cleaned = []

    for day, value, is_promo in zip(dates, values, flagged):
        if not is_promo:
            cleaned.append(value)
            continue

        bucket = by_weekday[day.weekday()]
        typical = median(bucket) if bucket else typical_all
        cleaned.append(min(value, typical) if typical is not None else value)

    return cleaned


def _clip_stockouts(
    dates: list[date],
    values: list[float],
    store_dates: list[date] | None,
    store_values: list[float] | None,
) -> list[float]:
    """Пустая полка не должна ронять прогноз: обвал при открытом магазине
    заменяем обычным спросом дня недели. Редкий товар с нулями через день не
    трогаем: для него ноль — норма, а не дефицит. Общий простой магазина тоже
    не трогаем: если встала вся касса, это не полка.
    """

    if len(dates) != len(values) or len(values) < 8:
        return values
    if not store_dates or not store_values or len(store_dates) != len(store_values):
        return values

    store_by_day = dict(zip(store_dates, store_values))
    store_aligned = [max(0.0, store_by_day.get(day, 0.0)) for day in dates]

    if median(values[-28:]) < STOCKOUT_MIN_MEDIAN and median(values[-90:]) < STOCKOUT_MIN_MEDIAN:
        return values

    clean: list[float] = []
    week_sum = 0.0

    for index, value in enumerate(values):
        if index >= 7:
            baseline = week_sum / 7

            if baseline >= STOCKOUT_MIN_MEDIAN and value < STOCKOUT_DROP * baseline:
                store_typical = median(store_aligned[max(0, index - 28) : index])

                if (
                    median(values[max(0, index - 28) : index]) >= STOCKOUT_MIN_MEDIAN
                    and store_typical > 0
                    and store_aligned[index] >= STOCKOUT_STORE_SHARE * store_typical
                ):
                    # Заменяем средним прошлой недели — локальным и актуальным
                    # для своей эпохи: медиана за всю историю у редкого раньше
                    # товара даёт ноль и только усугубляет провал.
                    value = baseline

        clean.append(value)
        week_sum += value

        if index >= 7:
            week_sum -= clean[index - 7]

    return clean


def _fit_window(values: list[float]) -> list[float]:
    """Обрезает ряд до окна, на котором ещё есть смысл гонять StatsForecast."""

    if len(values) <= MAX_FIT_DAYS:
        return values

    return values[-MAX_FIT_DAYS:]


def _fit_pair(dates: list[date] | None, values: list[float]) -> tuple[list[date], list[float]]:
    values = _fit_window(values)
    if not dates or len(dates) < len(values):
        return _synthetic_dates(len(values)), values
    if len(dates) > len(values):
        dates = dates[-len(values) :]
    return dates, values


def _synthetic_dates(length: int) -> list[date]:
    start = date(2000, 1, 1)
    return [start + timedelta(days=index) for index in range(length)]


def _future_of(dates: list[date], horizon: int) -> list[date]:
    last = dates[-1] if dates else date(2000, 1, 1)
    return [last + timedelta(days=index + 1) for index in range(horizon)]


def _local_average(
    values: list[float],
    horizon: int,
    cap: float | None = None,
) -> list[float]:
    window = values[-min(28, len(values)) :]
    level = sum(window) / len(window)

    if cap is not None:
        level = min(level, cap)

    return [max(0.0, level)] * horizon


def _ses_level(values: list[float], alpha: float = 0.2) -> float:
    if not values:
        return 0.0

    level = values[0]
    for value in values[1:]:
        level = alpha * value + (1 - alpha) * level
    return max(0.0, level)


def _sba_rate(values: list[float], alpha: float = 0.1) -> float:
    size = None
    interval = None
    last = None

    for index, value in enumerate(values):
        if value <= 0:
            continue
        if last is None:
            size = value
            interval = float(index + 1)
        else:
            gap = index - last
            size = size + alpha * (value - size)
            interval = interval + alpha * (gap - interval)
        last = index

    if size is None or not interval:
        return sum(values) / len(values) if values else 0.0

    return max(0.0, (1 - alpha / 2) * size / interval)


def _tsb_rate(values: list[float], alpha_p: float = 0.1, alpha_z: float = 0.1) -> float:
    probability = 0.0
    size = 0.0
    started = False

    for value in values:
        occurred = 1.0 if value > 0 else 0.0
        if not started:
            if value <= 0:
                continue
            size = value
            probability = 1.0
            started = True
            continue
        probability = probability + alpha_p * (occurred - probability)
        if value > 0:
            size = size + alpha_z * (value - size)

    return max(0.0, probability * size) if started else 0.0


def _croston_rate(values: list[float]) -> float:
    """SBA, а если товар затих — TSB, чтобы нули в хвосте гасили спрос."""

    trailing = 0
    for value in reversed(values):
        if value > 0:
            break
        trailing += 1

    return _tsb_rate(values) if trailing >= WEEK else _sba_rate(values)


def _weekly_average(
    values: list[float],
    dates: list[date],
    future_dates: list[date],
    cap: float,
) -> list[float]:
    by_weekday: dict[int, list[float]] = defaultdict(list)
    window = min(84, len(values))

    for day, value in zip(dates[-window:], values[-window:]):
        by_weekday[day.weekday()].append(value)

    overall = sum(values[-min(28, len(values)) :]) / min(28, len(values) or 1)
    daily = []

    for day in future_dates:
        bucket = by_weekday[day.weekday()]
        level = sum(bucket) / len(bucket) if bucket else overall
        daily.append(max(0.0, min(level, cap)))

    return daily


def _naive_week(values: list[float], horizon: int, cap: float) -> list[float]:
    week = values[-min(WEEK, len(values)) :]
    if not week:
        return [0.0] * horizon
    return [max(0.0, min(week[index % len(week)], cap)) for index in range(horizon)]


def _naive_year(values: list[float], horizon: int, cap: float) -> list[float] | None:
    if len(values) < YEAR:
        return None

    daily = []
    for index in range(horizon):
        source = -YEAR + index
        if source >= len(values) or abs(source) > len(values):
            return None
        daily.append(max(0.0, min(values[source], cap)))

    return daily


def _pooled_weekly(
    values: list[float],
    dates: list[date],
    future_dates: list[date],
    store_dates: list[date] | None,
    store_values: list[float] | None,
    cap: float,
) -> list[float]:
    """Редкий SKU тянем к форме недели магазина, уровень — свой."""

    if (
        not store_dates
        or not store_values
        or len(store_dates) != len(store_values)
        or store_values is values
    ):
        return _weekly_average(values, dates, future_dates, cap)

    sku_by_weekday: dict[int, list[float]] = defaultdict(list)
    window = min(84, len(values))
    for day, value in zip(dates[-window:], values[-window:]):
        sku_by_weekday[day.weekday()].append(value)

    sku_mean = sum(values[-min(28, len(values)) :]) / min(28, len(values) or 1)
    store_by_day = dict(zip(store_dates, store_values))
    store_by_weekday: dict[int, list[float]] = defaultdict(list)

    for day in dates[-window:]:
        total = store_by_day.get(day)
        if total is not None:
            store_by_weekday[day.weekday()].append(total)

    store_all = [value for bucket in store_by_weekday.values() for value in bucket]
    store_mean = sum(store_all) / len(store_all) if store_all else 0.0
    daily = []
    prior_weight = 4

    for day in future_dates:
        weekday = day.weekday()
        sku_bucket = sku_by_weekday[weekday]
        sku_hat = sum(sku_bucket) / len(sku_bucket) if sku_bucket else sku_mean
        store_bucket = store_by_weekday[weekday]
        if store_mean > 0 and store_bucket:
            prior = sku_mean * (sum(store_bucket) / len(store_bucket) / store_mean)
        else:
            prior = sku_mean
        weight = len(sku_bucket) / (len(sku_bucket) + prior_weight)
        daily.append(max(0.0, min(weight * sku_hat + (1 - weight) * prior, cap)))

    return daily


def _local_forecast(
    name: str,
    values: list[float],
    dates: list[date],
    horizon: int,
    future_dates: list[date],
    store_dates: list[date] | None,
    store_values: list[float] | None,
    cap: float,
) -> list[float]:
    if name == 'weighted_average':
        daily = [min(_ses_level(values), cap)] * horizon
    elif name == 'weekly_average':
        daily = _weekly_average(values, dates, future_dates, cap)
    elif name == 'seasonal_naive_week':
        daily = _naive_week(values, horizon, cap)
    elif name == 'seasonal_naive_year':
        daily = _naive_year(values, horizon, cap) or _local_average(values, horizon, cap)
    elif name == 'croston_sba':
        daily = [min(_croston_rate(values), cap)] * horizon
    elif name == 'pooled_weekly':
        daily = _pooled_weekly(values, dates, future_dates, store_dates, store_values, cap)
    else:
        daily = _local_average(values, horizon, cap)

    if len(daily) < horizon:
        fill = daily[-1] if daily else 0.0
        daily = daily + [fill] * (horizon - len(daily))

    return [max(0.0, min(value, cap)) for value in daily[:horizon]]


def _eligible_local(
    values: list[float],
    allowed: set[str] | None = None,
    store_dates: list[date] | None = None,
    store_values: list[float] | None = None,
) -> list[str]:
    length = len(values)
    active = sum(value > 0 for value in values)
    zero_share = 1 - active / length if length else 1
    names = ['average']

    if length >= 14:
        names.extend(['weighted_average', 'weekly_average', 'seasonal_naive_week'])
    if length >= 14 and active >= 3 and zero_share >= SPARSE_ZERO_SHARE:
        names.append('croston_sba')
    if length >= YEAR and sum(values[-YEAR : -YEAR + WEEK]) > 0:
        names.append('seasonal_naive_year')
    if (
        store_dates
        and store_values
        and len(store_dates) == len(store_values)
        and store_values is not values
        and length >= 14
    ):
        names.append('pooled_weekly')

    if allowed is not None:
        names = [name for name in names if name in allowed]

    return names or ['average']


def _eligible_sf(values: list[float], allowed: set[str] | None = None) -> set[str]:
    length = len(values)
    active = sum(value > 0 for value in values)
    names: set[str] = set()

    if length >= 28 and active >= 14:
        names.update({'auto_ets', 'auto_theta'})
    if length >= 28 and active >= 6:
        names.add('holt')
    if length >= 56 and active >= 14:
        names.add('holt_winters_weekly')

    if allowed is not None:
        names &= allowed

    return names


def _eligible(values: list[float], allowed: set[str] | None = None) -> set[str]:
    return set(_eligible_local(values, allowed)) | _eligible_sf(values, allowed)


def _sf_allowed(allowed: set[str] | None, local_error: float | None) -> set[str] | None:
    """Какие модели StatsForecast можно гонять.

    На карточке (`allowed` пустой) — все подходящие. В списке и плане сначала
    только дешёвые; Holt/ETS подключаем, если локальная точность уже низкая,
    иначе в таблице «Низкая», а на «Авто» — «Средняя».
    """

    if allowed is None:
        return None
    if set(allowed) <= set(LOCAL_MODELS) and (
        local_error is None or local_error > FAIR_ERROR
    ):
        return None
    return allowed


def _error_bucket(values: list[float]) -> int:
    """Точность смотрим неделей: закупают пачку дней, а не каждый чек.

    Туалетная бумага может уйти 0 или 24 штуки за день — дневной WAPE тогда
    всегда около 100%, хотя за неделю спрос ровный. Редкие нули больше не
    условие: та же неделя нужна и плотному, но рваному ряду.
    """

    if len(values) < WEEK_BUCKET:
        return 1

    return WEEK_BUCKET


def _bucket_sums(values: list[float], size: int) -> list[float]:
    return [sum(values[index : index + size]) for index in range(0, len(values), size)]


def _zero_demand_error(estimated: list[float]) -> float:
    total = sum(max(0.0, value) for value in estimated)
    if total <= 0:
        return 0.0
    return min(1.0, total / max(1.0, len(estimated)))


def _select(
    values: list[float],
    horizon: int,
    model: str | None = None,
    *,
    dates: list[date] | None = None,
    future_dates: list[date] | None = None,
    store_dates: list[date] | None = None,
    store_values: list[float] | None = None,
    allowed: set[str] | None = None,
) -> tuple[str, list[float], float, float]:
    """Выбирает модель: явно запрошенную или лучшую на holdout."""

    return _select_many(
        {'_': values},
        horizon,
        model,
        allowed,
        dates_of={'_': dates} if dates is not None else None,
        future_dates=future_dates,
        store_dates=store_dates,
        store_values=store_values,
    )['_']


def _select_many(
    series: dict[str, list[float]],
    horizon: int,
    model: str | None = None,
    allowed: set[str] | None = None,
    dates_of: dict[str, list[date]] | None = None,
    future_dates: list[date] | None = None,
    store_dates: list[date] | None = None,
    store_values: list[float] | None = None,
) -> dict[str, tuple[str, list[float], float, float]]:
    """Отбирает модель по каждому ряду и строит дневной прогноз на горизонт.

    Сначала дешёвые локальные модели. Если точность низкая — в списке и плане
    тоже — к ним добавляются Holt/ETS. На карточке «Авто» они пробуются всегда:
    побеждает меньшая ошибка на хвосте.
    """

    dates_of = dates_of or {}
    fitted: dict[str, tuple[list[date], list[float]]] = {}

    for uid, values in series.items():
        if not values:
            continue
        fitted[str(uid)] = _fit_pair(dates_of.get(uid) or dates_of.get(str(uid)), values)

    results: dict[str, tuple[str, list[float], float, float]] = {}
    pending_sf: dict[str, list[float]] = {}
    sf_scope_of: dict[str, set[str] | None] = {}
    forced_sf = model in MODELS and model not in LOCAL_MODELS
    forced_local = model if model in LOCAL_MODELS else None

    for uid, (dates, values) in fitted.items():
        future = future_dates or _future_of(dates, horizon)

        if not forced_sf:
            local_allowed = {forced_local} if forced_local else allowed
            if local_allowed is not None:
                local_allowed = set(local_allowed) & set(LOCAL_MODELS)
            results[uid] = _select_local(
                values,
                dates,
                horizon,
                future,
                store_dates,
                store_values,
                allowed=local_allowed,
                forced=forced_local,
            )

        local = results.get(uid)
        scope = None if forced_sf else _sf_allowed(
            allowed,
            None if local is None else local[2],
        )
        if forced_sf or (model is None and _eligible_sf(values, scope)):
            pending_sf[uid] = values
            sf_scope_of[uid] = scope

    if not pending_sf:
        return results

    chosen: dict[str, tuple[str, float, float]] = {}

    if forced_sf:
        for uid, values in pending_sf.items():
            error, mae = _holdout_score(values, model)
            chosen[uid] = (model, error, mae)
    else:
        groups: dict[int, list[str]] = defaultdict(list)

        for uid, values in pending_sf.items():
            groups[_holdout_size(len(values))].append(uid)

        for holdout, uids in groups.items():
            train = {uid: pending_sf[uid][:-holdout] for uid in uids}
            actual = {uid: pending_sf[uid][-holdout:] for uid in uids}
            eligible_of = {
                uid: _eligible_sf(pending_sf[uid], sf_scope_of[uid])
                & _eligible_sf(train[uid], sf_scope_of[uid])
                for uid in uids
            }
            predicted: dict[str, dict[str, list[float]]] = {uid: {} for uid in uids}
            need: dict[str, list[str]] = defaultdict(list)

            for uid, names in eligible_of.items():
                for name in names:
                    need[name].append(uid)

            for name, group_uids in need.items():
                batch = _sf_batch({uid: train[uid] for uid in group_uids}, holdout, (name,))

                for uid in group_uids:
                    mean = batch.get(uid, {}).get(name)
                    if mean:
                        predicted[uid][name] = mean

            for uid in uids:
                best: tuple[float, float, str] | None = None
                bucket = _error_bucket(pending_sf[uid])

                for name in eligible_of[uid]:
                    scored = _score(
                        actual[uid],
                        predicted.get(uid, {}).get(name, []),
                        bucket=bucket,
                    )

                    if scored is None:
                        continue

                    candidate = (scored[0], scored[1], name)

                    if best is None or candidate < best:
                        best = candidate

                if best is not None:
                    chosen[uid] = (best[2], best[0], best[1])

    by_name: dict[str, list[str]] = defaultdict(list)

    for uid, (name, _, _) in chosen.items():
        by_name[name].append(uid)

    finals: dict[str, list[float]] = {}

    for name, uids in by_name.items():
        forecasted = _sf_batch({uid: pending_sf[uid] for uid in uids}, horizon, (name,))

        for uid in uids:
            finals[uid] = forecasted.get(uid, {}).get(name, [])

    for uid, values in pending_sf.items():
        if uid not in chosen:
            continue

        name, error, mae = chosen[uid]
        daily = finals.get(uid) or []

        if not daily:
            continue

        current = results.get(uid)
        if current is None or (error, mae) < (current[2], current[3]):
            results[uid] = (name, daily, error, mae)

    for uid, (dates, values) in fitted.items():
        if uid not in results:
            future = future_dates or _future_of(dates, horizon)
            results[uid] = _select_local(
                values,
                dates,
                horizon,
                future,
                store_dates,
                store_values,
            )

    return results


def _select_local(
    values: list[float],
    dates: list[date],
    horizon: int,
    future_dates: list[date],
    store_dates: list[date] | None = None,
    store_values: list[float] | None = None,
    allowed: set[str] | None = None,
    forced: str | None = None,
) -> tuple[str, list[float], float, float]:
    """Дешёвые модели без StatsForecast. На низкой точности пробуем все сразу."""

    cap = max(1.0, _percentile(values, 0.9)) * 4 if values else 1.0
    names = [forced] if forced else _eligible_local(values, allowed, store_dates, store_values)
    bucket = _error_bucket(values)

    if len(values) < 14:
        name = forced or 'average'
        daily = _local_forecast(
            name, values, dates, horizon, future_dates, store_dates, store_values, cap
        )
        in_sample = _local_forecast(
            name, values, dates, len(values), dates, store_dates, store_values, cap
        )
        scored = _score(values, in_sample, bucket=bucket)
        if scored is None:
            mae = _dispersion(values, daily[0] if daily else 0.0)
            return name, daily, _relative_error(mae, values), mae
        return name, daily, scored[0], scored[1]

    holdout = _holdout_size(len(values))
    train_values, actual = values[:-holdout], values[-holdout:]
    train_dates, actual_dates = dates[:-holdout], dates[-holdout:]
    best: tuple[float, float, str] | None = None

    for name in names:
        predicted = _local_forecast(
            name,
            train_values,
            train_dates,
            holdout,
            actual_dates,
            store_dates,
            store_values,
            cap,
        )
        scored = _score(actual, predicted, bucket=bucket)
        if scored is None:
            continue
        candidate = (scored[0], scored[1], name)
        if best is None or candidate < best:
            best = candidate

    if best is None:
        name = 'average'
        daily = _local_average(values, horizon, cap)
        mae = _dispersion(values, daily[0] if daily else 0.0)
        return name, daily, _relative_error(mae, values), mae

    name = best[2]
    daily = _local_forecast(
        name, values, dates, horizon, future_dates, store_dates, store_values, cap
    )

    # Хвост нулевой, а год назад в эти дни продавали — это сезон, не мёртвый товар.
    if sum(daily) <= 0 and name != 'seasonal_naive_year' and 'seasonal_naive_year' in names:
        yearly = _local_forecast(
            'seasonal_naive_year',
            values,
            dates,
            horizon,
            future_dates,
            store_dates,
            store_values,
            cap,
        )
        if sum(yearly) > 0:
            return 'seasonal_naive_year', yearly, best[0], best[1]

    return name, daily, best[0], best[1]


def _holdout_size(length: int) -> int:
    return min(28, max(7, length // 5))


def _holdout_score(values: list[float], name: str) -> tuple[float, float]:
    if len(values) < 14:
        prediction = _predict_models(values, 1, (name,)).get(name) or _local_average(values, 1)
        mae = _dispersion(values, prediction[0] if prediction else 0.0)
        return _relative_error(mae, values), mae

    holdout = _holdout_size(len(values))
    estimated = _predict_models(values[:-holdout], holdout, (name,)).get(name)
    scored = _score(values[-holdout:], estimated or [], bucket=_error_bucket(values))

    if scored is None:
        prediction = _local_average(values, 1)
        mae = _dispersion(values, prediction[0])
        return _relative_error(mae, values), mae

    return scored


def _score(
    actual: list[float],
    estimated: list[float],
    *,
    bucket: int = 1,
) -> tuple[float, float] | None:
    if len(estimated) != len(actual) or not actual:
        return None
    if not all(isfinite(value) for value in estimated):
        return None

    absolute = [abs(wanted - got) for wanted, got in zip(actual, estimated)]
    mae = sum(absolute) / len(absolute)

    if bucket > 1:
        actual = _bucket_sums(actual, bucket)
        estimated = _bucket_sums(estimated, bucket)
        absolute = [abs(wanted - got) for wanted, got in zip(actual, estimated)]

    denominator = sum(abs(value) for value in actual)
    wape = sum(absolute) / denominator if denominator > 0 else _zero_demand_error(estimated)
    return wape, mae


def _predict_models(
    values: list[float],
    horizon: int,
    names: tuple[str, ...],
) -> dict[str, list[float]]:
    local_names = tuple(name for name in names if name in LOCAL_MODELS)
    sf_names = tuple(name for name in names if name not in LOCAL_MODELS)
    out: dict[str, list[float]] = {}

    if sf_names:
        out.update(_sf_batch({'_': values}, horizon, sf_names).get('_', {}))

    if local_names:
        dates = _synthetic_dates(len(values))
        future = _future_of(dates, horizon)
        cap = max(1.0, _percentile(values, 0.9)) * 4
        for name in local_names:
            out[name] = _local_forecast(name, values, dates, horizon, future, None, None, cap)

    return out


def _sf_batch(
    series: dict[str, list[float]],
    horizon: int,
    names: tuple[str, ...],
) -> dict[str, dict[str, list[float]]]:
    """Пакетный прогноз StatsForecast: unique_id → модель → дневной ряд."""

    if not series or horizon <= 0 or not names:
        return {}

    if len(series) > SF_BATCH:
        merged: dict[str, dict[str, list[float]]] = {}
        items = list(series.items())

        for start in range(0, len(items), SF_BATCH):
            chunk = dict(items[start : start + SF_BATCH])
            merged.update(_sf_batch(chunk, horizon, names))

        return merged

    import warnings

    import pandas as pd
    from statsforecast import StatsForecast
    from statsforecast.models import HistoricAverage

    rows = []
    caps = {}

    for uid, values in series.items():
        uid = str(uid)
        caps[uid] = max(1.0, _percentile(values, 0.9)) * 4
        start = date(2000, 1, 1)

        for offset, value in enumerate(values):
            rows.append(
                {
                    'unique_id': uid,
                    'ds': start + timedelta(days=offset),
                    'y': float(value),
                }
            )

    engines = [_engine(name) for name in names]
    forecasted = None

    with warnings.catch_warnings():
        warnings.filterwarnings('ignore')
        try:
            forecasted = StatsForecast(
                models=engines,
                freq='D',
                n_jobs=1,
                verbose=False,
                fallback_model=HistoricAverage(),
            ).forecast(df=pd.DataFrame(rows), h=horizon)
        except Exception:
            forecasted = None

    if forecasted is None:
        return {
            str(uid): _sf_one(values, horizon, names, caps[str(uid)])
            for uid, values in series.items()
        }

    if 'unique_id' not in forecasted.columns:
        forecasted = forecasted.reset_index()

    forecasted['unique_id'] = forecasted['unique_id'].astype(str)
    result: dict[str, dict[str, list[float]]] = {str(uid): {} for uid in series}

    for uid, group in forecasted.groupby('unique_id', sort=False):
        cap = caps[str(uid)]

        for name in names:
            if name not in group.columns:
                continue

            mean = [float(value) for value in group[name].tolist()]

            if len(mean) != horizon or not all(isfinite(value) for value in mean):
                continue

            result[str(uid)][name] = [max(0.0, min(value, cap)) for value in mean]

    if 'average' in names:
        for uid, values in series.items():
            key = str(uid)

            if 'average' not in result[key]:
                result[key]['average'] = _local_average(values, horizon, caps[key])

    return result


def _sf_one(
    values: list[float],
    horizon: int,
    names: tuple[str, ...],
    cap: float,
) -> dict[str, list[float]]:
    """Один ряд, если пакетный вызов StatsForecast не собрался."""

    import warnings

    import numpy as np

    y = np.asarray(values, dtype=np.float64)
    out: dict[str, list[float]] = {}

    with warnings.catch_warnings():
        warnings.filterwarnings('ignore')

        for name in names:
            try:
                raw = _engine(name, len(values)).forecast(y=y, h=horizon)
                mean = [float(value) for value in raw['mean']]
            except Exception:
                continue

            if len(mean) != horizon or not all(isfinite(value) for value in mean):
                continue

            out[name] = [max(0.0, min(value, cap)) for value in mean]

    if 'average' in names and 'average' not in out:
        out['average'] = _local_average(values, horizon, cap)

    return out


def _engine(name: str, length: int = 28):
    """Экземпляр модели StatsForecast с нашим стабильным именем в `alias`."""

    from statsforecast.models import (
        AutoETS,
        AutoTheta,
        CrostonSBA,
        Holt,
        HoltWinters,
        SeasonalNaive,
        SimpleExponentialSmoothingOptimized,
        WindowAverage,
    )

    if name == 'average':
        return WindowAverage(window_size=min(28, max(1, length)), alias='average')
    if name == 'weighted_average':
        return SimpleExponentialSmoothingOptimized(alias='weighted_average')
    if name == 'holt':
        return Holt(alias='holt')
    if name == 'holt_winters_weekly':
        return HoltWinters(season_length=WEEK, alias='holt_winters_weekly')
    if name == 'auto_ets':
        return AutoETS(season_length=WEEK, alias='auto_ets')
    if name == 'auto_theta':
        return AutoTheta(season_length=WEEK, alias='auto_theta')
    if name == 'croston_sba':
        return CrostonSBA(alias='croston_sba')
    if name == 'seasonal_naive_year':
        return SeasonalNaive(season_length=YEAR, alias='seasonal_naive_year')

    raise KeyError(name)


def _with_holidays(
    forecast: list[float],
    dates: list[date],
    values: list[float],
    future_dates: list[date],
    calendar: dict[date, str],
    store_dates: list[date],
    store_values: list[float],
    store_cache: dict[tuple[str, int], tuple[float, int]],
) -> tuple[list[float], float]:
    if not calendar:
        return forecast, 1.0

    adjusted = []

    for value, future in zip(forecast, future_dates):
        event = _holiday_event(future, calendar)

        if event is None:
            adjusted.append(value)
            continue

        name, offset = event
        key = (_holiday_name(name), offset)
        product_factor, product_samples = _historical_factor(
            dates,
            values,
            calendar,
            key,
        )

        if key not in store_cache:
            store_cache[key] = _historical_factor(
                store_dates,
                store_values,
                calendar,
                key,
            )

        store_factor, store_samples = store_cache[key]

        if product_samples >= 2:
            weight = product_samples / (product_samples + 3)
            factor = product_factor * weight + store_factor * (1 - weight)
        elif store_samples:
            factor = store_factor
        else:
            factor = 1.0

        factor = min(MAX_HOLIDAY_FACTOR, max(MIN_HOLIDAY_FACTOR, factor))
        adjusted.append(value * factor)

    base_total = sum(forecast)
    factor = sum(adjusted) / base_total if base_total > 0 else 1.0
    return adjusted, factor


def _holiday_event(day: date, calendar: dict[date, str]) -> tuple[str, int] | None:
    # Покупательский всплеск чаще приходится на два дня до праздника; следующий
    # день оставляем, потому что длинные праздники могут сдвигать спрос туда.
    for offset in (0, -1, -2, 1):
        holiday_day = day - timedelta(days=offset)

        if holiday_day in calendar:
            return calendar[holiday_day], offset

    return None


def _historical_factor(
    dates: list[date],
    values: list[float],
    calendar: dict[date, str],
    key: tuple[str, int],
) -> tuple[float, int]:
    if not dates:
        return 1.0, 0

    by_day = dict(zip(dates, values))
    wanted_name, offset = key
    ratios = []

    for holiday_day, name in calendar.items():
        if _holiday_name(name) != wanted_name:
            continue

        target = holiday_day + timedelta(days=offset)

        if target not in by_day:
            continue

        neighbours = [
            by_day[candidate]
            for distance in range(-56, 57, 7)
            if distance
            and (candidate := target + timedelta(days=distance)) in by_day
            and _holiday_event(candidate, calendar) is None
        ]

        if not neighbours:
            continue

        baseline = median(neighbours)

        if baseline > 0:
            ratios.append(by_day[target] / baseline)

    return (median(ratios), len(ratios)) if ratios else (1.0, 0)


def _holiday_name(name: str) -> str:
    lowered = str(name).lower()

    for suffix in (' (observed)', ' (наблюдаемый)', ' (выходной)'):
        lowered = lowered.replace(suffix, '')

    return lowered.strip()


def _calendar(first: date, last: date) -> dict[date, str]:
    years = range(first.year, last.year + 1)
    return dict(holidays.country_holidays('KZ', years=years, observed=True))


def _dispersion(values: list[float], level: float) -> float:
    return sum(abs(value - level) for value in values) / len(values)


def _relative_error(mae: float, values: list[float]) -> float:
    average = sum(values) / len(values)
    return mae / average if average > 0 else mae


def _percentile(values: list[float], point: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * point)))
    return ordered[index]


def _amount(value: float) -> Decimal:
    return Decimal(str(max(0.0, value))).quantize(THREE)
