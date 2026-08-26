"""Распознанная накладная → черновик приёмки в UMAG.

Черновик — это приёмка, для которой не позвали `provide`. Проводит её человек
руками в кабинете: это движение по складу и деньгам, и решать за него нельзя.
"""

import logging
import re
from datetime import datetime, time

from django.utils import timezone

from . import catalog, matching
from .client import UmagClient, UmagError
from .models import SupplierLink, UmagAccount

logger = logging.getLogger(__name__)

# Контрагентов у магазина шесть сотен — забираем одним запросом и ищем на месте.
AGENTS_PAGE = 1000

# Обычная строка приёмки. Бонусные UMAG держит отдельным списком.
LINE_TYPE = 0

#: Заведение товара в номенклатуре кабинета.
CREATE_PRODUCT = 'nom/product/create'
CATEGORIES = 'nom/category/find-categories'

#: Куда кладём заведённый товар. Так кабинет называет категорию для тех, кому
#: её не выбрали, — раскладывать товары по полкам не наше дело.
DEFAULT_CATEGORY = 'Незаданные'

#: Единица из накладной → код единицы в карточке UMAG. Всё, чего тут нет,
#: считаем штучным: так товар хотя бы заведётся, а единицу человек поправит.
UNITS = {
    'кг': 1,
    'г': 1,
    'гр': 1,
    'л': 2,
    'мл': 2,
}

#: Весовой и разливной товар кабинет держит отдельным типом.
WEIGHT_TYPE = 1

#: Внутренний весовой код магазина: тринадцать цифр, начинается с двойки.
INNER_TYPE = 2


class NotReady(UmagError):
    """Накладную ещё нельзя отправлять — сначала правки в UMAG или в строках."""


def preflight(invoice, account) -> dict:
    """Что мешает отправить накладную прямо сейчас.

    Ходит в UMAG по каждой строке, поэтому дёргается по кнопке, а не сам.
    """

    client = UmagClient(account, invoice.umag_store_id)
    supplier, matches, problems = _inspect(invoice, client)

    return {
        'ready': not problems,
        'supplier': supplier,
        'lines': [_line_view(match) for match in matches],
        'problems': problems,
    }


def push(invoice, account, agent_id: int | None = None) -> int:
    """Создаёт черновик приёмки и возвращает его номер в UMAG."""

    client = UmagClient(account, invoice.umag_store_id)

    if agent_id:
        _remember_supplier(invoice, client, agent_id)

    supplier, matches, problems = _inspect(invoice, client, create_supplier=True)

    if problems:
        raise NotReady('; '.join(problems))

    supply_id = _create_draft(client)

    try:
        _set_header(client, supply_id, invoice, supplier['agent_id'])
        _create_missing(client, matches, supplier['agent_id'])
        client.post(
            f'opr/supplies/v2/{supply_id}/add-products',
            {'products': [_product(match) for match in matches]},
        )
    except UmagError:
        # Половина приёмки хуже, чем её отсутствие: убираем за собой.
        _delete_draft(client, supply_id)
        raise

    invoice.umag_supply_id = supply_id
    invoice.umag_pushed_at = timezone.now()
    # Накладную завели до подключения UMAG — записываем, куда она в итоге ушла.
    invoice.umag_store_id = client.store_id
    invoice.umag_store_name = invoice.umag_store_name or account.store_name
    invoice.save(
        update_fields=(
            'umag_supply_id',
            'umag_pushed_at',
            'umag_store_id',
            'umag_store_name',
        )
    )

    return supply_id


def match_lines(invoice) -> None:
    """Сопоставляет позиции с номенклатурой сразу после распознавания.

    Ходит в кабинет от имени того, кто загрузил накладную: штрихкоды —
    прочитанные с бумаги и подобранные по прошлым накладным — превращаются в
    карточки товаров. К моменту, когда человек откроет накладную, позиции уже
    сведены. Строка без штрихкода остаётся несопоставленной: искать товар по
    названию мы перестали, см. `matching`.

    Заодно поддерживает свежесть копии номенклатуры: первая накладная за сутки
    ищет по вчерашней и запускает обновление, следующие идут уже по новой.
    """

    account = UmagAccount.objects.filter(user=invoice.created_by).first()

    # UMAG не подключён или магазин не выбран — сопоставлять не с чем.
    if account is None or not account.ready:
        return

    client = UmagClient(account, invoice.umag_store_id)

    for line in invoice.lines.all():
        _match_line(line, client)

    # Копия номенклатуры стареет — обновляем её после сопоставления, в фоне.
    # Расписания снаружи для этого не нужно.
    catalog.refresh_later(account)


