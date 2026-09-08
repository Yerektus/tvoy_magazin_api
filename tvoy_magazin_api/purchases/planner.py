"""Товарный отчёт UMAG → план закупа.

Отчёт `report/list-product-report` отдаёт актуальный остаток и закупочную цену.
Спрос берём из локальной копии всех чеков и возвратов: по дневному ряду можно
увидеть тренд, неделю, годовой сезон и прошлые праздники. Сам прогноз выбирает
подходящую объёму истории модель в `forecast.py`.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP

from django.utils import timezone

from umag import matching, sales as umag_sales
from umag.client import UmagClient, UmagError
from umag.models import UmagAccount

from . import forecast as demand_forecast
from .models import PurchasePlan, PurchasePlanItem

logger = logging.getLogger(__name__)

REPORT = 'report/list-product-report'
SUPPLIER_REPORT = 'report/list-supplier-report'
AGENTS = 'org/agent/list-agent-names'

# Поставщиков в кабинете под шесть сотен, но продавали в периоде десятки —
# по ним и ходим. По одному это минуты, поэтому в несколько потоков.
SUPPLIER_THREADS = 6
AGENTS_PAGE = 1000

# Отчёт отдаёт тысячами строк, берём страницами. Быстрые просмотры по умолчанию
# ограничены пятью тысячами; полный план явно снимает предел, чтобы не потерять
# редкий сезонный товар.
PAGE = 1000
MAX_ROWS = 5000

# Штучное заказывают целыми, весовое — с точностью до сотой.
WHOLE_MEASURES = ('шт', 'уп', 'пач', 'кор', 'бут')

ZERO = Decimal('0')


class PlanError(RuntimeError):
    """План не посчитать — человеку нужно что-то поправить."""


class Cancelled(Exception):
    """Расчёт бросили — результат сохранять не нужно."""


def build(plan) -> None:
    """Считает план и заполняет его строки. Ошибку поднимает наверх."""

    account = UmagAccount.objects.filter(user=plan.user).first()

    if account is None or not account.ready:
        raise PlanError('Подключите UMAG и выберите магазин')

    client = UmagClient(account, plan.store_id or account.store_id)

    try:
        # UMAG жёстко ограничивает отчёт тремя месяцами. Полная история для
        # модели лежит в чеках; отсюда нужны только текущие остатки и цены.
        rows = report(client, plan.days, max_rows=None)
        _still_building(plan)
        # В первый раз это полная история чеков, дальше — только недельный
        # перекрывающийся хвост. Расчёт уже фоновый, поэтому UI продолжает
        # показывать BUILDING и может отменить долгую первичную загрузку.
        umag_sales.sync(account, progress=lambda: _still_building(plan))
    except UmagError as error:
        raise PlanError(str(error)) from error
    except (RuntimeError, ValueError) as error:
        raise PlanError(str(error)) from error

    _still_building(plan)

    # Прогнозируем всю сохранённую номенклатуру, а не только товары с продажей
    # в выбранном коротком периоде: так в план возвращаются сезонные позиции.
    predictions = demand_forecast.for_products(
        plan.user.organization,
        plan.store_id,
        None,
        plan.horizon,
    )
    prepared = [dict(row) for row in rows]
    present = {str(row.get('barcode') or '') for row in prepared}
    missing = [
        barcode
        for barcode, prediction in predictions.items()
        if barcode not in present and prediction.quantity + prediction.safety_stock > 0
    ]
    prepared.extend(_live_rows(client, missing))

    needed = [
        line
        for row in prepared
        if (
            line := _line(
                row,
                plan.days,
                plan.horizon,
                plan.use_stock,
                predictions.get(str(row.get('barcode') or '')),
            )
        )
    ]

    # Закупаются поставщиками, а не построчно, поэтому у каждой строки должен
    # быть свой. Не получилось — план всё равно нужен, просто без группировки.
    _still_building(plan)
    known = suppliers(client, plan.days)
    _still_building(plan)

    for line in needed:
        line['supplier'] = known.get(line['barcode'], '')

    # Сначала то, что кончится раньше всех, при равном сроке — что расходится
    # быстрее: без него полка опустеет заметнее.
    needed.sort(key=lambda line: (line['cover_days'], -line['per_day']))

    plan.items_total = len(needed)
    plan.total_cost = sum((line['cost'] or ZERO for line in needed), ZERO)

    # Строки сохраняем все: закуп идёт по поставщикам, и обрезанный список
    # оставил бы часть из них без половины заказа.
    _still_building(plan)
    PurchasePlanItem.objects.bulk_create(
        PurchasePlanItem(plan=plan, position=position, **line)
        for position, line in enumerate(needed, start=1)
    )


def _live_rows(client, barcodes: list[str]) -> list[dict]:
    """Актуальные остатки сезонных товаров, которых нет в коротком отчёте."""

    if not barcodes:
        return []

    with ThreadPoolExecutor(max_workers=SUPPLIER_THREADS) as pool:
        rows = pool.map(lambda barcode: _live_row(client, barcode), barcodes)

    return [row for row in rows if row is not None]


def _live_row(client, barcode: str) -> dict | None:
    try:
        found = client.get('nom/product/findProductByBarcode', barcode=barcode)
    except UmagError as error:
        # Карточку могли удалить после прошлогоднего сезона. Основной план из-за
        # одной такой позиции не теряем.
        logger.warning('UMAG не отдал сезонный товар %s: %s', barcode, error)
        return None

    if not isinstance(found, dict):
        return None

    card = found.get('product') or {}
    prices = found.get('productStorePrice') or {}

    return {
        'barcode': barcode,
        'productName': (card.get('name') or '').strip(),
        'measure': matching.unit_for(card.get('measure')),
        'saleQuantity': 0,
        'refundQuantity': 0,
        'stockQuantity': found.get('stockQuantity'),
        '_price': prices.get('arrivalCost'),
    }


def _still_building(plan) -> None:
    """Бросает, если план уже удалили или перестали считать."""

    if not PurchasePlan.objects.filter(pk=plan.pk, status=PurchasePlan.Status.BUILDING).exists():
        raise Cancelled()


def report(client, days: int, max_rows: int | None = MAX_ROWS, **filters) -> list[dict]:
    """Товарный отчёт за период, страницами. `filters` — например `supplierId`.

    `max_rows` — где остановиться. Плану нужен весь ассортимент, а тому, кто
    просто смотрит на магазин, хватает верхушки: каждая страница — ещё запрос
    и ещё секунда ожидания.
    """

    now = timezone.now()
    to_time = _millis(now)
    from_time = _millis(now - timedelta(days=days))

    rows: list[dict] = []

    while max_rows is None or len(rows) < max_rows:
        body = client.get(
            REPORT,
            fromTime=from_time,
            toTime=to_time,
            first=len(rows),
            pageSize=PAGE,
            **filters,
        )

        page = (body or {}).get('data') or []
        rows.extend(page)

        # Страница неполная или отчёт закончился — дальше ходить незачем.
        if (
            len(page) < PAGE
            or len(rows) >= (body.get('count') or 0)
            or (max_rows is not None and len(rows) >= max_rows)
        ):
            break

    return rows if max_rows is None else rows[:max_rows]


def suppliers(client, days: int) -> dict[str, str]:
    """Штрихкод → поставщик, у которого этот товар берут.

    В товарном отчёте поставщика нет, зато он принимает `supplierId` — так и
    собираем карту: спрашиваем отчёт по каждому поставщику с продажами.
    """

    try:
        selling = _selling(client, days)
        agents = [agent for agent in _agents(client) if _name(agent) in selling]
    except UmagError as error:
        logger.warning('UMAG не отдал поставщиков: %s', error)
        return {}

    if not agents:
        return {}

    # Один и тот же товар берут у разных поставщиков — оставляем того, у кого
    # его продали больше: к нему и пойдут за следующей партией.
    found: dict[str, tuple[Decimal, str]] = {}

    with ThreadPoolExecutor(max_workers=SUPPLIER_THREADS) as pool:
        for agent, rows in zip(agents, pool.map(lambda item: _of(client, item, days), agents)):
            for row in rows:
                barcode = str(row.get('barcode') or '')
                quantity = _decimal(row.get('saleQuantity'))

                if barcode and quantity > found.get(barcode, (ZERO, ''))[0]:
                    found[barcode] = (quantity, _name(agent))

    return {barcode: name for barcode, (_, name) in found.items()}


def _of(client, agent: dict, days: int) -> list[dict]:
    """Товары одного поставщика. Ошибка — просто строка без группы."""

    try:
        return report(client, days, max_rows=None, supplierId=agent.get('id'))
    except UmagError as error:
        logger.warning('UMAG не отдал товары поставщика %s: %s', agent.get('id'), error)
        return []


def _selling(client, days: int) -> set[str]:
    """Поставщики, чей товар в периоде продавался: остальных обходить незачем."""

    now = timezone.now()
    body = client.get(
        SUPPLIER_REPORT,
        fromTime=_millis(now - timedelta(days=days)),
        toTime=_millis(now),
        first=0,
        pageSize=PAGE,
    )

    return {_name(row) for row in (body or {}).get('data') or [] if _name(row)}


def _agents(client) -> list[dict]:
    body = client.get(AGENTS, agentType='SUPPLIER', first=0, pageSize=AGENTS_PAGE)

    return body if isinstance(body, list) else (body or {}).get('agents') or []


def _name(item: dict) -> str:
    return (item.get('name') or item.get('supplierName') or '').strip()


def _line(
    row: dict,
    days: int,
    horizon: int,
    use_stock: bool = True,
    prediction: demand_forecast.Forecast | None = None,
) -> dict | None:
    """Строка плана по товару. Пусто — заказывать нечего."""

    sold = _decimal(row.get('saleQuantity')) - _decimal(row.get('refundQuantity'))

    # Без сохранённой истории остаётся прежняя безопасная формула. Это важно
    # при первом развёртывании и для тестового кабинета без чеков.
    if prediction is None and sold <= 0:
        return None

    per_day = sold / Decimal(days) if sold > 0 else ZERO
    stock = _decimal(row.get('stockQuantity'))
    measure = (row.get('measure') or '').strip()

    # Отрицательный остаток — пересорт в кабинете; для закупа это тот же ноль.
    # А когда остаток не берут в расчёт, заказываем весь горизонт целиком: так
    # считают перед праздником, когда полку хотят набить заново.
    on_hand = max(stock, ZERO) if use_stock else ZERO
    forecast_quantity = prediction.quantity if prediction else per_day * Decimal(horizon)
    forecast_per_day = prediction.per_day if prediction else per_day
    safety_stock = prediction.safety_stock if prediction else ZERO
    suggested = _round(forecast_quantity + safety_stock - on_hand, measure)

    if forecast_quantity <= 0 or forecast_per_day <= 0 or suggested <= 0:
        return None

    price = _price(row)

    return {
        'barcode': str(row.get('barcode') or ''),
        'name': (row.get('productName') or row.get('productFullName') or '').strip()[:255],
        'measure': measure[:32],
        'sold': _quantity(sold),
        'stock': _quantity(stock),
        'per_day': _quantity(per_day),
        'cover_days': (on_hand / forecast_per_day).quantize(Decimal('0.1')),
        'forecast_model': prediction.model if prediction else 'average',
        'forecast_quantity': _quantity(forecast_quantity),
        'forecast_per_day': _quantity(forecast_per_day),
        'safety_stock': _quantity(safety_stock),
        'holiday_factor': prediction.holiday_factor if prediction else Decimal('1.000'),
        'forecast_error': prediction.error if prediction else None,
        'suggested': suggested,
        'price': price,
        'cost': (suggested * price).quantize(Decimal('0.01')) if price is not None else None,
    }


def _price(row: dict) -> Decimal | None:
    """Средняя закупочная за период: сумма прихода на проданное количество."""

    if row.get('_price') is not None:
        return _decimal(row['_price']).quantize(Decimal('0.01'))

    quantity = _decimal(row.get('saleQuantity'))
    amount = _decimal(row.get('saleArrivalAmount'))

    if quantity <= 0 or amount <= 0:
        return None

    return (amount / quantity).quantize(Decimal('0.01'))


def _round(value: Decimal, measure: str) -> Decimal:
    """Заказ округляем вверх: недобрать хуже, чем взять с запасом."""

    step = Decimal('1') if measure.lower().startswith(WHOLE_MEASURES) else Decimal('0.01')

    return value.quantize(step, rounding=ROUND_UP)


def _quantity(value: Decimal) -> Decimal:
    return value.quantize(Decimal('0.001'))


def _decimal(value) -> Decimal:
    if value is None:
        return ZERO

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return ZERO


def _millis(moment) -> int:
    return int(moment.timestamp() * 1000)
