"""Локальная копия чеков UMAG для прогноза спроса.

Товарный отчёт годится для среднего расхода, но в нём нет дней недели,
праздников и последовательности продаж. Поэтому забираем чеки и сразу
сворачиваем их в дневной спрос: сырые чеки держим только на неделю
перекрытия, чтобы поймать правку вчерашнего чека и поздний возврат.
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

from . import demand, matching
from .client import UmagClient, UmagError
from .models import (
    UmagRefund,
    UmagRefundItem,
    UmagSale,
    UmagSaleItem,
    UmagSalesSync,
)

logger = logging.getLogger(__name__)

SALES = 'opr/sale/list'
SALES_FALLBACK = 'opr/sale/list-without-products'
SALE = 'opr/sale/get/{id}'
REFUNDS = 'opr/refund/listing'
REFUND = 'opr/refund/get/{id}'

# Кабинет показывает по 50. Просим больше — меньше кругов по списку. Если
# сервер обрежет страницу, подстроимся под фактический размер и не примем
# короткий ответ за конец выдачи.
PAGE = 100
DETAIL_THREADS = 12
# Чеки пишем пачками: отдельная транзакция на каждый чек на первой выгрузке
# держит Postgres в тысячи раз дольше, чем сеть UMAG.
WRITE_BATCH = 500

# Исправленный вчера чек и поздний возврат должны попасть в уже готовую копию.
OVERLAP = timedelta(days=7)
# Один запрос за всю жизнь магазина заставляет UMAG считать огромный список и
# обрывается по read timeout. Месячные окна ограничивают работу одного запроса.
WINDOW = timedelta(days=31)
HISTORY_START = datetime(2000, 1, 1, tzinfo=UTC)
# Прогнозу хватает двух лет: годовая сезонность и тот же день недели. Первая
# выгрузка с 2000 года тянула пустые месяцы и старые чеки без пользы для модели.
HISTORY_DAYS = 365 * 2
ZERO = Decimal('0')
LINE_KEYS = ('saleProducts', 'refundProducts', 'items')

Progress = Callable[[], None]


@dataclass(frozen=True)
class SyncResult:
    sales: int
    refunds: int
    history_from: datetime | None
    synced_until: datetime


@dataclass(frozen=True)
class ParsedSale:
    external_id: str
    occurred_at: datetime
    items: tuple[dict, ...]


@dataclass(frozen=True)
class ParsedRefund:
    external_id: str
    sale_external_id: str
    sale_occurred_at: datetime | None
    occurred_at: datetime
    items: tuple[dict, ...]


SALE_FIELDS = ('occurred_at', 'updated_at')
REFUND_FIELDS = (
    'sale_external_id',
    'sale',
    'sale_occurred_at',
    'occurred_at',
    'updated_at',
)


def sync(
    account,
    *,
    full: bool = False,
    progress: Progress | None = None,
) -> SyncResult:
    """Обновляет все продажи выбранного магазина.

    Первый вызов берёт два года: этого хватает прогнозу. Повторный захватывает
    семь уже известных дней: UMAG разрешает править чек и оформлять возврат
    позже самой продажи. `--full` читает историю с начала календаря. После
    каждого месяца чеки старше недели выкидываем — в базе остаётся дневной спрос.
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
    since = _history_from(full, state.synced_until, until)

    state.status = UmagSalesSync.Status.SYNCING
    state.error = ''
    state.heartbeat_at = timezone.now()
    state.save(update_fields=('status', 'error', 'heartbeat_at'))

    client = UmagClient(account, account.store_id)
    keep_after = until - OVERLAP
    sales_count = 0
    refunds_count = 0

    try:
        with ThreadPoolExecutor(max_workers=DETAIL_THREADS) as pool:
            for start, finish in _windows(since, until):
                _touch(state.pk)
                sales_count += _sync_sales(
                    client,
                    organization,
                    account.store_id,
                    start,
                    finish,
                    progress,
                    state.pk,
                    pool,
                )
                refunds_count += _sync_refunds(
                    client,
                    organization,
                    account.store_id,
                    start,
                    finish,
                    progress,
                    state.pk,
                    pool,
                )
                demand.rebuild(organization, account.store_id, start, finish)
                demand.trim(organization, account.store_id, keep_after)
                _touch(state.pk)
    except Exception as error:
        UmagSalesSync.objects.filter(pk=state.pk).update(
            status=UmagSalesSync.Status.FAILED,
            error=str(error)[:1000],
        )
        raise

    history_from = demand.history_start(organization, account.store_id)
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


