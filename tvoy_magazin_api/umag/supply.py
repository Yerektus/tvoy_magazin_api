"""Распознанная накладная → черновик приёмки в UMAG.

Черновик — это приёмка, для которой не позвали `provide`. Проводит её человек
руками в кабинете: это движение по складу и деньгам, и решать за него нельзя.
"""

import logging
import re
from datetime import datetime, time

from django.utils import timezone

from . import matching
from .client import UmagClient, UmagError
from .models import SupplierLink, UmagAccount

logger = logging.getLogger(__name__)

# Контрагентов у магазина шесть сотен — забираем одним запросом и ищем на месте.
AGENTS_PAGE = 1000

# Обычная строка приёмки. Бонусные UMAG держит отдельным списком.
LINE_TYPE = 0


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

    supplier, matches, problems = _inspect(invoice, client)

    if problems:
        raise NotReady('; '.join(problems))

    supply_id = _create_draft(client)

    try:
        _set_header(client, supply_id, invoice, supplier['agent_id'])
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

    Ходит в кабинет от имени того, кто загрузил накладную: штрихкоды с бумаги
    превращаются в карточки товаров, а строкам без штрихкода его подбирает
    модель. К моменту, когда человек откроет накладную, позиции уже сведены.
    """

    account = UmagAccount.objects.filter(user=invoice.created_by).first()

    # UMAG не подключён или магазин не выбран — сопоставлять не с чем.
    if account is None or not account.ready:
        return

    client = UmagClient(account, invoice.umag_store_id)
    matches = [_match_line(line, client) for line in invoice.lines.all()]
    matching.suggest(invoice, matches, client)


def normalize(name: str) -> str:
    """«ТОО «Жасар-Сауда»» и «жасар сауда» — один и тот же поставщик."""

    lowered = re.sub(r'\b(тоо|ип|ао|кх|тов)\b', ' ', (name or '').lower())
    return re.sub(r'\s+', ' ', re.sub(r'[^0-9a-zа-яё]+', ' ', lowered)).strip()


def _inspect(invoice, client) -> tuple[dict, list[dict], list[str]]:
    """Один проход по накладной: поставщик, строки и список претензий."""

    supplier = _match_supplier(invoice, client)
    matches = [_match_line(line, client) for line in invoice.lines.all()]

    # Строки без штрихкода достаются модели: уверенный выбор она впишет сама.
    filled = matching.suggest(invoice, matches, client)

    if filled:
        # Штрихкод появился — перечитываем строку из UMAG, чтобы в ответе была
        # её карточка с ценой и остатком, а статус стал обычным «ok».
        lines = {line.pk: line for line in invoice.lines.filter(pk__in=filled)}
        matches = [
            _match_line(lines[match['id']], client) if match['id'] in lines else match
            for match in matches
        ]

    problems = []

    if not supplier['agent_id']:
        problems.append(
            'Выберите поставщика в UMAG'
            if supplier['candidates']
            else f'Поставщика «{invoice.supplier or "без названия"}» нет в UMAG — заведите контрагента'
        )

    if not matches:
        problems.append('В накладной нет позиций')

    stuck = [match for match in matches if match['status'] != 'ok']
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


def _match_supplier(invoice, client) -> dict:
    """Ищет контрагента: сперва прошлый выбор, потом одноимённые кандидаты."""

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

    return {
        'name': invoice.supplier,
        'agent_id': chosen['id'] if chosen else None,
        'agent_name': chosen['name'] if chosen else '',
        'candidates': candidates,
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

        match['status'] = 'unknown_barcode'
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
