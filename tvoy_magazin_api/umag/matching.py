"""Строка накладной → товар в кабинете UMAG.

Штрихкод есть — товар находится точно, и из его карточки берутся название и
цена. Штрихкода нет или его нет в кабинете — ищем кандидатов по названию и
просим модель выбрать, какой товар тот же самый.

Уверенный выбор модель вписывает в строку сама — штрихкод берётся из карточки
UMAG, и дальше строка сопоставляется уже по нему, как будто он был в бумаге.
Там, где модель сомневается, она только предлагает: подставить или нет, решает
человек. Порог не формальность — неверный товар молча уедет в приёмку и
испортит остатки.

Сам выбор хранится в строке накладной, поэтому повторная проверка не гоняет
модель второй раз. Правка названия или штрихкода его сбрасывает.
"""

import logging
import re
from decimal import Decimal

from invoices.openrouter import OpenRouterError, match_products

from .client import UmagError

logger = logging.getLogger(__name__)

# Сколько кандидатов показываем модели: больше — дороже и без пользы.
CANDIDATES = 12

# Столько поисков по названию на строку максимум: каждый — ходка в UMAG.
QUERIES = 3

# Слова короче ищут пол-магазина: «1 л», «шт», «*12».
WORD = 4

# Ниже этого модель сама себе не верит — такую догадку не показываем вовсе.
CONFIDENCE = 0.7

# А отсюда и выше штрихкод вписывается в строку сам, без кнопки.
AUTO = 0.85

# Товары, а не услуги и не тара: тем же типом ищет приёмка в кабинете.
PRODUCT_TYPE = 0

# Единицу карточка хранит кодом. Расшифровка снята с номенклатуры кабинета:
# 0 — штучные товары, 1 — весовые (мука, крупы, развес), 2 — разливные.
MEASURES = {0: 'шт', 1: 'кг', 2: 'л'}


def unit_for(measure) -> str:
    """Единица измерения из карточки товара. Незнакомый код не выдумываем."""

    return MEASURES.get(measure, '')


def suggest(invoice, matches: list[dict], client) -> set[int]:
    """Разбирается со строками, которым штрихкод не помог.

    Уверенный выбор вписывает в строку, сомнительный кладёт в `matches`
    подсказкой. Возвращает id строк, куда штрихкод подставлен сам: их нужно
    перечитать из UMAG, чтобы в ответе была карточка товара.
    """

    pending = [match for match in matches if match['status'] in ('no_barcode', 'unknown_barcode')]
    if not pending:
        return set()

    lines = {line.pk: line for line in invoice.lines.all()}
    filled: set[int] = set()
    ask = []

    for match in pending:
        line = lines.get(match['id'])
        if line is None:
            continue

        # Товар уже выбирали — второй раз не платим.
        if line.umag_product_id and line.umag_barcode:
            if _accept(line, line.umag_barcode, line.umag_confidence):
                filled.add(line.pk)
            else:
                _apply(match, line.umag_product_name, line.umag_barcode, line.umag_confidence)
            continue

        found = candidates(line.name, client)
        if found:
            ask.append({'line': line, 'match': match, 'candidates': found})

    if ask:
        filled |= _guess(invoice, ask)

    return filled


def candidates(name: str, client) -> list[dict]:
    """Товары кабинета, похожие названием на строку накладной."""

    found: dict[int, dict] = {}

    for query in _queries(name):
        try:
            body = client.get(
                'nom/product/by-part',
                namePart=query,
                limit=CANDIDATES,
                productTypes=PRODUCT_TYPE,
            )
        except UmagError as error:
            # Поиск — не повод ронять проверку: без кандидатов строка просто
            # останется несопоставленной.
            logger.warning('UMAG не нашёл товары по «%s»: %s', query, error)
            continue

        for product in body if isinstance(body, list) else []:
            product_id = product.get('id')
            barcode = str(product.get('barcode') or '')

            # Без штрихкода товар не предложить: подставлять в строку нечего.
            if product_id and barcode and product_id not in found:
                found[product_id] = {
                    'id': product_id,
                    'name': (product.get('name') or '').strip(),
                    'barcode': barcode,
                }

        # Набрали, из чего выбирать, — дальше запросы уже общее и мусорнее.
        if len(found) >= CANDIDATES:
            break

    return list(found.values())[:CANDIDATES]


