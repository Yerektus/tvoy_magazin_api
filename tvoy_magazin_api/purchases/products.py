"""Товары магазина — то, что реально продавалось в UMAG.

Планировка больше не ходит за полной историей чеков: её заранее кладём сюда
синхронизацией, а расчёт читает уже готовую копию.
"""

from decimal import Decimal

from django.db.models import Max, Sum

from umag.models import UmagRefundItem, UmagSaleItem, UmagSalesSync

ZERO = Decimal('0')
IDLE = 'idle'


def snapshot(account) -> dict:
    """Список товаров выбранного магазина и состояние выгрузки чеков."""

    organization = account.user.organization if account else None
    store_id = account.store_id if account else None

    if organization is None or not store_id:
        return _empty()

    state = UmagSalesSync.objects.filter(
        organization=organization,
        store_id=store_id,
    ).first()
    items = catalog(organization, store_id)

    return {
        'status': state.status if state else IDLE,
        'synced_at': state.synced_at if state else None,
        'history_from': state.history_from if state else None,
        'error': state.error if state else '',
        'items_total': len(items),
        'items': items,
    }


def catalog(organization, store_id: int) -> list[dict]:
    """Уникальные штрихкоды из чеков: сколько продали и когда в последний раз."""

    sold = (
        UmagSaleItem.objects.filter(
            sale__organization=organization,
            sale__store_id=store_id,
        )
        .exclude(barcode='')
        .values('barcode')
        .annotate(
            sold=Sum('quantity'),
            last_sold=Max('sale__occurred_at'),
            name=Max('name'),
            measure=Max('measure'),
        )
    )
    refunded = {
        row['barcode']: row['qty'] or ZERO
        for row in UmagRefundItem.objects.filter(
            refund__organization=organization,
            refund__store_id=store_id,
        )
        .exclude(barcode='')
        .values('barcode')
        .annotate(qty=Sum('quantity'))
    }
    items = []

    for row in sold:
        net = (row['sold'] or ZERO) - refunded.get(row['barcode'], ZERO)
        items.append(
            {
                'barcode': row['barcode'],
                'name': (row['name'] or '').strip() or row['barcode'],
                'measure': row['measure'] or '',
                'sold': max(net, ZERO),
                'last_sold': row['last_sold'],
            }
        )

    items.sort(key=lambda item: (-item['sold'], item['name'].casefold()))
    return items


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


def _empty() -> dict:
    return {
        'status': IDLE,
        'synced_at': None,
        'history_from': None,
        'error': '',
        'items_total': 0,
        'items': [],
    }
