"""Товары магазина — то, что реально продавалось в UMAG.

Планировка больше не ходит за полной историей чеков: её заранее сворачиваем
в дневной спрос, а расчёт читает уже готовую копию.
"""

from datetime import timedelta
from math import ceil

from django.db.models import Max, Min
from django.db.models.functions import Lower
from django.utils import timezone

from umag import demand as store_demand
from umag.matching import unit_for
from umag.models import UmagProduct, UmagSale, UmagSalesSync, UmagSoldProduct

from . import forecast as demand_forecast

IDLE = 'idle'
DEFAULT_HORIZON = 14
DEFAULT_HISTORY_DAYS = 60
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
SORT_FIELDS = {
    'name': 'name',
    'barcode': 'barcode',
    'sold': 'sold',
    'last': 'last_sold',
}
# Поток выгрузки мог умереть, не сняв `syncing`. Месяц чеков качается
# дольше трёх минут — смотрим и heartbeat, и последние записи в копию.
STALE_AFTER = timedelta(minutes=10)


def snapshot(
    account,
    *,
    q: str = '',
    barcode: str = '',
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort: str = 'sold',
    order: str = 'desc',
    last_from=None,
    last_to=None,
    sold_from=None,
    sold_to=None,
) -> dict:
    """Страница товаров выбранного магазина и состояние выгрузки чеков."""

    organization = account.user.organization if account else None
    store_id = account.store_id if account else None

    if organization is None or not store_id:
        return _empty()

    state = UmagSalesSync.objects.filter(
        organization=organization,
        store_id=store_id,
    ).first()
    if state is not None:
        recover_stale_sync(state, organization, store_id)
        state.refresh_from_db()
    items, total, page = catalog(
        organization,
        store_id,
        q=q,
        barcode=barcode,
        page=page,
        page_size=page_size,
        sort=sort,
        order=order,
        last_from=last_from,
        last_to=last_to,
        sold_from=sold_from,
        sold_to=sold_to,
    )

    return {
        'status': state.status if state else IDLE,
        'synced_at': state.synced_at if state else None,
        'history_from': state.history_from if state else None,
        'error': state.error if state else '',
        'items_total': total,
        'page': page,
        'page_size': page_size,
        'items': items,
    }


def sync_is_stale(state) -> bool:
    """Выгрузка помечена как идущая, но поток давно не отзывается."""

    if state.status != UmagSalesSync.Status.SYNCING:
        return False

    latest = state.heartbeat_at
    written = (
        UmagSale.objects.filter(
            organization_id=state.organization_id,
            store_id=state.store_id,
        )
        .aggregate(last=Max('updated_at'))
        .get('last')
    )

    if written is not None and (latest is None or written > latest):
        latest = written

    if latest is None:
        return True

    return timezone.now() - latest > STALE_AFTER


def recover_stale_sync(state, organization, store_id) -> None:
    """Снимает вечный `syncing`, чтобы кнопка «Обновить» снова работала.

    Если чеки уже есть — сворачиваем их в спрос и считаем копию готовой до
    последней продажи. Следующий запуск догонит хвост, а не пойдёт с 2000 года.
    """

    if not sync_is_stale(state):
        return

    bounds = UmagSale.objects.filter(
        organization=organization,
        store_id=store_id,
    ).aggregate(first=Min('occurred_at'), last=Max('occurred_at'))

    if bounds['last'] is not None:
        store_demand.rebuild(organization, store_id, bounds['first'], bounds['last'])

    history_from = store_demand.history_start(organization, store_id)

    if history_from is None:
        state.status = UmagSalesSync.Status.FAILED
        state.error = 'Выгрузка прервалась. Нажмите «Обновить».'
        state.save(update_fields=('status', 'error'))
        return

    state.status = UmagSalesSync.Status.READY
    state.history_from = history_from
    state.synced_until = bounds['last'] or history_from
    state.synced_at = timezone.now()
    state.error = ''
    state.save(
        update_fields=('status', 'history_from', 'synced_until', 'synced_at', 'error'),
    )


def catalog(
    organization,
    store_id: int,
    *,
    q: str = '',
    barcode: str = '',
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    sort: str = 'sold',
    order: str = 'desc',
    last_from=None,
    last_to=None,
    sold_from=None,
    sold_to=None,
) -> tuple[list[dict], int, int]:
    """Страница уникальных штрихкодов: сколько продали и когда в последний раз."""

    rows = _grouped(organization, store_id)
    needle = q.strip()
    code = barcode.strip()

    if needle:
        rows = rows.filter(name__icontains=needle)

    if code:
        rows = rows.filter(barcode__icontains=code)

    if last_from:
        rows = rows.filter(last_sold__date__gte=last_from)

    if last_to:
        rows = rows.filter(last_sold__date__lte=last_to)

    if sold_from is not None:
        rows = rows.filter(sold__gte=sold_from)

    if sold_to is not None:
        rows = rows.filter(sold__lte=sold_to)

    field = SORT_FIELDS.get(sort, 'sold')
    descending = order != 'asc'
    if field == 'name':
        key = Lower('name')
        rows = rows.order_by(key.desc() if descending else key, 'barcode')
    else:
        rows = rows.order_by(f'-{field}' if descending else field, 'barcode')

    total = rows.count()
    page_size = min(max(page_size, 1), MAX_PAGE_SIZE)
    pages = max(1, ceil(total / page_size)) if total else 1
    page = min(max(page, 1), pages)
    offset = (page - 1) * page_size
    page_rows = list(rows[offset : offset + page_size])

    return _as_items(store_id, page_rows), total, page