def _touch(state_id: int) -> None:
    UmagSalesSync.objects.filter(pk=state_id).update(heartbeat_at=timezone.now())


def _history_from(full: bool, synced_until: datetime | None, until: datetime) -> datetime:
    """Откуда читать чеки: полная история, первые два года или хвост с перекрытием."""

    if synced_until is not None and not full:
        return max(HISTORY_START, synced_until - OVERLAP)

    if full:
        return HISTORY_START

    return max(HISTORY_START, until - timedelta(days=HISTORY_DAYS))


def _sync_sales(client, organization, store_id, since, until, progress, state_id, pool) -> int:
    buffer: list[ParsedSale] = []

    def save(row, detail) -> None:
        buffer.append(_parse_sale(row, detail))
        if len(buffer) >= WRITE_BATCH:
            _flush_sales(organization, store_id, buffer)
            buffer.clear()

    saved = _walk(
        client,
        SALES,
        'sales',
        'count',
        since,
        until,
        save,
        lambda external_id: client.get(SALE.format(id=external_id)),
        progress,
        pool,
        state_id=state_id,
        fallback=SALES_FALLBACK,
    )

    if buffer:
        _flush_sales(organization, store_id, buffer)

    return saved


def _sync_refunds(client, organization, store_id, since, until, progress, state_id, pool) -> int:
    buffer: list[ParsedRefund] = []

    def save(row, detail) -> None:
        buffer.append(_parse_refund(row, detail))
        if len(buffer) >= WRITE_BATCH:
            _flush_refunds(organization, store_id, buffer)
            buffer.clear()

    saved = _walk(
        client,
        REFUNDS,
        'refunds',
        'totalCount',
        since,
        until,
        save,
        lambda external_id: client.get(REFUND.format(id=external_id)),
        progress,
        pool,
        state_id=state_id,
        deleted='false',
    )

    if buffer:
        _flush_refunds(organization, store_id, buffer)

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
    pool,
    *,
    state_id: int,
    fallback: str | None = None,
    **filters,
) -> int:
    first = 0
    saved = 0
    seen: set[str] = set()
    observed_page = None
    switched = False

    while True:
        _touch(state_id)
        try:
            body = client.get(
                path,
                fromTime=_millis(since),
                toTime=_millis(until),
                first=first,
                pageSize=PAGE,
                **filters,
            )
        except UmagError as error:
            if not (
                fallback
                and not switched
                and first == 0
                and error.status in (404, 405)
            ):
                raise

            path = fallback
            switched = True
            body = client.get(
                path,
                fromTime=_millis(since),
                toTime=_millis(until),
                first=first,
                pageSize=PAGE,
                **filters,
            )

        if (
            fallback
            and not switched
            and first == 0
            and not _is_list_page(body, rows_key)
        ):
            path = fallback
            switched = True
            continue

        rows = _rows(body, rows_key)

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

        for (row, _), response in zip(fresh, _details(fresh, detail, pool)):
            save(row, response)
            saved += 1

        if progress:
            progress()

        _touch(state_id)

        total = _integer(body.get(count_key)) if isinstance(body, dict) else None
        got = len(rows)
        first += got

        if not rows or (total is not None and first >= total):
            break

        if observed_page is None:
            observed_page = got
            continue

        if got < observed_page:
            break

    return saved


