"""Строка накладной → товар в кабинете UMAG.

Штрихкод есть — товар находится точно, и из его карточки берутся название и
цена. Ищем по названию только строки, у которых штрихкода нет вовсе.

Штрихкод с бумаги главнее названия, и спорить с ним мы не беремся. Он прошёл
проверку контрольной цифры, значит это настоящий код товара; если такого товара
в кабинете нет, товар новый — его заводят с этим же кодом (`supply`), а не
приклеивают строку к похожему по названию. Похожий по названию — это чужая
карточка, а приход на чужую карточку и есть пересорт.

Модель тут работает дважды: сначала говорит, каким куском названия искать —
в строке накладной товар перемешан с фасовкой, и что из этого главное, видно
только по смыслу, — а потом выбирает товар из найденного.

Выбор модель вписывает в строку сама — штрихкод берётся из карточки UMAG, и
дальше строка сопоставляется уже по нему, как будто он был в бумаге. Порог
один и невысокий: где модель уверена хотя бы наполовину, пусть подставляет.
Проверять всё равно человеку, а пустая строка помогает ему меньше, чем
заполненная с пометкой, что цифру дала модель.

Сам выбор хранится в строке накладной, поэтому повторная проверка не гоняет
модель второй раз. Правка названия или штрихкода его сбрасывает.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from invoices.openrouter import OpenRouterError, match_products, search_terms

from . import catalog
from .client import UmagError

logger = logging.getLogger(__name__)

# Сколько кандидатов показываем модели: больше — дороже и без пользы.
CANDIDATES = 12

# Столько поисков по названию на строку максимум: каждый — ходка в UMAG.
QUERIES = 3

# Во столько потоков доспрашиваем кабинет о строках, которых нет в копии.
LOOKUP_THREADS = 6

# Слова короче ищут пол-магазина: «1 л», «шт», «*12».
WORD = 4

# Порог один: уверена наполовину и выше — штрихкод идёт прямо в строку, ниже
# — выбор не принимаем совсем. Раньше между ними была полоса «только подсказка»,
# но подсказка, которую надо подтверждать руками, экономит меньше, чем стоит
# лишний шаг: строку человек всё равно сверяет с бумагой перед отправкой.
CONFIDENCE = 0.5

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

    pending = [match for match in matches if match['status'] == 'no_barcode']
    if not pending:
        return set()

    lines = {line.pk: line for line in invoice.lines.all()}
    filled: set[int] = set()
    ask = []
    todo = []

    for match in pending:
        line = lines.get(match['id'])
        if line is None:
            continue

        # Товар уже выбирали — второй раз не платим. Номера товара при выборе
        # из нашей копии номенклатуры нет, поэтому смотрим на штрихкод: он и
        # есть результат выбора.
        if line.umag_barcode:
            if _accept(line, line.umag_barcode, line.umag_confidence):
                filled.add(line.pk)
            else:
                _apply(match, line.umag_product_name, line.umag_barcode, line.umag_confidence)
            continue

        todo.append((line, match))

    # Своя копия номенклатуры ищет лучше кабинета и без сети. Её нет — идём в
    # UMAG по подстроке, и тогда слово для поиска выбирает модель.
    search = catalog.finder(client.store_id)
    terms = {} if search else _terms(invoice, [line for line, _ in todo])

    misses = []

    for line, match in todo:
        found = (
            search.find(line.name)
            if search
            else candidates(line.name, client, terms.get(line.pk, ''))
        )

        if found:
            ask.append({'line': line, 'match': match, 'candidates': found})
        elif search:
            misses.append((line, match))

    # В копии не нашлось — спрашиваем кабинет живьём: в отчёт, из которого она
    # собрана, не попадает товар, не двигавшийся три месяца.
    ask.extend(_lookup(client, misses))

    if ask:
        filled |= _guess(invoice, ask)

    return filled


def candidates(name: str, client, term: str = '') -> list[dict]:
    """Товары кабинета, похожие названием на строку накладной.

    `term` — чем искать по мнению модели. Не нашлось по нему — идём своими
    правилами: без кандидатов строка осталась бы несопоставленной вовсе.
    """

    found: dict[int, dict] = {}

    for query in _queries(name, term):
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
                    # Тут номер карточки известен сразу, а при поиске по нашей
                    # копии номенклатуры его нет — поле общее, значение разное.
                    'product_id': product_id,
                    'name': (product.get('name') or '').strip(),
                    'barcode': barcode,
                }

        # Набрали, из чего выбирать, — дальше запросы уже общее и мусорнее.
        if len(found) >= CANDIDATES:
            break

    return list(found.values())[:CANDIDATES]


def _queries(name: str, term: str = '') -> list[str]:
    """Запросы к поиску — от точного к общему.

    Первым идёт то, что выбрала модель: она видит, где в строке товар, а где
    фасовка. Дальше — наши правила, на случай когда модель промолчала или её
    слово ничего не нашло.

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

    queries = [term.strip()] if term and term.strip() else []

    if words:
        queries += [' '.join(words[:2]), words[0], max(words, key=len)]

    return list(dict.fromkeys(query for query in queries if query))[:QUERIES]


def _lookup(client, misses: list) -> list[dict]:
    """Спрашивает кабинет о строках, которых не нашлось в копии.

    Копия обновляется раз в сутки, а товар в кабинете могли завести сегодня.
    Строк таких немного, но каждая — несколько запросов, поэтому идём в
    несколько потоков: они ждут сеть, а не процессор.
    """

    if not misses:
        return []

    with ThreadPoolExecutor(max_workers=LOOKUP_THREADS) as pool:
        found = pool.map(lambda item: catalog.lookup(client, client.store_id, item[0].name), misses)

    return [
        {'line': line, 'match': match, 'candidates': candidates}
        for (line, match), candidates in zip(misses, found)
        if candidates
    ]


def _terms(invoice, lines: list) -> dict[int, str]:
    """Спрашивает модель, чем искать каждую строку в номенклатуре.

    Одна ходка на всю накладную, до похода в кабинет. Не ответила — вернём
    пусто, и поиск пойдёт по своим правилам: молчание модели не повод
    оставлять строки несопоставленными.
    """

    if not lines:
        return {}

    try:
        answer = search_terms([{'id': line.pk, 'name': line.name} for line in lines])
    except OpenRouterError as error:
        logger.warning('Модель не выбрала слова для поиска (%s): %s', invoice.pk, error)
        return {}

    _charge(invoice, answer.cost)

    chosen: dict[int, str] = {}

    for row in answer.data.get('queries') or []:
        line_id = _int(row.get('line_id')) if isinstance(row, dict) else None

        if line_id is not None:
            chosen[line_id] = str(row.get('query') or '').strip()[:100]

    return chosen


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

        chosen_id = _int(row.get('product_id'))
        product = next(
            (candidate for candidate in item['candidates'] if candidate['id'] == chosen_id),
            None,
        )
        confidence = _confidence(row.get('confidence'))

        # Товар не из списка кандидатов или уверенности нет — строку не трогаем.
        if product is None or confidence < CONFIDENCE:
            continue

        # Номер карточки знаем не всегда: при выборе из нашей копии его нет.
        # Он появится сам, когда строка сопоставится по подставленному штрихкоду.
        _remember(
            item['line'],
            product.get('product_id'),
            product['name'],
            product['barcode'],
            confidence,
        )

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

    if (confidence or 0) < CONFIDENCE or not barcode:
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
