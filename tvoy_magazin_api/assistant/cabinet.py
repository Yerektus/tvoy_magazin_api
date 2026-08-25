"""Что аналитик может посмотреть в кабинете UMAG.

Отдельный файл, а не пара функций в `tools`, — чтобы граница была видимой.
Правила здесь жёстче, чем для своих данных, потому что UMAG чужой и в него
можно писать:

1. **Только чтение.** Ходим через [ReadOnly], который умеет один метод — GET, и
   только по адресам из [ALLOWED]. Даже если завтра кто-то добавит сюда ручку,
   создать приёмку или контрагента через неё не выйдет.

2. **Токен спрашивающего.** Берём кабинет того, кто задал вопрос, и его магазин.
   Не «любой подключённый в организации»: токен принадлежит сотруднику, и
   показывать через него данные другому — не наше право.

3. **Кабинет молчит — молчим и мы.** UMAG недоступен, токен протух, товара нет
   — возвращаем понятную строку, а не роняем разговор.

4. **Сначала своя копия.** Номенклатура лежит у нас (`UmagProduct`), и поиск по
   ней не стоит ни запроса, ни секунды ожидания. В кабинет идём за тем, чего в
   копии нет по существу: за ценой, остатком и продажами на сейчас.

Продажи — главное, чего нет больше нигде: у нас лежит закуп (что привезли), а
что из этого разошлось, знает только касса. Отдаёт их товарный отчёт — тот же,
на котором считается план закупа, поэтому ходим в него через `planner.report`, а
не своим запросом.
"""

import logging

from django.core.cache import cache

from purchases import planner
from umag.client import UmagClient, UmagError
from umag.models import UmagAccount, UmagProduct

logger = logging.getLogger(__name__)

#: Единственные адреса кабинета, которые аналитику разрешено читать.
ALLOWED = frozenset({'nom/product/findProductByBarcode', planner.REPORT})

#: Больше товаров за раз не отдаём.
MAX_ROWS = 30

#: Сколько строк отчёта просматриваем. Проверенный кабинет — 2750 товаров, то
#: есть магазин помещается целиком; дальше начинается хвост, который продаётся
#: реже раза в месяц.
SCAN = 3000

#: Сколько секунд помним отчёт. В одном ответе аналитик спрашивает и «что
#: продаётся», и «что кончается» — второй раз ждать кабинет незачем.
REPORT_TTL = 120

#: Дольше года не смотрим: за год ассортимент меняется целиком.
MAX_DAYS = 365


class ReadOnly:
    """Обёртка над клиентом кабинета, умеющая только смотреть.

    У настоящего `UmagClient` есть `post`, `post_form` и `delete` — ими
    создаются приёмки и контрагенты. Сюда они не проброшены намеренно: у
    аналитика нет способа что-либо изменить, даже ошибившись.
    """

    def __init__(self, account):
        self._client = UmagClient(account)

    def get(self, path: str, **params):
        if path not in ALLOWED:
            raise UmagError(f'Читать {path} аналитику нельзя', 403)

        return self._client.get(path, **params)


def _cabinet(user) -> ReadOnly | None:
    """Кабинет спрашивающего — или ничего, если он не подключён."""

    account = UmagAccount.objects.filter(user=user).first()

    return ReadOnly(account) if account and account.ready else None


def catalog(user, query='') -> dict:
    """Поиск по номенклатуре магазина.

    Ищем в своей копии: она обновляется раз в сутки и для «есть ли такой товар»
    этого достаточно, а кабинет отвечает не быстро.
    """

    account = UmagAccount.objects.filter(user=user).first()

    if account is None or not account.store_id:
        return {'ошибка': 'UMAG не подключён или магазин не выбран'}

    rows = UmagProduct.objects.filter(store_id=account.store_id)
    query = str(query or '').strip()[:120]

    if query:
        rows = rows.filter(name__icontains=query)

    return {
        'магазин': account.store_name or None,
        'найдено': rows.count(),
        'товары': [
            {
                'товар': row.name,
                'штрихкод': row.barcode or None,
                'единица': row.measure or None,
            }
            for row in rows.order_by('name')[:MAX_ROWS]
        ],
    }


def product(user, barcode='') -> dict:
    """Карточка товара в кабинете: цена и остаток на сейчас.

    Единственное место, где аналитик ходит в UMAG живьём: цену и остаток в копии
    не держим — они меняются каждый час, и вчерашние хуже, чем никакие.
    """

    client = _cabinet(user)

    if client is None:
        return {'ошибка': 'UMAG не подключён'}

    code = str(barcode or '').strip()[:64]

    if not code.isdigit():
        return {'ошибка': 'Нужен штрихкод — только цифры'}

    try:
        found = client.get('nom/product/findProductByBarcode', barcode=code)
    except UmagError as error:
        # 422 у кабинета значит «такого штрихкода нет», остальное — его беда.
        if error.status == 422:
            return {'ошибка': 'Товара с таким штрихкодом в кабинете нет'}

        logger.warning('Аналитик не смог прочитать товар %s: %s', code, error)
        return {'ошибка': 'Кабинет UMAG сейчас не отвечает'}

    card = found.get('product') or {}
    prices = found.get('productStorePrice') or {}

    return {
        'товар': card.get('name') or None,
        'штрихкод': code,
        'остаток': found.get('stockQuantity'),
        'цена_продажи': prices.get('sellingPrice'),
        'цена_прихода': prices.get('arrivalCost'),
    }