def _details(fresh: list, detail, pool) -> list:
    """Карточка чека из списка, если позиции уже там — иначе отдельный GET."""

    if not fresh:
        return []

    missing = [
        index for index, (row, _) in enumerate(fresh) if not _has_lines(row)
    ]

    if not missing:
        return [row for row, _ in fresh]

    fetched = list(pool.map(lambda index: detail(fresh[index][1]), missing))
    bodies = [row for row, _ in fresh]

    for index, body in zip(missing, fetched):
        bodies[index] = body

    return bodies


def _has_lines(row: dict) -> bool:
    """В списке уже есть товарные строки — второй запрос за чеком не нужен."""

    nested = row.get('sale') if isinstance(row.get('sale'), dict) else None

    for key in LINE_KEYS:
        if isinstance(row.get(key), list) or (
            isinstance(nested, dict) and isinstance(nested.get(key), list)
        ):
            return True

    products = row.get('products')

    if not (isinstance(products, list) and products and isinstance(products[0], dict)):
        return False

    return 'barcode' in products[0] or 'quantity' in products[0] or 'fullName' in products[0]


def _rows(body, key: str) -> list:
    if isinstance(body, list):
        return body

    if isinstance(body, dict):
        rows = body.get(key)
        return rows if isinstance(rows, list) else []

    return []


def _is_list_page(body, key: str) -> bool:
    if isinstance(body, list):
        return True

    if isinstance(body, dict):
        return key in body or 'count' in body or 'totalCount' in body or 'data' in body

    return False


def _parse_sale(summary: dict, body) -> ParsedSale:
    detail = body if isinstance(body, dict) else {}
    header = detail.get('sale') if isinstance(detail.get('sale'), dict) else {}
    data = {**summary, **header}
    external_id = _external_id(data)
    occurred_at = _moment(data)

    if not external_id or occurred_at is None:
        raise RuntimeError('UMAG вернул продажу без id или времени')

    products = {
        _text(product.get('barcode'), 64): product
        for product in detail.get('products') or []
        if isinstance(product, dict) and product.get('barcode') is not None
    }
    items = []

    for position, row in enumerate(
        detail.get('saleProducts') or detail.get('items') or [],
        start=1,
    ):
        if not isinstance(row, dict):
            continue

        barcode = _text(row.get('barcode'), 64)
        product = products.get(barcode, {})
        quantity = _decimal(row.get('quantity'))
        items.append(
            {
                'position': position,
                'barcode': barcode,
                'name': _text(
                    row.get('name')
                    or row.get('fullName')
                    or product.get('name')
                    or product.get('fullName'),
                    255,
                ),
                'measure': _measure(
                    product.get('measure') if row.get('measure') is None else row['measure']
                ),
                'quantity': _quantity(quantity),
                'on_promo': _on_promo(row.get('price'), row.get('priceBefore')),
            }
        )

    return ParsedSale(
        external_id=external_id,
        occurred_at=occurred_at,
        items=tuple(items),
    )


def _parse_refund(summary: dict, body) -> ParsedRefund:
    detail = body if isinstance(body, dict) else {}
    header = detail.get('refund') if isinstance(detail.get('refund'), dict) else {}
    data = {**summary, **header}
    external_id = _external_id(data)
    occurred_at = _moment(data)

    if not external_id or occurred_at is None:
        raise RuntimeError('UMAG вернул возврат без id или времени')

    items = []

    for position, row in enumerate(
        detail.get('refundProducts') or detail.get('products') or detail.get('items') or [],
        start=1,
    ):
        if not isinstance(row, dict):
            continue

        quantity = _decimal(row.get('quantity'))
        items.append(
            {
                'position': position,
                'barcode': _text(row.get('barcode'), 64),
                'quantity': _quantity(quantity),
            }
        )

    return ParsedRefund(
        external_id=external_id,
        sale_external_id=_text(data.get('saleId'), 64),
        sale_occurred_at=_moment(
            {
                'time': data.get('saleTime') or data.get('saleDate') or data.get('saleCreatedAt'),
            }
        ),
        occurred_at=occurred_at,
        items=tuple(items),
    )


