"""Сводка продаж магазина за период — график, дни недели и категории.

Считаем по уже свёрнутому дневному спросу: чеки для этого трогать незачем.
Выручку не показываем — в копии продаж цен нет, только количество.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Sum
from django.utils import timezone

from umag.models import UmagDailyDemand, UmagProduct, UmagSalesSync, UmagSoldProduct

from . import products

ZERO = Decimal('0')
THREE = Decimal('0.001')
DEFAULT_DAYS = 30
MIN_DAYS = 7
MAX_DAYS = 90
CATEGORY_LIMIT = 8


def snapshot(account, *, days: int = DEFAULT_DAYS) -> dict:
    """Продажи выбранного магазина за последние `days` дней, включая сегодня."""

    organization = account.user.organization if account else None
    store_id = account.store_id if account else None
    days = min(max(int(days), MIN_DAYS), MAX_DAYS)

    if organization is None or not store_id:
        return _empty(days)

    state = UmagSalesSync.objects.filter(
        organization=organization,
        store_id=store_id,
    ).first()
    if state is not None:
        products.recover_stale_sync(state, organization, store_id)
        state.refresh_from_db()

    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)

    return {
        'status': state.status if state else products.IDLE,
        'synced_at': state.synced_at if state else None,
        'history_from': state.history_from if state else None,
        'error': state.error if state else '',
        'has_sales': UmagSoldProduct.objects.filter(
            organization=organization,
            store_id=store_id,
        )
        .exclude(barcode='')
        .exists(),
        'days': days,
        'start': start,
        'end': today,
        **_period(organization, store_id, start, today, previous_start, previous_end),
    }


def _period(
    organization,
    store_id: int,
    start: date,
    end: date,
    previous_start: date,
    previous_end: date,
) -> dict:
    """Агрегаты текущего окна и сравнение с таким же предыдущим."""

    current = UmagDailyDemand.objects.filter(
        organization=organization,
        store_id=store_id,
        day__gte=start,
        day__lte=end,
    ).exclude(barcode='')
    previous_sold = (
        UmagDailyDemand.objects.filter(
            organization=organization,
            store_id=store_id,
            day__gte=previous_start,
            day__lte=previous_end,
        )
        .exclude(barcode='')
        .aggregate(sold=Sum('quantity'))
        .get('sold')
    )
    totals = current.aggregate(
        sold=Sum('quantity'),
        promo=Sum('promo_quantity'),
        sku_count=Count('barcode', distinct=True),
    )
    sold = _amount(totals['sold'])
    promo = _amount(totals['promo'])
    daily = _daily(current, start, end)
    by_barcode = list(current.values('barcode').annotate(sold=Sum('quantity')))

    return {
        'sold': sold,
        'sku_count': totals['sku_count'] or 0,
        'active_days': sum(1 for row in daily if row['sold'] > 0),
        'promo_share': _ratio(promo, sold),
        'trend': _trend(sold, previous_sold),
        'history': daily,
        'weekdays': _weekdays(daily),
        'categories': _categories(store_id, by_barcode),
    }


def _daily(query, start: date, end: date) -> list[dict]:
    """Каждый день окна, даже если продаж не было — иначе график рвётся."""

    totals = {
        row['day']: _amount(row['sold'])
        for row in query.values('day').annotate(sold=Sum('quantity'))
    }
    history = []
    day = start

    while day <= end:
        history.append({'date': day, 'sold': totals.get(day, ZERO)})
        day += timedelta(days=1)

    return history


def _weekdays(history: list[dict]) -> list[dict]:
    """Пн–вс: сумма количеств. Порядок фиксированный, чтобы столбцы не прыгали."""

    totals = [ZERO] * 7

    for row in history:
        totals[row['date'].weekday()] += row['sold']

    return [{'weekday': index, 'sold': total} for index, total in enumerate(totals)]


def _categories(store_id: int, rows: list[dict]) -> list[dict]:
    """Категории из номенклатуры. Без названия не показываем: это просто дырка в карточке."""

    if not rows:
        return []

    titles = dict(
        UmagProduct.objects.filter(
            store_id=store_id,
            barcode__in=[row['barcode'] for row in rows],
        )
        .exclude(category='')
        .values_list('barcode', 'category')
    )
    grouped: dict[str, dict] = {}

    for row in rows:
        name = titles.get(row['barcode'])
        if not name:
            continue
        bucket = grouped.setdefault(name, {'name': name, 'sold': ZERO, 'sku_count': 0})
        bucket['sold'] += _amount(row['sold'])
        bucket['sku_count'] += 1

    ranked = sorted(grouped.values(), key=lambda row: row['sold'], reverse=True)
    return ranked[:CATEGORY_LIMIT]


def _empty(days: int) -> dict:
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    history = _daily(UmagDailyDemand.objects.none(), start, today)

    return {
        'status': products.IDLE,
        'synced_at': None,
        'history_from': None,
        'error': '',
        'has_sales': False,
        'days': days,
        'start': start,
        'end': today,
        'sold': ZERO,
        'sku_count': 0,
        'active_days': 0,
        'promo_share': None,
        'trend': None,
        'history': history,
        'weekdays': _weekdays(history),
        'categories': [],
    }


def _amount(value) -> Decimal:
    if value is None:
        return ZERO
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(THREE)


def _ratio(part, whole) -> Decimal | None:
    whole = _amount(whole)
    if whole <= 0:
        return None
    return (_amount(part) / whole).quantize(THREE)


def _trend(current, previous) -> Decimal | None:
    previous = _amount(previous)
    if previous <= 0:
        return None
    return (_amount(current) / previous - 1).quantize(THREE)