def find_item(organization, store_id: int, barcode: str) -> dict | None:
    """Один товар из чеков — без сборки всего списка."""

    rows = list(_grouped(organization, store_id).filter(barcode=barcode)[:1])

    if not rows:
        return None

    return _as_items(store_id, rows)[0]


def sales_ready(account) -> bool:
    """Чеки уже выгружены — расчёту плана ходить в UMAG за историей незачем."""

    organization = account.user.organization if account else None

    if organization is None or not account.store_id:
        return False

    return UmagSalesSync.objects.filter(
        organization=organization,
        store_id=account.store_id,
        status=UmagSalesSync.Status.READY,
    ).exists()


def detail(
    account,
    barcode: str,
    *,
    horizon: int = DEFAULT_HORIZON,
    history_days: int = DEFAULT_HISTORY_DAYS,
    model: str | None = None,
    forecast: bool = True,
) -> dict | None:
    """Карточка товара: продажи, дневной ряд и прогноз на горизонт."""

    organization = account.user.organization if account else None
    store_id = account.store_id if account else None
    barcode = (barcode or '').strip()

    if organization is None or not store_id or not barcode:
        return None

    item = find_item(organization, store_id, barcode)

    if item is None:
        return None

    outlook = demand_forecast.for_barcode(
        organization,
        store_id,
        barcode,
        horizon,
        history_days=history_days,
        model=model,
        forecast=forecast,
    )

    item['supplier'] = _supplier(store_id, barcode)

    if outlook is None:
        return {
            **item,
            'horizon': horizon,
            'history_days': history_days,
            'history': [],
            'forecast': None,
        }

    prediction = outlook['forecast']

    if prediction is None:
        return {
            **item,
            'horizon': horizon,
            'history_days': history_days,
            'history': outlook['history'],
            'forecast': None,
        }

    return {
        **item,
        'horizon': horizon,
        'history_days': history_days,
        'history': outlook['history'],
        'forecast': {
            'model': prediction.model,
            'quantity': prediction.quantity,
            'per_day': prediction.per_day,
            'safety_stock': prediction.safety_stock,
            'holiday_factor': prediction.holiday_factor,
            'error': prediction.error,
            'observations': prediction.observations,
            'series': outlook['series'],
        },
    }


def _supplier(store_id: int, barcode: str) -> str:
    """У кого этот товар берут — из последней готовой планировки или закупа."""

    from .models import ApprovedPurchaseItem, PurchasePlan, PurchasePlanItem

    planned = (
        PurchasePlanItem.objects.filter(
            plan__store_id=store_id,
            plan__status=PurchasePlan.Status.READY,
            barcode=barcode,
        )
        .exclude(supplier='')
        .order_by('-plan__built_at', '-id')
        .values_list('supplier', flat=True)
        .first()
    )

    if planned:
        return planned

    bought = (
        ApprovedPurchaseItem.objects.filter(
            purchase__store_id=store_id,
            barcode=barcode,
        )
        .exclude(purchase__supplier='')
        .order_by('-purchase__approved_at', '-id')
        .values_list('purchase__supplier', flat=True)
        .first()
    )

    return bought or ''


def _empty() -> dict:
    return {
        'status': IDLE,
        'synced_at': None,
        'history_from': None,
        'error': '',
        'items_total': 0,
        'page': 1,
        'page_size': DEFAULT_PAGE_SIZE,
        'items': [],
    }


def _grouped(organization, store_id: int):
    """Проданные штрихкоды — уже свёрнутые, без повторного GROUP BY по чекам."""

    return UmagSoldProduct.objects.filter(
        organization=organization,
        store_id=store_id,
    ).exclude(barcode='')


def _as_items(store_id: int, rows: list) -> list[dict]:
    units = {
        barcode: unit_for(measure)
        for barcode, measure in UmagProduct.objects.filter(
            store_id=store_id,
            barcode__in=[row.barcode for row in rows],
        ).values_list('barcode', 'measure')
    }

    return [
        {
            'barcode': row.barcode,
            'name': (row.name or '').strip() or row.barcode,
            'measure': unit_for(row.measure) or units.get(row.barcode, ''),
            'sold': row.sold,
            'last_sold': row.last_sold,
        }
        for row in rows
    ]