def rematch_line(line) -> None:
    """Штрихкод в строке поправили — ищем товар в кабинете заново.

    Прежнее сопоставление к строке уже не относится, а новое можно найти сразу:
    штрихкод — точный ключ, и ждать до отправки, чтобы человек увидел, тот ли
    товар подобрался, незачем.

    Молча ничего не делаем, если кабинет не подключён или недоступен: правка
    строки — не то место, где человека стоит останавливать чужой ошибкой.
    Сопоставление тогда найдётся при следующей проверке накладной.
    """

    invoice = line.invoice
    account = UmagAccount.objects.filter(user=invoice.created_by).first()

    if account is None or not account.ready or not line.barcode:
        return

    try:
        _match_line(line, UmagClient(account, invoice.umag_store_id))
    except UmagError:
        return


def normalize(name: str) -> str:
    """«ТОО «Жасар-Сауда»» и «жасар сауда» — один и тот же поставщик."""

    lowered = re.sub(r'\b(тоо|ип|ао|кх|тов)\b', ' ', (name or '').lower())
    return re.sub(r'\s+', ' ', re.sub(r'[^0-9a-zа-яё]+', ' ', lowered)).strip()


def _inspect(invoice, client, create_supplier: bool = False) -> tuple[dict, list[dict], list[str]]:
    """Один проход по накладной: поставщик, строки и список претензий.

    `create_supplier` включает отправка: незнакомого контрагента заводим в
    UMAG сами. При обычном осмотре (`preflight`) он выключен — смотреть на
    вкладку «Проверка» можно сколько угодно, и записей от этого не прибавится.
    """

    supplier = _match_supplier(invoice, client, create=create_supplier)
    matches = [_match_line(line, client) for line in invoice.lines.all()]

    problems = []

    # Незнакомого контрагента заводим сами при отправке, поэтому помехой он
    # больше не считается — иначе осмотр вечно сообщал бы «нет в UMAG», а
    # фронт по этому сообщению до самой отправки бы и не доходил.
    # Остаётся один случай: тёзок несколько, и выбрать должен человек.
    if not supplier['agent_id'] and supplier['candidates']:
        problems.append('Выберите поставщика в UMAG')

    if not supplier['agent_id'] and not supplier['candidates'] and not (invoice.supplier or '').strip():
        problems.append('Поставщик в накладной не распознан — впишите название')

    if not matches:
        problems.append('В накладной нет позиций')

    # `new_product` не помеха: такой товар мы заведём сами при отправке.
    stuck = [match for match in matches if match['status'] not in ('ok', 'new_product')]
    if stuck:
        names = ', '.join(f'«{match["name"]}»' for match in stuck[:3])
        tail = f' и ещё {len(stuck) - 3}' if len(stuck) > 3 else ''
        problems.append(f'Не сопоставлены позиции: {names}{tail}')

    return supplier, matches, problems


def _create_draft(client) -> int:
    """`create` отдаёт пустой черновик — шапка и строки досылаются следом."""

    created = client.post('opr/supplies/v2/create')
    supply_id = created.get('id') if isinstance(created, dict) else created

    try:
        return int(supply_id)
    except (TypeError, ValueError) as error:
        raise UmagError(f'UMAG не вернул номер приёмки: {str(created)[:200]}') from error


