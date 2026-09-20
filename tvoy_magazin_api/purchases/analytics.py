"""Сводка продаж магазина за период — график, дни недели, часы и категории.

Количество по дням считаем по уже свёрнутому дневному спросу. Часы — из
чеков, которые ещё лежат в копии: дальше недели их сворачивают. Деньги и
число чеков — из товарного отчёта и списка продаж UMAG: в копии цен нет.
Выручку по дням спрашиваем тем же отчётом на каждую дату.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count, Sum
from django.utils import timezone

from umag.client import UmagClient, UmagError
from umag.models import (
    UmagDailyDemand,
    UmagProduct,
    UmagRefund,
    UmagSaleItem,
    UmagSalesSync,
    UmagSoldProduct,
)

from . import products

logger = logging.getLogger(__name__)

ZERO = Decimal('0')
THREE = Decimal('0.001')
MONEY = Decimal('0.01')
CATEGORY_LIMIT = 12
REPORT = 'report/list-product-report'
CATEGORY_REPORT = 'report/list-category-report'
SALES = 'opr/sale/list'
TURNOVER_TTL = 120
REVENUE_THREADS = 6
UNTITLED_CATEGORY = 'Без категории'
_UNSET = object()


def snapshot(account, *, start: date, end: date) -> dict:
    """Продажи выбранного магазина с `start` по `end`, включая границы."""

    organization = account.user.organization if account else None
    store_id = account.store_id if account else None
    days = (end - start).days + 1

    if organization is None or not store_id:
        return _empty(start, end)

    state = UmagSalesSync.objects.filter(
        organization=organization,
        store_id=store_id,
    ).first()
    if state is not None:
        products.recover_stale_sync(state, organization, store_id)
        state.refresh_from_db()

    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    money = _turnover(account, start, end)
    previous_money = _turnover(account, previous_start, previous_end)

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
        'end': end,
        **money,
        **_money_trends(money, previous_money),
        **_period(
            organization,
            store_id,
            start,
            end,
            previous_start,
            previous_end,
            _daily_revenue(account, start, end),
            account,
        ),
    }


def _period(
    organization,
    store_id: int,
    start: date,
    end: date,
    previous_start: date,
    previous_end: date,
    revenue_by_day: dict | None = None,
    account=None,
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
    daily = _daily(current, start, end, revenue_by_day)
    by_barcode = list(current.values('barcode').annotate(sold=Sum('quantity')))

    return {
        'sold': sold,
        'sku_count': totals['sku_count'] or 0,
        'active_days': sum(1 for row in daily if row['sold'] > 0),
        'promo_share': _ratio(promo, sold),
        'trend': _trend(sold, previous_sold),
        'history': daily,
        'weekdays': _weekdays(daily),
        'hours': _hours(organization, store_id, start, end),
        'categories': _categories(account, store_id, by_barcode, start, end),
    }


def _daily(query, start: date, end: date, revenue_by_day: dict | None = None) -> list[dict]:
    """Каждый день окна, даже если продаж не было — иначе график рвётся."""

    totals = {
        row['day']: _amount(row['sold'])
        for row in query.values('day').annotate(sold=Sum('quantity'))
    }
    money = revenue_by_day or {}
    history = []
    day = start

    while day <= end:
        history.append(
            {
                'date': day,
                'sold': totals.get(day, ZERO),
                'revenue': money.get(day),
            }
        )
        day += timedelta(days=1)

    return history


def _weekdays(history: list[dict]) -> list[dict]:
    """Пн–вс: сумма количеств. Порядок фиксированный, чтобы столбцы не прыгали."""

    totals = [ZERO] * 7

    for row in history:
        totals[row['date'].weekday()] += row['sold']

    return [{'weekday': index, 'sold': total} for index, total in enumerate(totals)]


def _hours(organization, store_id: int, start: date, end: date) -> list[dict]:
    """0–23: сумма количеств по местному часу чека.

    Дневной спрос часа не знает. Берём сырые чеки за выбранные даты: старше
    окна перекрытия их уже нет, и тогда столбцы будут только по свежим дням.
    """

    tz = ZoneInfo(settings.TIME_ZONE)
    begin = datetime.combine(start, time.min, tzinfo=tz)
    finish = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz) - timedelta(
        milliseconds=1
    )
    totals = [ZERO] * 24
    items = (
        UmagSaleItem.objects.filter(
            sale__organization=organization,
            sale__store_id=store_id,
            sale__occurred_at__gte=begin,
            sale__occurred_at__lte=finish,
        )
        .exclude(barcode='')
        .select_related('sale')
        .iterator()
    )

    for item in items:
        totals[_local_hour(item.sale.occurred_at)] += item.quantity

    refunds = (
        UmagRefund.objects.filter(organization=organization, store_id=store_id)
        .select_related('sale')
        .prefetch_related('items')
    )

    for refund in refunds:
        moment = refund.sale_occurred_at or (
            refund.sale.occurred_at if refund.sale_id else None
        )
        if moment is None:
            moment = refund.occurred_at
        local = _as_local(moment)
        if local.date() < start or local.date() > end:
            continue
        for item in refund.items.all():
            if item.barcode:
                totals[local.hour] -= item.quantity

    return [
        {'hour': index, 'sold': max(_amount(total), ZERO)}
        for index, total in enumerate(totals)
    ]


def _as_local(moment: datetime):
    tz = ZoneInfo(settings.TIME_ZONE)
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment.astimezone(tz)


def _local_hour(moment: datetime) -> int:
    return _as_local(moment).hour


def _categories(account, store_id: int, rows: list[dict], start: date, end: date) -> list[dict]:
    """Сначала отчёт кабинета за период — в копии номенклатуры категории часто пустые."""

    if account is not None:
        try:
            live = _fetch_category_sales(account, start, end)
        except UmagError as error:
            logger.warning('UMAG не отдал продажи по категориям: %s', error)
            live = []
        if live:
            return live

    return _categories_from_catalog(store_id, rows)


def _fetch_category_sales(account, start: date, end: date) -> list[dict]:
    key = f'analytics-categories:{account.store_id}:{start.isoformat()}:{end.isoformat()}'
    cached = cache.get(key)
    if cached is not None:
        return cached

    if account is None or not account.ready:
        return []

    client = UmagClient(account, account.store_id)
    from_time, to_time = _range_millis(start, end)
    report = client.get(
        CATEGORY_REPORT,
        fromTime=from_time,
        toTime=to_time,
        first=0,
        pageSize=100,
    )
    ranked = _parse_category_report(report)
    cache.set(key, ranked, TURNOVER_TTL)
    return ranked


def _parse_category_report(report) -> list[dict]:
    """Верхний уровень отчёта кабинета. «Незаданные» показываем как «Без категории»."""

    rows = report.get('data') if isinstance(report, dict) else report
    if not isinstance(rows, list):
        return []

    grouped: list[dict] = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = (row.get('categoryName') or row.get('name') or '').strip()
        if not raw:
            continue
        name = UNTITLED_CATEGORY if raw.casefold() == 'незаданные' else raw
        sold = _amount(row.get('saleQuantity')) - _amount(row.get('refundQuantity'))
        if sold <= 0:
            continue
        children = row.get('children') if isinstance(row.get('children'), list) else []
        grouped.append({'name': name, 'sold': sold, 'sku_count': len(children)})

    grouped.sort(key=lambda row: row['sold'], reverse=True)
    return grouped[:CATEGORY_LIMIT]


def _categories_from_catalog(store_id: int, rows: list[dict]) -> list[dict]:
    """Категории из номенклатуры. Без названия не показываем: это просто дырка в карточке."""

    if not rows:
        return []

    titles = {}
    for barcode, category, subcategory in UmagProduct.objects.filter(
        store_id=store_id,
        barcode__in=[row['barcode'] for row in rows],
    ).values_list('barcode', 'category', 'subcategory'):
        titles[barcode] = (category or subcategory or '').strip()

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


def _empty(start: date, end: date) -> dict:
    history = _daily(UmagDailyDemand.objects.none(), start, end)

    return {
        'status': products.IDLE,
        'synced_at': None,
        'history_from': None,
        'error': '',
        'has_sales': False,
        'days': (end - start).days + 1,
        'start': start,
        'end': end,
        'revenue': None,
        'profit': None,
        'visitors': None,
        'average_check': None,
        'revenue_trend': None,
        'profit_trend': None,
        'visitors_trend': None,
        'average_check_trend': None,
        'sold': ZERO,
        'sku_count': 0,
        'active_days': 0,
        'promo_share': None,
        'trend': None,
        'history': history,
        'weekdays': _weekdays(history),
        'hours': [{'hour': index, 'sold': ZERO} for index in range(24)],
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
    if current is None or previous is None:
        return None
    previous = _amount(previous)
    if previous <= 0:
        return None
    return (_amount(current) / previous - 1).quantize(THREE)


def _money_trends(current: dict, previous: dict) -> dict:
    """Насколько итоги отличаются от такого же окна перед выбранным."""

    return {
        'revenue_trend': _trend(current.get('revenue'), previous.get('revenue')),
        'profit_trend': _trend(current.get('profit'), previous.get('profit')),
        'visitors_trend': _trend(current.get('visitors'), previous.get('visitors')),
        'average_check_trend': _trend(
            current.get('average_check'),
            previous.get('average_check'),
        ),
    }


def _turnover(account, start: date, end: date) -> dict:
    """Выручка, прибыль, чеки и средний чек за период — из кабинета UMAG."""

    empty = {'revenue': None, 'profit': None, 'visitors': None, 'average_check': None}

    if account is None or not account.ready:
        return empty

    key = f'analytics-turnover:{account.store_id}:{start.isoformat()}:{end.isoformat()}'
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        totals = _fetch_turnover(account, start, end)
    except UmagError as error:
        logger.warning('UMAG не отдал итоги продаж: %s', error)
        cache.set(key, empty, 30)
        return empty

    cache.set(key, totals, TURNOVER_TTL)
    return totals


def _daily_revenue(account, start: date, end: date) -> dict[date, Decimal | None]:
    """Выручка каждого дня окна. Без кабинета — пустые точки, график не врёт количеством."""

    days: list[date] = []
    day = start

    while day <= end:
        days.append(day)
        day += timedelta(days=1)

    if account is None or not account.ready:
        return {item: None for item in days}

    with ThreadPoolExecutor(max_workers=REVENUE_THREADS) as pool:
        values = list(pool.map(lambda item: _day_revenue(account, item), days))

    return dict(zip(days, values))


def _day_revenue(account, day: date) -> Decimal | None:
    key = f'analytics-day-revenue:{account.store_id}:{day.isoformat()}'
    cached = cache.get(key, _UNSET)

    if cached is not _UNSET:
        return cached

    try:
        amount = _report_revenue(account, day, day)
    except UmagError as error:
        logger.warning('UMAG не отдал выручку за %s: %s', day.isoformat(), error)
        return None

    cache.set(key, amount, TURNOVER_TTL)
    return amount


def _report_revenue(account, start: date, end: date) -> Decimal:
    client = UmagClient(account, account.store_id)
    from_time, to_time = _range_millis(start, end)
    report = client.get(REPORT, fromTime=from_time, toTime=to_time, first=0, pageSize=1) or {}
    sums = report.get('sum') if isinstance(report.get('sum'), dict) else {}
    return _money(sums.get('saleSellingAmount')) - _money(sums.get('refundSellingAmount'))


def _fetch_turnover(account, start: date, end: date) -> dict:
    client = UmagClient(account, account.store_id)
    from_time, to_time = _range_millis(start, end)
    report = client.get(REPORT, fromTime=from_time, toTime=to_time, first=0, pageSize=1) or {}
    sales = client.get(SALES, fromTime=from_time, toTime=to_time, first=0, pageSize=1) or {}
    sums = report.get('sum') if isinstance(report.get('sum'), dict) else {}
    revenue = _money(sums.get('saleSellingAmount')) - _money(sums.get('refundSellingAmount'))
    cost = _money(sums.get('saleArrivalAmount')) - _money(sums.get('refundArrivalAmount'))
    profit = _money(sums.get('marginAmount'))

    if profit == 0 and (revenue or cost):
        profit = revenue - cost

    visitors = _count(sales.get('count'), sales.get('totalCount'))
    average = (revenue / visitors).quantize(MONEY) if visitors else None

    return {
        'revenue': revenue,
        'profit': profit,
        'visitors': visitors,
        'average_check': average,
    }


def _range_millis(start: date, end: date) -> tuple[int, int]:
    tz = ZoneInfo(settings.TIME_ZONE)
    begin = datetime.combine(start, time.min, tzinfo=tz)
    finish = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz) - timedelta(milliseconds=1)
    return int(begin.timestamp() * 1000), int(finish.timestamp() * 1000)


def _money(value) -> Decimal:
    if value is None or value == '':
        return ZERO

    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ZERO

    return amount.quantize(MONEY)


def _count(*values) -> int:
    for value in values:
        if value is None or value == '':
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0
