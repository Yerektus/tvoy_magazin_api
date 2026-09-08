"""Локальная копия чеков UMAG для прогноза спроса.

Товарный отчёт годится для среднего расхода, но в нём нет дней недели,
праздников и последовательности продаж. Поэтому один раз забираем все чеки и
возвраты, затем перед расчётом дочитываем только перекрывающийся хвост.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import matching
from .client import UmagClient
from .models import (
    UmagRefund,
    UmagRefundItem,
    UmagSale,
    UmagSaleItem,
    UmagSalesSync,
)

logger = logging.getLogger(__name__)

SALES = 'opr/sale/list-without-products'
SALE = 'opr/sale/get/{id}'
REFUNDS = 'opr/refund/listing'
REFUND = 'opr/refund/get/{id}'

# Кабинет сам показывает по 50 строк. Больший размер закрытый API может
# проигнорировать, поэтому безопаснее листать так же, как официальный клиент.
PAGE = 50
DETAIL_THREADS = 6

# Исправленный вчера чек и поздний возврат должны попасть в уже готовую копию.
OVERLAP = timedelta(days=7)
# Один запрос за всю жизнь магазина заставляет UMAG считать огромный список и
# обрывается по read timeout. Месячные окна ограничивают работу одного запроса.
WINDOW = timedelta(days=31)
HISTORY_START = datetime(2000, 1, 1, tzinfo=UTC)
ZERO = Decimal('0')

Progress = Callable[[], None]


@dataclass(frozen=True)
class SyncResult:
    sales: int
    refunds: int
    history_from: datetime | None
    synced_until: datetime


def sync(
    account,
    *,
    full: bool = False,
    progress: Progress | None = None,
) -> SyncResult:
    """Обновляет все продажи выбранного магазина.

    Первый вызов идёт от начала поддерживаемого календаря. Повторный захватывает
    семь уже известных дней: UMAG разрешает править чек и оформлять возврат
    позже самой продажи.
    """

    if not account.ready:
        raise ValueError('В UMAG не выбран магазин')

    organization = account.user.organization

    if organization is None:
        raise ValueError('У пользователя нет организации')

    state, _ = UmagSalesSync.objects.get_or_create(
        organization=organization,
        store_id=account.store_id,
    )
    until = timezone.now()
    since = (
        HISTORY_START
        if full or state.synced_until is None
        else max(HISTORY_START, state.synced_until - OVERLAP)
    )

    state.status = UmagSalesSync.Status.SYNCING
    state.error = ''
    state.save(update_fields=('status', 'error'))

    client = UmagClient(account, account.store_id)

    try:
        sales_count = _sync_sales(client, organization, account.store_id, since, until, progress)
        refunds_count = _sync_refunds(
            client,
            organization,
            account.store_id,
            since,
            until,
            progress,
        )
    except Exception as error:
        UmagSalesSync.objects.filter(pk=state.pk).update(
            status=UmagSalesSync.Status.FAILED,
            error=str(error)[:1000],
        )
        raise

    history_from = (
        UmagSale.objects.filter(organization=organization, store_id=account.store_id)
        .order_by('occurred_at')
        .values_list('occurred_at', flat=True)
        .first()
    )
    state.status = UmagSalesSync.Status.READY
    state.history_from = history_from
    state.synced_until = until
    state.synced_at = timezone.now()
    state.error = ''
    state.save(
        update_fields=(
            'status',
            'history_from',
            'synced_until',
            'synced_at',
            'error',
        )
    )

    return SyncResult(sales_count, refunds_count, history_from, until)


def _sync_sales(client, organization, store_id, since, until, progress) -> int:
    return _walk_windows(
        since,
        until,
        lambda start, finish: _walk(
            client,
            SALES,
            'sales',
            'count',
            start,
            finish,
            lambda row, detail: _save_sale(organization, store_id, row, detail),
            lambda external_id: client.get(SALE.format(id=external_id)),
            progress,
        ),
    )


def _sync_refunds(client, organization, store_id, since, until, progress) -> int:
    return _walk_windows(
        since,
        until,
        lambda start, finish: _walk(
            client,
            REFUNDS,
            'refunds',
            'totalCount',
            start,
            finish,
            lambda row, detail: _save_refund(organization, store_id, row, detail),
            lambda external_id: client.get(REFUND.format(id=external_id)),
            progress,
            deleted='false',
        ),
    )


def _walk_windows(since: datetime, until: datetime, walk) -> int:
    saved = 0

    for start, finish in _windows(since, until):
        saved += walk(start, finish)

    return saved


def _windows(since: datetime, until: datetime):
    start = since

    while start < until:
        finish = min(start + WINDOW, until)
        yield start, finish
        # Границы UMAG включительные; следующий миллисекундный тик не загрузит
        # чек ровно на стыке второй раз.
        start = finish + timedelta(milliseconds=1)


def _walk(
    client,
    path: str,
    rows_key: str,
    count_key: str,
    since: datetime,
    until: datetime,
    save,
    detail,
    progress: Progress | None,
    **filters,
) -> int:
    first = 0
    saved = 0
    seen: set[str] = set()

    while True:
        body = client.get(
            path,
            fromTime=_millis(since),
            toTime=_millis(until),
            first=first,
            pageSize=PAGE,
            **filters,
        )
        rows = body if isinstance(body, list) else (body or {}).get(rows_key) or []

        # Повтор той же страницы означает, что кабинет проигнорировал `first`.
        # Бесконечно ходить в закрытый API в таком случае нельзя.
        batch = [
            (row, _external_id(row))
            for row in rows
            if isinstance(row, dict) and _external_id(row)
        ]
        fresh = [(row, external_id) for row, external_id in batch if external_id not in seen]

        if rows and not fresh:
            raise RuntimeError(f'UMAG не применил пагинацию для {path}')

        seen.update(external_id for _, external_id in fresh)

        with ThreadPoolExecutor(max_workers=DETAIL_THREADS) as pool:
            details = list(pool.map(lambda item: detail(item[1]), fresh))

        for (row, _), response in zip(fresh, details):
            save(row, response)
            saved += 1

        if progress:
            progress()

        total = _integer((body or {}).get(count_key)) if isinstance(body, dict) else None
        first += len(rows)

        if not rows or len(rows) < PAGE or (total is not None and first >= total):
            break

    return saved


@transaction.atomic
def _save_sale(organization, store_id: int, summary: dict, body) -> None:
    detail = body if isinstance(body, dict) else {}
    header = detail.get('sale') if isinstance(detail.get('sale'), dict) else {}
    data = {**summary, **header}
    external_id = _external_id(data)
    occurred_at = _moment(data)

    if not external_id or occurred_at is None:
        raise RuntimeError('UMAG вернул продажу без id или времени')

    sale, _ = UmagSale.objects.update_or_create(
        organization=organization,
        store_id=store_id,
        external_id=external_id,
        defaults={
            'occurred_at': occurred_at,
            'receipt_no': _text(data.get('receiptNo') or data.get('number'), 64),
            'pos_id': _text(data.get('posId'), 64),
            'amount': _money(data.get('amount') or data.get('totalAmount')),
            'comment': _text(data.get('comment') or data.get('note'), 2000),
            'is_ofd': _boolean(data.get('isOfd')),
        },
    )

    products = {
        _text(product.get('barcode'), 64): product
        for product in detail.get('products') or []
        if isinstance(product, dict) and product.get('barcode') is not None
    }
    rows = detail.get('saleProducts') or detail.get('items') or []
    items = []

    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue

        barcode = _text(row.get('barcode'), 64)
        product = products.get(barcode, {})
        quantity = _decimal(row.get('quantity'))
        price = _nullable_money(row.get('price'))
        total = _nullable_money(row.get('amount') or row.get('total'))

        if total is None and price is not None:
            total = (quantity * price).quantize(Decimal('0.01'))

        items.append(
            UmagSaleItem(
                sale=sale,
                position=position,
                barcode=barcode,
                name=_text(
                    row.get('name')
                    or row.get('fullName')
                    or product.get('name')
                    or product.get('fullName'),
                    255,
                ),
                measure=_measure(
                    product.get('measure') if row.get('measure') is None else row['measure']
                ),
                quantity=_quantity(quantity),
                price=price,
                price_before=_nullable_money(row.get('priceBefore')),
                total=total,
            )
        )

    sale.items.all().delete()
    UmagSaleItem.objects.bulk_create(items, batch_size=200)


@transaction.atomic
def _save_refund(organization, store_id: int, summary: dict, body) -> None:
    detail = body if isinstance(body, dict) else {}
    header = detail.get('refund') if isinstance(detail.get('refund'), dict) else {}
    data = {**summary, **header}
    external_id = _external_id(data)
    occurred_at = _moment(data)

    if not external_id or occurred_at is None:
        raise RuntimeError('UMAG вернул возврат без id или времени')

    sale_external_id = _text(data.get('saleId'), 64)
    sale = (
        UmagSale.objects.filter(
            organization=organization,
            store_id=store_id,
            external_id=sale_external_id,
        ).first()
        if sale_external_id
        else None
    )
    refund, _ = UmagRefund.objects.update_or_create(
        organization=organization,
        store_id=store_id,
        external_id=external_id,
        defaults={
            'sale_external_id': sale_external_id,
            'sale': sale,
            'occurred_at': occurred_at,
            'amount': _money(data.get('amount') or data.get('totalAmount')),
            'paid_amount': _money(data.get('payedAmount') or data.get('paidAmount')),
            'note': _text(data.get('note') or data.get('comment'), 2000),
        },
    )

    rows = detail.get('refundProducts') or detail.get('products') or detail.get('items') or []
    items = []

    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue

        quantity = _decimal(row.get('quantity'))
        price = _nullable_money(row.get('price'))
        total = _nullable_money(row.get('amount') or row.get('total'))

        if total is None and price is not None:
            total = (quantity * price).quantize(Decimal('0.01'))

        items.append(
            UmagRefundItem(
                refund=refund,
                position=position,
                barcode=_text(row.get('barcode'), 64),
                name=_text(row.get('name') or row.get('fullName'), 255),
                measure=_measure(row.get('measure')),
                quantity=_quantity(quantity),
                price=price,
                total=total,
            )
        )

    refund.items.all().delete()
    UmagRefundItem.objects.bulk_create(items, batch_size=200)


def _external_id(row: dict) -> str:
    return _text(row.get('id') or row.get('saleId') or row.get('refundId'), 64)


def _moment(row: dict) -> datetime | None:
    value = (
        row.get('time')
        or row.get('date')
        or row.get('createdAt')
        or row.get('saleDate')
        or row.get('refundDate')
    )

    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)

    if isinstance(value, str):
        parsed = parse_datetime(value)

        if parsed is not None:
            return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)

        try:
            number = float(value)
        except ValueError:
            return None

        seconds = number / 1000 if number > 10_000_000_000 else number
        return datetime.fromtimestamp(seconds, tz=UTC)

    return None


def _millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _text(value, limit: int) -> str:
    return str(value or '').strip()[:limit]


def _measure(value) -> str:
    # Ноль — штуки, его нельзя скормить `_text`: `0 or ''` стирает единицу.
    return matching.unit_for(value)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value)) if value is not None else ZERO
    except (InvalidOperation, ValueError):
        return ZERO


def _quantity(value) -> Decimal:
    return _decimal(value).quantize(Decimal('0.001'))


def _money(value) -> Decimal:
    return _decimal(value).quantize(Decimal('0.01'))


def _nullable_money(value) -> Decimal | None:
    return None if value is None or value == '' else _money(value)


def _integer(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _boolean(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    return None
