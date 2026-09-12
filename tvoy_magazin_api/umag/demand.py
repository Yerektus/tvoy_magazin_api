"""Дневной спрос из чеков: компактная копия, по которой считают прогноз и список.

Сырые чеки держим только на окно перекрытия. После каждой порции выгрузки
сворачиваем дни в `UmagDailyDemand` и выкидываем старое. Поздний возврат на
уже свёрнутый день вычитаем один раз.
"""

from collections import defaultdict
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Max, Min, Sum
from django.utils import timezone

from .models import (
    UmagDailyDemand,
    UmagRefund,
    UmagSale,
    UmagSaleItem,
    UmagSoldProduct,
)

ZERO = Decimal('0')
THREE = Decimal('0.001')
WRITE_BATCH = 500


def rebuild(organization, store_id: int, start: datetime, finish: datetime) -> None:
    """Пересчитывает спрос за [start, finish] из чеков, которые ещё в базе."""

    start_day = _local_day(start)
    finish_day = _local_day(finish)

    with transaction.atomic():
        net, promo, seen = _net_from_receipts(organization, store_id, start, finish)
        UmagDailyDemand.objects.filter(
            organization=organization,
            store_id=store_id,
            day__gte=start_day,
            day__lte=finish_day,
        ).delete()
        UmagDailyDemand.objects.bulk_create(
            [
                UmagDailyDemand(
                    organization=organization,
                    store_id=store_id,
                    barcode=barcode,
                    day=day,
                    quantity=quantity.quantize(THREE),
                    promo_quantity=min(promo.get((barcode, day), ZERO), quantity).quantize(THREE),
                )
                for (barcode, day), quantity in net.items()
                if barcode and quantity > 0
            ],
            batch_size=WRITE_BATCH,
        )
        folded = _fold_late_refunds(organization, store_id, start_day, finish_day)
        _refresh_products(organization, store_id, seen, folded)


def rebuild_store(organization, store_id: int) -> None:
    """Сворачивает все лежащие чеки магазина. Для тестов и догонки оборванной выгрузки."""

    bounds = UmagSale.objects.filter(
        organization=organization,
        store_id=store_id,
    ).aggregate(first=Min('occurred_at'), last=Max('occurred_at'))

    if bounds['first'] is None or bounds['last'] is None:
        return

    rebuild(organization, store_id, bounds['first'], bounds['last'])


def trim(organization, store_id: int, keep_after: datetime) -> None:
    """Удаляет чеки и возвраты старше окна перекрытия — спрос уже в агрегатах."""

    UmagSale.objects.filter(
        organization=organization,
        store_id=store_id,
        occurred_at__lt=keep_after,
    ).delete()
    UmagRefund.objects.filter(
        organization=organization,
        store_id=store_id,
        occurred_at__lt=keep_after,
    ).delete()


def history_start(organization, store_id: int) -> datetime | None:
    """С какой даты есть свёрнутая история. Пусто — продаж ещё не было."""

    day = (
        UmagDailyDemand.objects.filter(organization=organization, store_id=store_id)
        .order_by('day')
        .values_list('day', flat=True)
        .first()
    )

    if day is None:
        return None

    return datetime.combine(day, time.min, tzinfo=ZoneInfo(settings.TIME_ZONE))


def _net_from_receipts(organization, store_id: int, start: datetime, finish: datetime):
    """Продажи минус возвраты по дням исходного чека. `seen` — штрихкоды из чеков."""

    net: dict[tuple[str, object], Decimal] = defaultdict(lambda: ZERO)
    promo: dict[tuple[str, object], Decimal] = defaultdict(lambda: ZERO)
    seen: dict[str, dict] = {}
    items = (
        UmagSaleItem.objects.filter(
            sale__organization=organization,
            sale__store_id=store_id,
            sale__occurred_at__gte=start,
            sale__occurred_at__lte=finish,
        )
        .exclude(barcode='')
        .select_related('sale')
        .iterator()
    )

    for item in items:
        day = _local_day(item.sale.occurred_at)
        net[item.barcode, day] += item.quantity
        if item.on_promo:
            promo[item.barcode, day] += item.quantity
        row = seen.setdefault(item.barcode, {'last_sold': None, 'name': '', 'measure': ''})
        if row['last_sold'] is None or item.sale.occurred_at > row['last_sold']:
            row['last_sold'] = item.sale.occurred_at
            if item.name:
                row['name'] = item.name
            if item.measure:
                row['measure'] = item.measure

    refunds = UmagRefund.objects.filter(
        organization=organization,
        store_id=store_id,
    ).select_related('sale').prefetch_related('items')

    accounted = []

    for refund in refunds:
        day = _demand_day(refund)
        if day is None or day < _local_day(start) or day > _local_day(finish):
            continue

        for item in refund.items.all():
            if item.barcode:
                net[item.barcode, day] -= item.quantity

        if not refund.folded:
            refund.folded = True
            accounted.append(refund)

    if accounted:
        UmagRefund.objects.bulk_update(accounted, ('folded',), batch_size=WRITE_BATCH)

    return net, promo, seen