def _set_header(client, supply_id: int, invoice, agent_id: int) -> None:
    """Шапка: поставщик, дата документа и откуда эта приёмка взялась."""

    payload = {
        'comment': f'Накладная №{invoice.number or invoice.id} — распознано в «Твой магазин»',
        'docTime': _doc_time(invoice),
        'supplierId': agent_id,
    }

    try:
        client.post(f'opr/supplies/v2/{supply_id}/edit', payload)
    except UmagError as error:
        if error.status != 400:
            raise

        # Формат даты снять было негде: кабинет её только показывает. Второй
        # заход шлёт миллисекунды — так время ходит в фильтрах списка приёмок.
        logger.warning('UMAG не принял дату строкой (%s), пробуем миллисекунды', error)
        payload['docTime'] = _doc_millis(invoice)
        client.post(f'opr/supplies/v2/{supply_id}/edit', payload)


def _delete_draft(client, supply_id: int) -> None:
    try:
        client.post(f'opr/supplies/v2/{supply_id}/delete', {'id': supply_id})
    except UmagError as error:
        # Не заслоняем исходную ошибку — про оставшийся черновик скажем в логе.
        logger.warning('Черновик %s остался в UMAG: %s', supply_id, error)


def _doc_date(invoice):
    return invoice.issued_at or timezone.localdate(invoice.created_at)


def _doc_time(invoice) -> str:
    return f'{_doc_date(invoice).isoformat()}T00:00:00'


def _doc_millis(invoice) -> int:
    moment = timezone.make_aware(datetime.combine(_doc_date(invoice), time.min))
    return int(moment.timestamp() * 1000)


def _match_supplier(invoice, client, create: bool = False) -> dict:
    """Ищет контрагента: сперва прошлый выбор, потом одноимённые кандидаты.

    Не нашёлся ни один и `create` включён — заводим его в UMAG сами.
    """

    name = normalize(invoice.supplier)
    # Связки живут по магазинам: в каждом свои контрагенты.
    link = SupplierLink.objects.filter(store_id=client.store_id, name=name).first()

    if link:
        return {
            'name': invoice.supplier,
            'agent_id': link.agent_id,
            'agent_name': link.agent_name,
            'candidates': [],
        }

    candidates = [
        {'id': agent['id'], 'name': (agent.get('name') or '').strip()}
        for agent in _agents(client)
        if name and normalize(agent.get('name') or '') == name
    ]

    # Единственного тёзку берём молча, из нескольких выбирает человек: в
    # кабинете поставщики задвоены, и не всё равно, на какого вешать приёмку.
    chosen = candidates[0] if len(candidates) == 1 else None

    # Тёзок нет вовсе — значит такого поставщика в кабинете ещё не заводили.
    # Когда есть из кого выбрать, молча создавать нельзя: получился бы дубль.
    if not candidates and create:
        return _create_supplier(invoice, client)

    return {
        'name': invoice.supplier,
        'agent_id': chosen['id'] if chosen else None,
        'agent_name': chosen['name'] if chosen else '',
        'candidates': candidates,
    }


# Правовая форма для карточки контрагента. UMAG принимает только эти значения,
# а угадать её можно по тому, как поставщик подписан в накладной.
LEGAL_TYPES = (
    ('ТОО', ('тоо', 'товарищество')),
    ('ИП', ('ип', 'индивидуальный предприниматель')),
    ('AO', ('ао', 'акционерное')),
    ('РГКП', ('ргкп',)),
    ('РГП', ('ргп',)),
    ('ОО', ('оо ', 'общественное')),
)
DEFAULT_LEGAL_TYPE = 'ТОО'


def _legal_type(name: str) -> str:
    """Правовая форма по названию из накладной: «ТОО «Караван»» → ТОО."""

    lowered = (name or '').lower().lstrip(' "«\'')

    for legal_type, prefixes in LEGAL_TYPES:
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return legal_type

    return DEFAULT_LEGAL_TYPE


