"""Товарный отчёт UMAG → план закупа.

Отчёт `report/list-product-report` отдаёт по каждому товару продажи за период и
остаток на сейчас — этого хватает, чтобы посчитать расход в день и понять, чего
не доживёт до следующего завоза. Продажи в кабинете есть только так: отдельного
API продаж по товарам у UMAG нет.

Считаем без хитростей: сколько продавалось в день, на сколько дней хватит
остатка и сколько дозаказать, чтобы хватило на горизонт планирования.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP

from django.utils import timezone

from umag.client import UmagClient, UmagError
from umag.models import UmagAccount

from .models import PurchasePlanItem

logger = logging.getLogger(__name__)

REPORT = 'report/list-product-report'
SUPPLIER_REPORT = 'report/list-supplier-report'
AGENTS = 'org/agent/list-agent-names'

# Поставщиков в кабинете под шесть сотен, но продавали в периоде десятки —
# по ним и ходим. По одному это минуты, поэтому в несколько потоков.
SUPPLIER_THREADS = 6
AGENTS_PAGE = 1000

# Отчёт отдаёт тысячами строк, берём страницами. Дальше пяти тысяч — хвост,
# который продаётся раз в месяц, планировать там нечего.
PAGE = 1000
MAX_ROWS = 5000

# Штучное заказывают целыми, весовое — с точностью до сотой.
WHOLE_MEASURES = ('шт', 'уп', 'пач', 'кор', 'бут')

ZERO = Decimal('0')


class PlanError(RuntimeError):
    """План не посчитать — человеку нужно что-то поправить."""


def build(plan) -> None:
    """Считает план и заполняет его строки. Ошибку поднимает наверх."""


    account = UmagAccount.objects.filter(user=plan.user).first()

    if account is None or not account.ready:
        raise PlanError('Подключите UMAG и выберите магазин')

    client = UmagClient(account, plan.store_id or account.store_id)

    try:
        rows = report(client, plan.days)
    except UmagError as error:
        raise PlanError(str(error)) from error

    needed = [line for row in rows if (line := _line(row, plan.days, plan.horizon))]

    # Закупаются поставщиками, а не построчно, поэтому у каждой строки должен
    # быть свой. Не получилось — план всё равно нужен, просто без группировки.
    known = suppliers(client, plan.days)

    for line in needed:
        line['supplier'] = known.get(line['barcode'], '')

    # Сначала то, что кончится раньше всех, при равном сроке — что расходится
    # быстрее: без него полка опустеет заметнее.
    needed.sort(key=lambda line: (line['cover_days'], -line['per_day']))

    plan.items_total = len(needed)
    plan.total_cost = sum((line['cost'] or ZERO for line in needed), ZERO)

    # Строки сохраняем все: закуп идёт по поставщикам, и обрезанный список
    # оставил бы часть из них без половины заказа.
    PurchasePlanItem.objects.bulk_create(
        PurchasePlanItem(plan=plan, position=position, **line)
        for position, line in enumerate(needed, start=1)
    )


def report(client, days: int, **filters) -> list[dict]:
    """Товарный отчёт за период, страницами. `filters` — например `supplierId`."""

    now = timezone.now()
    to_time = _millis(now)
    from_time = _millis(now - timedelta(days=days))

    rows: list[dict] = []

    while len(rows) < MAX_ROWS:
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
        if len(page) < PAGE or len(rows) >= (body.get('count') or 0):
            break

    return rows


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
        return report(client, days, supplierId=agent.get('id'))
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


def _line(row: dict, days: int, horizon: int) -> dict | None:
    """Строка плана по товару. Пусто — заказывать нечего."""

    sold = _decimal(row.get('saleQuantity')) - _decimal(row.get('refundQuantity'))

    # Не продавался — планировать нечего: заводить запас под ноль продаж
    # значит замораживать деньги на полке.
    if sold <= 0:
        return None

    per_day = sold / Decimal(days)
    stock = _decimal(row.get('stockQuantity'))
    measure = (row.get('measure') or '').strip()

    # Отрицательный остаток — пересорт в кабинете; для закупа это тот же ноль.
    on_hand = max(stock, ZERO)
    suggested = _round(per_day * Decimal(horizon) - on_hand, measure)

    if suggested <= 0:
        return None

    price = _price(row)

    return {
        'barcode': str(row.get('barcode') or ''),
        'name': (row.get('productName') or row.get('productFullName') or '').strip()[:255],
        'measure': measure[:32],
        'sold': _quantity(sold),
        'stock': _quantity(stock),
        'per_day': _quantity(per_day),
        'cover_days': (on_hand / per_day).quantize(Decimal('0.1')),
        'suggested': suggested,
        'price': price,
        'cost': (suggested * price).quantize(Decimal('0.01')) if price is not None else None,
    }


def _price(row: dict) -> Decimal | None:
    """Средняя закупочная за период: сумма прихода на проданное количество."""

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