def _fold_late_refunds(organization, store_id: int, start_day, finish_day) -> set[str]:
    """Возврат на день, чеков которого уже нет: вычитаем из агрегата один раз."""

    affected: set[str] = set()
    refunds = (
        UmagRefund.objects.filter(
            organization=organization,
            store_id=store_id,
            folded=False,
        )
        .select_related('sale')
        .prefetch_related('items')
    )

    for refund in refunds:
        day = _demand_day(refund)
        if day is None or start_day <= day <= finish_day:
            continue

        for item in refund.items.all():
            if not item.barcode:
                continue
            _subtract(organization, store_id, item.barcode, day, item.quantity)
            affected.add(item.barcode)

        refund.folded = True
        refund.save(update_fields=('folded',))

    return affected


def _subtract(organization, store_id: int, barcode: str, day, quantity: Decimal) -> None:
    row = UmagDailyDemand.objects.filter(
        organization=organization,
        store_id=store_id,
        barcode=barcode,
        day=day,
    ).first()

    if row is None:
        return

    row.quantity = (row.quantity - quantity).quantize(THREE)

    if row.quantity <= 0:
        row.delete()
        return

    if row.promo_quantity > row.quantity:
        row.promo_quantity = row.quantity
        row.save(update_fields=('quantity', 'promo_quantity'))
        return

    row.save(update_fields=('quantity',))


def _refresh_products(
    organization,
    store_id: int,
    seen: dict,
    extra: set[str] | None = None,
) -> None:
    """Обновляет сумму и последнюю продажу у товаров, которых коснулось окно."""

    meta_by_barcode = seen
    barcodes = set(seen) | (extra or set())

    if not barcodes:
        return

    totals = {
        row['barcode']: row
        for row in UmagDailyDemand.objects.filter(
            organization=organization,
            store_id=store_id,
            barcode__in=barcodes,
        )
        .values('barcode')
        .annotate(sold=Sum('quantity'), last_day=Max('day'))
    }
    existing = {
        product.barcode: product
        for product in UmagSoldProduct.objects.filter(
            organization=organization,
            store_id=store_id,
            barcode__in=barcodes,
        )
    }
    created = []
    updated = []

    for barcode in barcodes:
        total = totals.get(barcode, {})
        sold = (total.get('sold') or ZERO).quantize(THREE)
        last_day = total.get('last_day')
        meta = meta_by_barcode.get(barcode, {})
        product = existing.get(barcode)
        last_sold = meta.get('last_sold')
        name = (meta.get('name') or '').strip()
        measure = (meta.get('measure') or '').strip()

        if last_sold is None and last_day is not None:
            last_sold = datetime.combine(last_day, time.min, tzinfo=ZoneInfo(settings.TIME_ZONE))

        if product is None:
            created.append(
                UmagSoldProduct(
                    organization=organization,
                    store_id=store_id,
                    barcode=barcode,
                    name=name,
                    measure=measure,
                    sold=sold,
                    last_sold=last_sold,
                )
            )
            continue

        if last_sold is None or (product.last_sold and product.last_sold > last_sold):
            last_sold = product.last_sold
        if not name:
            name = product.name
        if not measure:
            measure = product.measure

        product.sold = sold
        product.last_sold = last_sold
        product.name = name
        product.measure = measure
        updated.append(product)

    if created:
        UmagSoldProduct.objects.bulk_create(created, batch_size=WRITE_BATCH)
    if updated:
        UmagSoldProduct.objects.bulk_update(
            updated,
            ('name', 'measure', 'sold', 'last_sold'),
            batch_size=WRITE_BATCH,
        )


def _demand_day(refund) -> object:
    moment = refund.sale_occurred_at or (refund.sale.occurred_at if refund.sale_id else None)
    if moment is None:
        moment = refund.occurred_at
    return _local_day(moment)


def _local_day(moment: datetime):
    tz = ZoneInfo(settings.TIME_ZONE)
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment)
    return moment.astimezone(tz).date()