def _create_supplier(invoice, client) -> dict:
    """Заводит контрагента в UMAG по названию из накладной.

    Без контрагента приёмку не создать, а руками его заводят в кабинете —
    поэтому делаем это сами. Зовётся только из `push`: `preflight` обязан
    оставаться чтением, иначе один взгляд на вкладку «Проверка» плодил бы
    контрагентов.

    БИН не отправляем намеренно: кабинет требует к заполненному БИН ещё и
    юридическое название, а его в накладной обычно нет. Допишет человек.
    """

    name = (invoice.supplier or '').strip()

    if not name:
        return {'name': '', 'agent_id': None, 'agent_name': '', 'candidates': []}

    # Кабинет шлёт контрагента формой, а сам объект — строкой в `agentJson`.
    # На обычный JSON этот адрес отвечает 415.
    created = client.post_form(
        'org/agent/create',
        {'agentJson': {
            'id': None,
            'type': 'SUPPLIER',
            'name': name,
            'bin': '',
            'legalType': _legal_type(name),
            'legalName': '',
            'companyId': '',
            'storeId': '',
            'legalAddress': '',
            'actualAddress': '',
            'phone': '',
            'note': '',
            'isDeleted': False,
            'editTime': '',
        }},
    )

    agent_id = created.get('id') if isinstance(created, dict) else None

    if not agent_id:
        raise UmagError(f'UMAG не вернул контрагента: {str(created)[:200]}')

    agent_name = (created.get('name') or name).strip()

    # Запоминаем сразу: второй раз того же поставщика заводить не нужно.
    SupplierLink.objects.update_or_create(
        store_id=client.store_id,
        name=normalize(name),
        defaults={'agent_id': agent_id, 'agent_name': agent_name},
    )
    logger.info('Завели контрагента «%s» (%s) в магазине %s', agent_name, agent_id, client.store_id)

    return {
        'name': invoice.supplier,
        'agent_id': agent_id,
        'agent_name': agent_name,
        'candidates': [],
    }


def _remember_supplier(invoice, client, agent_id: int) -> None:
    agent = next((item for item in _agents(client) if item['id'] == agent_id), None)

    if agent is None:
        raise UmagError('Такого контрагента нет в UMAG')

    SupplierLink.objects.update_or_create(
        store_id=client.store_id,
        name=normalize(invoice.supplier),
        defaults={'agent_id': agent_id, 'agent_name': (agent.get('name') or '').strip()},
    )


def _agents(client) -> list[dict]:
    body = client.get('org/agent/list', agentType='SUPPLIER', first=0, pageSize=AGENTS_PAGE)
    return (body.get('agents') or []) if isinstance(body, dict) else []


def _match_line(line, client) -> dict:
    """Ищет товар по штрихкоду. Что делать без него, знает `matching`."""

    # Модель иногда дописывает к штрихкоду мусор вроде «шт.» — в UMAG уходят
    # только цифры, иначе запрос падает ещё на их стороне.
    code = re.sub(r'\D', '', line.barcode or '')

    match = {
        'id': line.id,
        'position': line.position,
        'name': line.name,
        'barcode': line.barcode,
        'code': code,
        'quantity': line.quantity,
        'price': line.price,
        # Единица нужна, только если товар придётся заводить в кабинете.
        'unit': line.unit,
        'status': 'ok',
        'product_id': None,
        'product_name': '',
        'selling_price': None,
        'measure': '',
        'stock': None,
        'suggested_barcode': '',
        'suggested_name': '',
        'confidence': None,
    }

    if not line.quantity or not line.price:
        match['status'] = 'no_price'
        return match

    if not code:
        match['status'] = 'no_barcode'
        return match

    try:
        found = client.get('nom/product/findProductByBarcode', barcode=code)
    except UmagError as error:
        if error.status != 422:
            raise

        # Штрихкод с бумаги настоящий — контрольную цифру мы проверили ещё при
        # разборе. Раз его нет в кабинете, это новый товар, и заводить его надо
        # с этим самым кодом, а не приклеивать строку к похожему по названию:
        # чужая карточка — это пересорт, который потом никто не распутает.
        match['status'] = 'new_product'
        return match

    product = found.get('product') or {}
    prices = found.get('productStorePrice') or {}

    match['product_id'] = product.get('id')
    match['product_name'] = product.get('name') or ''
    match['measure'] = matching.unit_for(product.get('measure'))
    match['stock'] = found.get('stockQuantity')
    # Продажную цену возвращаем ту же, что стоит в карточке: приход из
    # накладной — не повод менять цену на полке.
    match['selling_price'] = prices.get('sellingPrice')

    # Строка сошлась по штрихкоду — запоминаем товар в ней самой, а заодно
    # переносим в неё единицу измерения из карточки.
    matching.remember_by_barcode(
        line,
        match['product_id'],
        match['product_name'],
        code,
        match['measure'],
    )
    # Единица — та, что стоит в строке: 1 для штрихкода с бумаги, оценка
    # модели — для штрихкода, который она подставила сама.
    match['confidence'] = line.umag_confidence

    return match