def sales(user, days=30, query='') -> dict:
    """Что продавалось за период: выручка, маржа и остаток по каждому товару.

    Единственный источник продаж в проекте. Свои накладные говорят, что в
    магазин привезли; разошлось ли это с полки, видно только здесь.
    """

    rows, error = _sold(user, days)

    if error:
        return error

    days = _days(days)
    query = str(query or '').strip().lower()[:120]

    if query:
        rows = [row for row in rows if query in _title(row).lower()]

    # Сортируем по выручке: «что продаётся» — вопрос про деньги, а не про
    # штуки. Штучное иначе всегда обгоняло бы весовое.
    rows.sort(key=lambda row: _float(row.get('saleSellingAmount')), reverse=True)

    return {
        'период_дней': days,
        'найдено': len(rows),
        'товары': [_row(row, days) for row in rows[:MAX_ROWS]],
    }


def running_out(user, days=30) -> dict:
    """Товары, которые кончатся раньше всех: остаток на нынешнем расходе.

    Считаем на сейчас, а не берём из плана закупа: план считают по кнопке, и
    вчерашний уже не про сегодняшнюю полку.
    """

    rows, error = _sold(user, days)

    if error:
        return error

    days = _days(days)
    ending = []

    for row in rows:
        line = _row(row, days)

        # Не продавался или полка полна — в списке «кончается» ему не место.
        if line['хватит_дней'] is None or line['продано'] in (None, 0):
            continue

        ending.append(line)

    ending.sort(key=lambda line: line['хватит_дней'])

    return {
        'период_дней': days,
        'найдено': len(ending),
        'товары': ending[:MAX_ROWS],
    }


def _sold(user, days) -> tuple[list[dict], dict | None]:
    """Строки товарного отчёта — или понятная ошибка вместо них."""

    account = UmagAccount.objects.filter(user=user).first()
    client = _cabinet(user)

    if client is None or account is None:
        return [], {'ошибка': 'UMAG не подключён'}

    days = _days(days)
    key = f'assistant:report:{account.store_id}:{days}'
    rows = cache.get(key)

    if rows is None:
        try:
            rows = planner.report(client, days, max_rows=SCAN)
        except UmagError as error:
            logger.warning('Аналитик не смог прочитать отчёт: %s', error)
            return [], {'ошибка': 'Кабинет UMAG сейчас не отвечает'}

        cache.set(key, rows, REPORT_TTL)

    return list(rows), None


def _row(row: dict, days: int) -> dict:
    """Строка отчёта в том виде, в каком её читает аналитик."""

    sold = _number(_float(row.get('saleQuantity')) - _float(row.get('refundQuantity')))
    stock = _number(row.get('stockQuantity'))
    per_day = (sold or 0) / days

    return {
        'товар': _title(row) or None,
        'штрихкод': str(row.get('barcode') or '') or None,
        'единица': (row.get('measure') or '').strip() or None,
        'продано': sold,
        'выручка': _money(row.get('saleSellingAmount')),
        'маржа': _money(row.get('marginAmount')),
        'наценка_%': _money(row.get('markupPercentage')),
        'остаток': stock,
        # Отрицательный остаток — пересорт в кабинете, а не запас на полке:
        # по хлебу и яйцам остатки там не ведут.
        'хватит_дней': round(max(stock or 0, 0) / per_day, 1) if per_day > 0 else None,
    }


def _title(row: dict) -> str:
    return (row.get('productName') or row.get('productFullName') or '').strip()


def _days(days) -> int:
    """Период в днях. Чужие числа приводим к своим границам, а не верим им."""

    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30

    return max(1, min(days, MAX_DAYS))


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(value):
    return None if value is None else round(_float(value), 2)


def _number(value, digits=3):
    if value is None:
        return None

    number = round(_float(value), digits)

    return int(number) if number == int(number) else number


SCHEMAS = [
    {
        'type': 'function',
        'function': {
            'name': 'umag_catalog',
            'description': 'Поиск товара в номенклатуре магазина по части названия. '
            'Отдаёт штрихкоды, по которым можно узнать цену и остаток.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'description': 'Часть названия товара'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'umag_sales',
            'description': 'Продажи магазина за период по товарам: сколько продано, '
            'на какую сумму, маржа, наценка и остаток на сейчас. Это единственный '
            'источник продаж — в накладных только закуп.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'days': {'type': 'integer', 'description': 'За сколько дней, 1–365'},
                    'query': {
                        'type': 'string',
                        'description': 'Часть названия товара, если спрашивают про один',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'umag_running_out',
            'description': 'Что заканчивается на полке: товары с самым коротким '
            'запасом, считая по продажам за период. Здесь всё на сейчас, в отличие '
            'от плана закупа, который считают по кнопке.',
            'parameters': {
                'type': 'object',
                'properties': {'days': {'type': 'integer'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'umag_product',
            'description': 'Цена и остаток товара в кабинете UMAG на сейчас. '
            'Штрихкод берут из поиска по номенклатуре или из позиции накладной.',
            'parameters': {
                'type': 'object',
                'properties': {'barcode': {'type': 'string'}},
                'required': ['barcode'],
            },
        },
    },
]

HANDLERS = {
    'umag_catalog': catalog,
    'umag_sales': sales,
    'umag_running_out': running_out,
    'umag_product': product,
}