def _latest_by_id(rows: list) -> list:
    """В одной пачке тот же чек может встретиться дважды — оставляем последний."""

    unique = {}

    for row in rows:
        unique[row.external_id] = row

    return list(unique.values())


@transaction.atomic
def _flush_sales(organization, store_id: int, parsed: list[ParsedSale]) -> None:
    rows = _latest_by_id(parsed)

    if not rows:
        return

    now = timezone.now()
    existing = {
        sale.external_id: sale
        for sale in UmagSale.objects.filter(
            organization=organization,
            store_id=store_id,
            external_id__in=[row.external_id for row in rows],
        )
    }
    created = []
    updated = []

    for row in rows:
        sale = existing.get(row.external_id)

        if sale is None:
            sale = UmagSale(
                organization=organization,
                store_id=store_id,
                external_id=row.external_id,
                occurred_at=row.occurred_at,
            )
            created.append(sale)
            existing[row.external_id] = sale
            continue

        sale.occurred_at = row.occurred_at
        sale.updated_at = now
        updated.append(sale)

    if created:
        UmagSale.objects.bulk_create(created, batch_size=WRITE_BATCH)
    if updated:
        UmagSale.objects.bulk_update(updated, SALE_FIELDS, batch_size=WRITE_BATCH)

    UmagSaleItem.objects.filter(sale_id__in=[sale.pk for sale in existing.values()]).delete()
    items = [
        UmagSaleItem(sale=existing[row.external_id], **item)
        for row in rows
        for item in row.items
    ]

    if items:
        UmagSaleItem.objects.bulk_create(items, batch_size=WRITE_BATCH)


@transaction.atomic
def _flush_refunds(organization, store_id: int, parsed: list[ParsedRefund]) -> None:
    rows = _latest_by_id(parsed)

    if not rows:
        return

    now = timezone.now()
    sales = {
        sale.external_id: sale
        for sale in UmagSale.objects.filter(
            organization=organization,
            store_id=store_id,
            external_id__in={row.sale_external_id for row in rows if row.sale_external_id},
        )
    }
    existing = {
        refund.external_id: refund
        for refund in UmagRefund.objects.filter(
            organization=organization,
            store_id=store_id,
            external_id__in=[row.external_id for row in rows],
        )
    }
    created = []
    updated = []

    for row in rows:
        refund = existing.get(row.external_id)
        sale = sales.get(row.sale_external_id)

        if refund is None:
            refund = UmagRefund(
                organization=organization,
                store_id=store_id,
                external_id=row.external_id,
                sale_external_id=row.sale_external_id,
                sale=sale,
                sale_occurred_at=sale.occurred_at if sale else row.sale_occurred_at,
                occurred_at=row.occurred_at,
            )
            created.append(refund)
            existing[row.external_id] = refund
            continue

        refund.sale_external_id = row.sale_external_id
        refund.sale = sale
        refund.sale_occurred_at = sale.occurred_at if sale else row.sale_occurred_at
        refund.occurred_at = row.occurred_at
        refund.updated_at = now
        updated.append(refund)

    if created:
        UmagRefund.objects.bulk_create(created, batch_size=WRITE_BATCH)
    if updated:
        UmagRefund.objects.bulk_update(updated, REFUND_FIELDS, batch_size=WRITE_BATCH)

    UmagRefundItem.objects.filter(refund_id__in=[refund.pk for refund in existing.values()]).delete()
    items = [
        UmagRefundItem(refund=existing[row.external_id], **item)
        for row in rows
        for item in row.items
    ]

    if items:
        UmagRefundItem.objects.bulk_create(items, batch_size=WRITE_BATCH)


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


def _on_promo(price, before) -> bool:
    """Скидка в чеке: продали дешевле, чем цена до акции."""

    if price is None or price == '' or before is None or before == '':
        return False

    actual = _decimal(price)
    regular = _decimal(before)
    return regular > 0 and actual < regular


def _integer(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