def _queries(name: str) -> list[str]:
    """Запросы к поиску — от точного к общему.

    Карточка в кабинете называется короче строки в накладной («Пряник
    шоколадный сайрам нан» против «Пряник шоколадный 450гр Сайрам нан 1*14»),
    поэтому целиком строку не ищем — берём её начало.

    Порядок слов тут важнее их длины: в русском названии первое слово — сам
    товар («Коржик», «Пряник»), а самое длинное обычно прилагательное
    («шоколадный», «Ванильный»), по которому находится что угодно, кроме
    нужного. Поэтому длинное слово идёт последним, на случай названий вроде
    «Напиток PEPSI-COLA».
    """

    cleaned = re.sub(r'[«»"()]', ' ', name or '')
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    words = [word for word in re.split(r'[\s,;/*]+', cleaned) if len(word) >= WORD]

    if not words:
        return []

    queries = [' '.join(words[:2]), words[0], max(words, key=len)]

    return list(dict.fromkeys(queries))[:QUERIES]


def _guess(invoice, ask: list[dict]) -> set[int]:
    """Одна ходка в модель на всю накладную."""

    payload = [
        {
            'id': item['line'].pk,
            'name': item['line'].name,
            'unit': item['line'].unit,
            'candidates': item['candidates'],
        }
        for item in ask
    ]

    try:
        answer = match_products(payload)
    except OpenRouterError as error:
        # Сопоставления не будет — строки останутся на человеке.
        logger.warning('Модель не сопоставила позиции накладной %s: %s', invoice.pk, error)
        return set()

    # Номера строк и товаров модель иногда отдаёт строками — сверяем числами.
    chosen = {
        _int(row.get('line_id')): row
        for row in (answer.data.get('matches') or [])
        if isinstance(row, dict)
    }

    filled: set[int] = set()

    for item in ask:
        row = chosen.get(item['line'].pk)
        if not row:
            continue

        product_id = _int(row.get('product_id'))
        product = next(
            (candidate for candidate in item['candidates'] if candidate['id'] == product_id),
            None,
        )
        confidence = _confidence(row.get('confidence'))

        # Товар не из списка кандидатов или уверенности нет — строку не трогаем.
        if product is None or confidence < CONFIDENCE:
            continue

        _remember(item['line'], product_id, product['name'], product['barcode'], confidence)

        if _accept(item['line'], product['barcode'], confidence):
            filled.add(item['line'].pk)
        else:
            _apply(item['match'], product['name'], product['barcode'], confidence)

    _charge(invoice, answer.cost)

    return filled


def remember_by_barcode(line, product_id, name: str, barcode: str, unit: str = '') -> None:
    """Строка сошлась по штрихкоду — точнее сопоставления не бывает.

    Кроме случая, когда этот штрихкод сама же модель и подставила: тогда её
    уверенность остаётся при строке, и видно, что цифра пришла не из бумаги.
    """

    guessed = line.umag_barcode == barcode and line.umag_confidence is not None
    _remember(line, product_id, name, barcode, line.umag_confidence if guessed else 1.0)

    # Единица — из карточки, а не с фотографии: в накладной пишут «бут.» и
    # «кор.», а товар в кабинете живёт в штуках, и приёмка считается в них.
    if unit and line.unit != unit:
        line.unit = unit
        line.save(update_fields=('unit',))


def _accept(line, barcode: str, confidence) -> bool:
    """Вписывает штрихкод в строку, когда модель уверена настолько.

    Дальше строка сопоставляется по штрихкоду наравне с теми, что пришли
    с бумаги: с ценой, остатком и карточкой товара.
    """

    if (confidence or 0) < AUTO or not barcode:
        return False

    if line.barcode != barcode:
        line.barcode = barcode
        line.save(update_fields=('barcode',))

    return True


def forget(line) -> None:
    """Название или штрихкод поправили — прежний выбор к строке не относится."""

    _remember(line, None, '', '', None)


def _remember(line, product_id, name: str, barcode: str, confidence) -> None:
    fields = {
        'umag_product_id': product_id,
        'umag_product_name': (name or '')[:255],
        'umag_barcode': (barcode or '')[:64],
        'umag_confidence': confidence,
    }

    if all(getattr(line, field) == value for field, value in fields.items()):
        return

    for field, value in fields.items():
        setattr(line, field, value)

    line.save(update_fields=tuple(fields))


def _apply(match: dict, name: str, barcode: str, confidence) -> None:
    """Кладёт выбранный товар в ответ проверки — как подсказку для человека."""

    match['suggested_name'] = name
    match['suggested_barcode'] = barcode
    match['confidence'] = confidence


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _confidence(value) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _charge(invoice, cost) -> None:
    """Сопоставление — платный запрос, он тоже идёт в стоимость документа."""

    if not cost:
        return

    invoice.cost = (invoice.cost or Decimal(0)) + Decimal(str(cost))
    invoice.save(update_fields=('cost',))
