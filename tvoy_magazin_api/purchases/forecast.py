"""Прогноз спроса по дневным продажам.

Ряд считает StatsForecast: среднее, сглаживание, Хольт, Хольт–Винтерс, AutoETS,
AutoTheta, Кростон и годовой сезонный naïve. Какие алгоритмы доступны, зависит
от длины ряда, лучший выбирается по отложенному хвосту. Акционный всплеск не
должен стать нормой — скидку снимаем с ряда до отбора модели, на графике
оставляем факт. Пустую полку тоже снимаем: обвал продаж при открытом магазине —
это дефицит, а не спрос, иначе прогноз занизит заказ и дефицит продлится.
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
    'holt',
    'holt_winters_weekly',
    'auto_ets',
    'auto_theta',
    'croston_sba',
    'seasonal_naive_year',
)
# План считает тысячи рядов: StatsForecast на каждом — минуты. Берём среднее
# по недавнему окну; на карточке товара сложные модели остаются.
PLAN_MODELS = ('average',)
PLAN_FIT_DAYS = 90
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

    if not wanted:
        return {}

    store_sparse = _store_daily_totals(organization, store_id, store_start, history_end)
    calendar = _calendar(store_start, as_of + timedelta(days=horizon))
    store_dates, store_values = _series(store_sparse, store_start, history_end)
    future_dates = [as_of + timedelta(days=offset) for offset in range(horizon)]
    store_factor_cache: dict[tuple[str, int], tuple[float, int]] = {}
    prepared: dict[str, tuple[list[date], list[float]]] = {}

    for barcode, rows in wanted.items():
        first_day = max(min(rows), fit_start)
        dates, values = _series(rows, first_day, history_end)

        if not values:
            continue

        _, promo_values = _series(promos.get(barcode, {}), first_day, history_end)
        values = _clip_promos(dates, values, promo_values)
        values = _clip_stockouts(dates, values, store_dates, store_values)
        prepared[barcode] = (dates, values)

    selected = _select_many(
        {barcode: values for barcode, (_, values) in prepared.items()},
        horizon,
        allowed=set(models) if models else None,
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
    leftover = set(barcodes)
    daily: dict[str, dict[date, float]] = defaultdict(lambda: defaultdict(float))

    for years in (1, 2):
        if not leftover:
            break

        start = shift_years(as_of, years)
        end = start + timedelta(days=horizon - 1)
        for row in UmagDailyDemand.objects.filter(
            organization=organization,
            store_id=store_id,
            barcode__in=leftover,
            day__gte=start,
            day__lte=end,
        ).values('barcode', 'day', 'quantity'):
            day = _as_date(row['day'])
            if day is None:
                continue
            daily[row['barcode']][day] += max(0.0, float(row['quantity'] or 0))

        leftover -= set(daily)

    forecasts = {}

    for barcode, rows in daily.items():
        if not rows:
            continue

        start = min(rows)
        _, values = _series(rows, start, start + timedelta(days=horizon - 1))
        demand = sum(values)

        if demand <= 0:
            continue

        forecasts[barcode] = Forecast(
            model='seasonal_naive_year',
            quantity=_amount(demand),
            per_day=_amount(demand / horizon),
            safety_stock=ZERO,
            holiday_factor=Decimal('1.000'),
            error=ZERO,
            observations=len(values),
            daily=tuple(_amount(value) for value in values),
            daily_error=ZERO,
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
    model, base, error, daily_mae = _select(values, horizon, model)
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
) -> Forecast:
    """Чистая точка входа для тестов и повторного использования без БД."""

    if horizon <= 0:
        raise ValueError('Горизонт должен быть положительным')
    if not values:
        raise ValueError('История продаж пуста')

    clean = [max(0.0, float(value)) for value in values]
    dates = dates or [date(2000, 1, 1) + timedelta(days=index) for index in range(len(clean))]
    clean = _clip_promos(dates, clean, promo_values)
    clean = _clip_stockouts(dates, clean, store_dates or dates, store_values or clean)
    model, base, error, daily_mae = _select(clean, horizon, model)
    future_dates = future_dates or [
        dates[-1] + timedelta(days=index + 1) for index in range(horizon)
    ]
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


def _select(
    values: list[float],
    horizon: int,
    model: str | None = None,
) -> tuple[str, list[float], float, float]:
    """Выбирает модель: явно запрошенную или лучшую на holdout."""

    return _select_many({'_': values}, horizon, model)['_']


def _select_many(
    series: dict[str, list[float]],
    horizon: int,
    model: str | None = None,
    allowed: set[str] | None = None,
) -> dict[str, tuple[str, list[float], float, float]]:
    """Отбирает модель по каждому ряду и строит дневной прогноз на горизонт."""

    series = {str(uid): _fit_window(values) for uid, values in series.items() if values}
    results: dict[str, tuple[str, list[float], float, float]] = {}
    pending: dict[str, list[float]] = {}

    for uid, values in series.items():
        if model in MODELS:
            pending[uid] = values
        elif (allowed is not None and allowed <= {'average'}) or len(values) < 14:
            daily = _local_average(values, horizon)
            mae = _dispersion(values, daily[0] if daily else 0.0)
            results[uid] = ('average', daily, _relative_error(mae, values), mae)
        else:
            pending[uid] = values

    if not pending:
        return results

    chosen: dict[str, tuple[str, float, float]] = {}

    if model in MODELS:
        for uid, values in pending.items():
            error, mae = _holdout_score(values, model)
            chosen[uid] = (model, error, mae)
    else:
        groups: dict[int, list[str]] = defaultdict(list)

        for uid, values in pending.items():
            groups[_holdout_size(len(values))].append(uid)

        for holdout, uids in groups.items():
            train = {uid: pending[uid][:-holdout] for uid in uids}
            actual = {uid: pending[uid][-holdout:] for uid in uids}
            eligible_of = {
                uid: _eligible(pending[uid], allowed) & _eligible(train[uid], allowed)
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

                for name in eligible_of[uid]:
                    scored = _score(actual[uid], predicted.get(uid, {}).get(name, []))

                    if scored is None:
                        continue

                    candidate = (scored[0], scored[1], name)

                    if best is None or candidate < best:
                        best = candidate

                if best is None:
                    mae = _dispersion(pending[uid], _local_average(pending[uid], 1)[0])
                    chosen[uid] = ('average', _relative_error(mae, pending[uid]), mae)
                else:
                    chosen[uid] = (best[2], best[0], best[1])

    by_name: dict[str, list[str]] = defaultdict(list)

    for uid, (name, _, _) in chosen.items():
        by_name[name].append(uid)

    finals: dict[str, list[float]] = {}

    for name, uids in by_name.items():
        forecasted = _sf_batch({uid: pending[uid] for uid in uids}, horizon, (name,))

        for uid in uids:
            finals[uid] = forecasted.get(uid, {}).get(name, [])

    for uid, values in pending.items():
        name, error, mae = chosen[uid]
        daily = finals.get(uid) or _local_average(values, horizon)

        if not finals.get(uid):
            name = 'average'
            mae = _dispersion(values, daily[0] if daily else 0.0)
            error = _relative_error(mae, values)

        results[uid] = (name, daily, error, mae)

    return results


def _eligible(values: list[float], allowed: set[str] | None = None) -> set[str]:
    length = len(values)
    active = sum(value > 0 for value in values)
    zero_share = 1 - active / length if length else 1
    names = {'average'}

    if length >= 14:
        names.add('weighted_average')
    # AutoETS/Theta на коротком или почти нулевом ряде только тратят время:
    # побеждает среднее, а подбор параметров — секунды на товар.
    if length >= 28 and active >= 14:
        names.update({'auto_ets', 'auto_theta'})
    if length >= 28 and active >= 6:
        names.add('holt')
    if length >= 28 and active >= 3 and zero_share >= 0.4:
        names.add('croston_sba')
    if length >= 56 and active >= 14:
        names.add('holt_winters_weekly')
    if length >= YEAR and active >= 24:
        names.add('seasonal_naive_year')

    if allowed is not None:
        names &= allowed

    return names or {'average'}


def _holdout_size(length: int) -> int:
    return min(28, max(7, length // 5))


def _holdout_score(values: list[float], name: str) -> tuple[float, float]:
    if len(values) < 14:
        prediction = _predict_models(values, 1, (name,)).get(name) or _local_average(values, 1)
        mae = _dispersion(values, prediction[0] if prediction else 0.0)
        return _relative_error(mae, values), mae

    holdout = _holdout_size(len(values))
    estimated = _predict_models(values[:-holdout], holdout, (name,)).get(name)
    scored = _score(values[-holdout:], estimated or [])

    if scored is None:
        prediction = _local_average(values, 1)
        mae = _dispersion(values, prediction[0])
        return _relative_error(mae, values), mae

    return scored


def _score(actual: list[float], estimated: list[float]) -> tuple[float, float] | None:
    if len(estimated) != len(actual) or not actual:
        return None
    if not all(isfinite(value) for value in estimated):
        return None

    absolute = [abs(wanted - got) for wanted, got in zip(actual, estimated)]
    mae = sum(absolute) / len(absolute)
    denominator = sum(abs(value) for value in actual)
    wape = sum(absolute) / denominator if denominator > 0 else mae
    return wape, mae


def _predict_models(
    values: list[float],
    horizon: int,
    names: tuple[str, ...],
) -> dict[str, list[float]]:
    return _sf_batch({'_': values}, horizon, names).get('_', {})


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