def _create_missing(client, matches: list[dict], agent_id: int | None) -> None:
    """Заводит в кабинете товары, которых там ещё нет.

    Зовётся только из `push`: осмотр обязан оставаться чтением, иначе один
    взгляд на вкладку «Проверка» плодил бы карточки.
    """

    missing = [match for match in matches if match['status'] == 'new_product']

    if not missing:
        return

    category = _category_id(client)

    for match in missing:
        _create_product(client, match, agent_id, category)


def _create_product(client, match: dict, agent_id: int | None, category: int | None) -> None:
    """Карточка товара по штрихкоду из накладной.

    Цену на полке ставим равной приходу: продавать себе в убыток хуже, чем
    продавать без наценки, а настоящую цену магазин выставляет сам. Название и
    единица — те, что прочитаны с бумаги; поправить их человек может в кабинете.
    """

    code = match['code']
    measure = UNITS.get((match.get('unit') or '').strip().lower(), 0)
    arrival = float(match['price'])

    product = {
        'id': None,
        'name': (match['name'] or '')[:255],
        'measure': measure,
        'type': _product_type(code, measure),
        # Код и штрихкод в карточке кабинета — одно и то же значение.
        'code': code,
        'barcode': code,
        'categoryId': category,
    }
    price = {
        'storeId': client.store_id,
        'productId': None,
        'arrivalCost': arrival,
        'sellingPrice': arrival,
        'wholesalePrice': 0,
        'isHiddenOnScale': False,
    }

    form = {'productJson': product, 'productStorePriceJson': price}

    if agent_id:
        form['supplierId'] = agent_id

    client.post_form(CREATE_PRODUCT, form)
    logger.info('Завели товар %s «%s» в магазине %s', code, product['name'], client.store_id)


def _product_type(code: str, measure: int) -> int:
    """Тип карточки: обычный товар, весовой или внутренний весовой код."""

    if len(code) == 13 and code.startswith('2'):
        return INNER_TYPE

    return WEIGHT_TYPE if measure else LINE_TYPE


def _category_id(client) -> int | None:
    """Категория для заводимых товаров.

    Кабинет просит её у человека, но у него же есть «Незаданные» — туда и
    кладём. Не нашлась — отправляем без категории: пусть решает кабинет, это
    не повод не принять накладную.
    """

    try:
        body = client.get(CATEGORIES)
    except UmagError as error:
        logger.warning('UMAG не отдал категории: %s', error)
        return None

    rows = body if isinstance(body, list) else (body or {}).get('categories') or []
    names = {(row.get('name') or '').strip().lower(): row.get('id') for row in rows if isinstance(row, dict)}

    return names.get(DEFAULT_CATEGORY.lower()) or next(iter(names.values()), None)


def _product(match: dict) -> dict:
    """Строка приёмки. `price` UMAG считает сам — из `arrivalCost` и скидки."""

    product = {
        'barcode': int(match['code']),
        'quantity': float(match['quantity']),
        'discount': 0,
        'type': LINE_TYPE,
        'arrivalCost': float(match['price']),
    }

    # Продажную цену шлём только ту, что уже стоит в карточке товара: приход
    # по накладной — не повод трогать цену на полке.
    if match['selling_price'] is not None:
        product['sellingPrice'] = match['selling_price']

    return product


def _line_view(match: dict) -> dict:
    return {
        'id': match['id'],
        'position': match['position'],
        'name': match['name'],
        'barcode': match['barcode'],
        'status': match['status'],
        'product_id': match['product_id'],
        'product_name': match['product_name'],
        'measure': match['measure'],
        'stock': match['stock'],
        'selling_price': match['selling_price'],
        'suggested_barcode': match['suggested_barcode'],
        'suggested_name': match['suggested_name'],
        # У подсказки по названию — насколько модель в ней уверена.
        'confidence': match['confidence'],
    }
