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
   ней не стоит ни запроса, ни секунды ожидания. В кабинет идём только за тем,
   чего в копии нет по существу: за ценой и остатком на сейчас.
"""

import logging

from umag.client import UmagClient, UmagError
from umag.models import UmagAccount, UmagProduct

logger = logging.getLogger(__name__)

#: Единственные адреса кабинета, которые аналитику разрешено читать.
ALLOWED = frozenset({'nom/product/findProductByBarcode'})

#: Больше товаров за раз не отдаём.
MAX_ROWS = 30


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
    'umag_product': product,
}
