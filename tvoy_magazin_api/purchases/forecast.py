"""Прогноз спроса по дневным продажам.

Модели намеренно небольшие и без тяжёлого ML-стека: для каждого товара
доступные алгоритмы зависят от длины и разреженности ряда, а лучший выбирается
по отложенному хвосту. Это важнее одной сложной модели — у нового йогурта и у
хлеба с трёхлетней историей принципиально разное количество сигнала.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from math import isfinite, sqrt
from statistics import median
from zoneinfo import ZoneInfo

import holidays
from django.conf import settings
from django.db.models import Sum
from django.db.models.functions import Coalesce, TruncDate
from django.utils import timezone

from umag.models import UmagRefundItem, UmagSaleItem

ZERO = Decimal('0')
THREE = Decimal('0.001')
MAX_HOLIDAY_FACTOR = 3.0
MIN_HOLIDAY_FACTOR = 0.5
SERVICE_LEVEL_Z = 1.28  # около 90% при близких к нормальным остатках ошибки


@dataclass(frozen=True)
class Forecast:
    model: str
    quantity: Decimal
    per_day: Decimal
    safety_stock: Decimal
    holiday_factor: Decimal
    error: Decimal
    observations: int


def for_products(
    organization,
    store_id: int,
    barcodes: set[str] | None,
    horizon: int,
    *,
    as_of: date | None = None,
) -> dict[str, Forecast]:
    """Строит прогнозы для товаров магазина до начала сегодняшнего дня."""

    if (barcodes is not None and not barcodes) or horizon <= 0:
        return {}

    as_of = as_of or timezone.localdate()
    history_end = as_of - timedelta(days=1)
    sparse = _daily_quantities(organization, store_id)
    wanted = {
        barcode: rows
        for barcode, rows in sparse.items()
        if (barcodes is None or barcode in barcodes) and rows
    }

    if not wanted:
        return {}

    store_sparse: dict[date, float] = defaultdict(float)

    for rows in sparse.values():
        for day, quantity in rows.items():
            store_sparse[day] += quantity

    first_store_day = min(store_sparse)
    calendar = _calendar(first_store_day, as_of + timedelta(days=horizon))
    store_dates, store_values = _series(store_sparse, first_store_day, history_end)
    future_dates = [as_of + timedelta(days=offset) for offset in range(horizon)]
    store_factor_cache: dict[tuple[str, int], tuple[float, int]] = {}
    forecasts = {}

    for barcode, rows in wanted.items():
        first_day = min(rows)
        dates, values = _series(rows, first_day, history_end)

        if not values:
            continue

        model, base, error, daily_mae = _select(values, horizon)
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

        forecasts[barcode] = Forecast(
            model=model,
            quantity=_amount(demand),
            per_day=_amount(demand / horizon),
            safety_stock=_amount(max(0.0, safety)),
            holiday_factor=Decimal(str(holiday_factor)).quantize(THREE),
            error=Decimal(str(min(99_999.0, max(0.0, error)))).quantize(THREE),
            observations=len(values),
        )

    return forecasts


def predict(
    values: list[float],
    horizon: int,
    *,
    dates: list[date] | None = None,
    future_dates: list[date] | None = None,
    holiday_calendar: dict[date, str] | None = None,
    store_values: list[float] | None = None,
    store_dates: list[date] | None = None,
) -> Forecast:
    """Чистая точка входа для тестов и повторного использования без БД."""

    if horizon <= 0:
        raise ValueError('Горизонт должен быть положительным')
    if not values:
        raise ValueError('История продаж пуста')

    clean = [max(0.0, float(value)) for value in values]
    model, base, error, daily_mae = _select(clean, horizon)
    dates = dates or [date(2000, 1, 1) + timedelta(days=index) for index in range(len(clean))]
    future_dates = future_dates or [
        dates[-1] + timedelta(days=index + 1) for index in range(horizon)
    ]
    calendar = holiday_calendar or {}
    adjusted, holiday_factor = _with_holidays(
        base,
        dates,
        clean,
        future_dates,
        calendar,
        store_dates or dates,
        store_values or clean,
        {},
    )
    demand = max(0.0, sum(adjusted))
    safety = min(SERVICE_LEVEL_Z * daily_mae * sqrt(horizon), demand * 0.5)

    return Forecast(
        model=model,
        quantity=_amount(demand),
        per_day=_amount(demand / horizon),
        safety_stock=_amount(max(0.0, safety)),
        holiday_factor=Decimal(str(holiday_factor)).quantize(THREE),
        error=Decimal(str(min(99_999.0, max(0.0, error)))).quantize(THREE),
        observations=len(clean),
    )


def _daily_quantities(organization, store_id: int) -> dict[str, dict[date, float]]:
    """Net-demand: продажи минус возвраты, привязанные к дню исходного чека."""

    tz = ZoneInfo(settings.TIME_ZONE)
    result: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))
    sold = (
        UmagSaleItem.objects.filter(
            sale__organization=organization,
            sale__store_id=store_id,
        )
        .exclude(barcode='')
        .annotate(day=TruncDate('sale__occurred_at', tzinfo=tz))
        .values('barcode', 'day')
        .annotate(total=Sum('quantity'))
    )

    for row in sold:
        if row['day'] is not None:
            result[row['barcode']][row['day']] += float(row['total'] or 0)

    returned_at = Coalesce('refund__sale__occurred_at', 'refund__occurred_at')
    refunded = (
        UmagRefundItem.objects.filter(
            refund__organization=organization,
            refund__store_id=store_id,
        )
        .exclude(barcode='')
        .annotate(day=TruncDate(returned_at, tzinfo=tz))
        .values('barcode', 'day')
        .annotate(total=Sum('quantity'))
    )

    for row in refunded:
        if row['day'] is not None:
            result[row['barcode']][row['day']] -= float(row['total'] or 0)

    return {
        barcode: {day: max(0.0, quantity) for day, quantity in rows.items()}
        for barcode, rows in result.items()
    }


def _series(rows: dict[date, float], first: date, last: date) -> tuple[list[date], list[float]]:
    if first > last:
        return [], []

    days = (last - first).days + 1
    dates = [first + timedelta(days=offset) for offset in range(days)]
    return dates, [max(0.0, rows.get(day, 0.0)) for day in dates]


def _select(values: list[float], horizon: int) -> tuple[str, list[float], float, float]:
    """Выбирает допущенную объёмом данных модель на одном временном holdout."""

    names = _eligible(values)

    if len(values) < 14:
        prediction = _model('average', values, horizon)
        mae = _dispersion(values, prediction[0] if prediction else 0.0)
        return 'average', prediction, _relative_error(mae, values), mae

    holdout = min(28, max(7, len(values) // 5))
    train = values[:-holdout]
    actual = values[-holdout:]
    scored = []

    for name in names:
        if name not in _eligible(train):
            continue

        estimated = _model(name, train, holdout)

        if len(estimated) != holdout or not all(isfinite(value) for value in estimated):
            continue

        absolute = [abs(wanted - got) for wanted, got in zip(actual, estimated)]
        mae = sum(absolute) / len(absolute)
        denominator = sum(abs(value) for value in actual)
        wape = sum(absolute) / denominator if denominator > 0 else mae
        scored.append((wape, mae, name))

    if not scored:
        chosen = 'average'
        mae = _dispersion(values, _model(chosen, values, 1)[0])
        error = _relative_error(mae, values)
    else:
        error, mae, chosen = min(scored)

    return chosen, _model(chosen, values, horizon), error, mae


def _eligible(values: list[float]) -> set[str]:
    length = len(values)
    active = sum(value > 0 for value in values)
    zero_share = 1 - active / length if length else 1
    names = {'average'}

    if length >= 14:
        names.add('weighted_average')
    if length >= 28 and active >= 6:
        names.add('holt')
    if length >= 28 and active >= 3 and zero_share >= 0.4:
        names.add('croston_sba')
    if length >= 56 and active >= 14:
        names.add('holt_winters_weekly')
    if length >= 364 and active >= 24:
        names.add('seasonal_naive_year')

    return names


def _model(name: str, values: list[float], horizon: int) -> list[float]:
    if name == 'weighted_average':
        window = values[-min(56, len(values)) :]
        weights = range(1, len(window) + 1)
        level = sum(value * weight for value, weight in zip(window, weights)) / sum(weights)
        return [max(0.0, level)] * horizon

    if name == 'croston_sba':
        level = _croston(values)
        return [level] * horizon

    if name == 'holt':
        return _holt(values, horizon)

    if name == 'holt_winters_weekly':
        return _holt_winters(values, horizon)

    if name == 'seasonal_naive_year':
        period = 364  # 52 недели: и сезон года, и тот же день недели
        return [max(0.0, values[len(values) + step - period]) for step in range(horizon)]

    window = values[-min(28, len(values)) :]
    level = sum(window) / len(window)
    return [max(0.0, level)] * horizon


def _croston(values: list[float], alpha: float = 0.1) -> float:
    nonzero = [(index, value) for index, value in enumerate(values) if value > 0]

    if not nonzero:
        return 0.0

    first_index, demand = nonzero[0]
    interval = float(first_index + 1)
    previous = first_index

    for index, value in nonzero[1:]:
        demand += alpha * (value - demand)
        gap = index - previous
        interval += alpha * (gap - interval)
        previous = index

    return max(0.0, (1 - alpha / 2) * demand / max(interval, 1e-9))


def _holt(values: list[float], horizon: int) -> list[float]:
    alpha = 0.35
    beta = 0.1
    damping = 0.9
    level = values[0]
    trend = (values[min(6, len(values) - 1)] - values[0]) / min(7, len(values))

    for value in values[1:]:
        previous = level
        level = alpha * value + (1 - alpha) * (level + damping * trend)
        trend = beta * (level - previous) + (1 - beta) * damping * trend

    typical = max(1.0, _percentile(values, 0.9))
    return [
        max(0.0, min(level + trend * _damped_steps(damping, step + 1), typical * 4))
        for step in range(horizon)
    ]


def _holt_winters(values: list[float], horizon: int) -> list[float]:
    period = 7

    if len(values) < period * 2:
        return _holt(values, horizon)

    alpha = 0.35
    beta = 0.08
    gamma = 0.2
    damping = 0.9
    level = sum(values[:period]) / period
    next_level = sum(values[period : period * 2]) / period
    trend = (next_level - level) / period
    seasons = [value - level for value in values[:period]]

    for index, value in enumerate(values):
        season_index = index % period
        previous_level = level
        previous_season = seasons[season_index]
        level = alpha * (value - previous_season) + (1 - alpha) * (
            level + damping * trend
        )
        trend = beta * (level - previous_level) + (1 - beta) * damping * trend
        seasons[season_index] = gamma * (value - level) + (1 - gamma) * previous_season

    typical = max(1.0, _percentile(values, 0.9))
    return [
        max(
            0.0,
            min(
                level
                + trend * _damped_steps(damping, step + 1)
                + seasons[(len(values) + step) % period],
                typical * 4,
            ),
        )
        for step in range(horizon)
    ]


def _damped_steps(damping: float, steps: int) -> float:
    return sum(damping**power for power in range(1, steps + 1))


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
