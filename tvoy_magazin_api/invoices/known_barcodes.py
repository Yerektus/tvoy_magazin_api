"""Штрихкод по названию: сперва свои накладные, потом номенклатура UMAG.

Раньше строку без штрихкода отдавали кабинету UMAG: там искали товар по куску
названия и просили модель выбрать подходящий. Выходило дорого и мимо — в
кабинете тысячи карточек, половина из них похожа названием, и «Сырок Чудо
ваниль 5%» приклеивался к «Сырок Чудо шоколад 5%». Пересорт в приёмке хуже
пустой строки.

Свои накладные врут меньше. Тот же поставщик привозит тот же товар и печатает
его одной и той же строкой месяц за месяцем: если в прошлой накладной у этой
строки уже стоял штрихкод — проверенный человеком перед отправкой в UMAG, — то
это и есть ответ, без модели и без сети.

Ищем сперва точное совпадение нормализованного названия, потом близкое: между
«Пряник шоколадный 450гр» и «Пряник шоколадный 450 гр» разница только в
пробеле, и терять на ней целый товар глупо. Порог высокий — соседние вкусы
одного производителя отличаются одним словом, и ниже него мы промахнёмся
ровно так же, как промахивался UMAG.

Чего нет в своих накладных, ищем в копии номенклатуры кабинета: товар может
приходить впервые, но в магазине он давно заведён. Кабинет тут не при чём —
копия лежит у нас, поиск идёт без сети. Условие приёмки строже, чем для своих
накладных: карточку берём, только если она заметно ближе прочих, иначе
«Сырок Чудо ваниль» уедет на карточку «Сырок Чудо шоколад».
"""

from difflib import SequenceMatcher

from umag import catalog
from umag.models import UmagAccount

from . import barcodes
from .models import InvoiceLine

#: Насколько названия должны совпасть, чтобы штрихкод считался тем же. Подобрано
#: по накладным: 0.9 склеивает «450гр» с «450 гр», но разводит «ваниль» и
#: «шоколад» — они отличаются сильнее.
SIMILARITY = 0.9

#: Сколько прошлых строк держим в памяти за раз. Больше — дольше сравнивать, а
#: свежие всё равно полезнее: у товара мог смениться штрихкод.
KNOWN = 2000

#: Насколько карточка кабинета должна совпасть с названием строки. Выше, чем у
#: своих накладных: там строку печатал тот же поставщик тем же словом, а в
#: номенклатуре рядом лежат все вкусы и фасовки одного товара.
CATALOG = 0.75

#: На сколько лучший кандидат должен опережать второго. Идут вплотную — значит
#: они различаются мелочью вроде вкуса, и выбирать за человека нельзя.
GAP = 0.08


def fill(invoice) -> None:
    """Дописывает штрихкоды строкам, у которых их не прочитали с бумаги."""

    empty = [line for line in invoice.lines.all() if not line.barcode]

    if not empty:
        return

    # Пусто — не повод останавливаться: строку ещё может узнать номенклатура
    # кабинета, а первые накладные организации приходят как раз без истории.
    known = _known(invoice)
    filled = []
    missed = []

    for line in empty:
        code = _lookup(normalize(line.name), known)

        if code:
            line.barcode = code
            line.barcode_auto = True
            filled.append(line)
        else:
            missed.append(line)

    filled.extend(_from_catalog(invoice, missed))

    if filled:
        InvoiceLine.objects.bulk_update(filled, ('barcode', 'barcode_auto'))


def _from_catalog(invoice, lines: list) -> list:
    """Штрихкоды из копии номенклатуры кабинета — для того, чего у нас не было.

    Копия обновляется раз в сутки и лежит у нас, так что поиск ничего не стоит.
    Нет её или магазин неизвестен — просто возвращаем пусто: строку сверит
    человек.
    """

    if not lines:
        return []

    store_id = invoice.umag_store_id or _store_of(invoice)
    search = catalog.finder(store_id) if store_id else None

    if search is None:
        return []

    filled = []

    for line in lines:
        code = _best(search, line.name)

        if code:
            line.barcode = code
            line.barcode_auto = True
            filled.append(line)

    return filled


def _store_of(invoice) -> int | None:
    """Магазин накладной. У старых он не записан — берём выбранный сейчас."""

    account = UmagAccount.objects.filter(user=invoice.created_by).first()

    return account.store_id if account else None


def _best(search, name: str) -> str:
    """Карточка, которая точно про этот товар. Сомнительную не берём."""

    wanted = catalog.normalize(name)
    scored = sorted(
        (
            (catalog.similarity(wanted, catalog.normalize(found['name'])), found)
            for found in search.find(name)
        ),
        key=lambda pair: -pair[0],
    )

    if not scored or scored[0][0] < CATALOG:
        return ''

    # Второй кандидат вплотную — значит различаются вкусом или фасовкой, а это
    # разные товары. Пусть человек выберет сам.
    if len(scored) > 1 and scored[0][0] - scored[1][0] < GAP:
        return ''

    code = str(scored[0][1].get('barcode') or '').strip()

    return code if barcodes.valid(code) else ''


def normalize(name: str) -> str:
    """«Сырок ЧУДО 5%» и «сырок чудо 5 %» — одна и та же строка."""

    kept = [char if char.isalnum() else ' ' for char in (name or '').lower()]

    return ' '.join(''.join(kept).split())


def _known(invoice) -> dict[str, str]:
    """Названия со штрихкодами из прошлых накладных организации.

    Не «того же сотрудника»: товар один на магазин, и код, однажды сверенный
    сменщиком, годится накладной, которую завёл хозяин.

    Берём и подставленные раньше строки тоже: их человек видел перед отправкой
    в UMAG и мог поправить, а значит они не хуже прочитанных с бумаги.
    """

    rows = (
        InvoiceLine.objects.filter(
            invoice__organization=invoice.organization_id,
            invoice__deleted_at__isnull=True,
        )
        .exclude(invoice=invoice)
        .exclude(barcode='')
        # Свежие важнее: у товара мог смениться штрихкод.
        .order_by('-invoice__created_at', '-id')
        .values_list('name', 'barcode')[:KNOWN]
    )

    known: dict[str, str] = {}

    for name, code in rows:
        key = normalize(name)

        if key and key not in known and barcodes.valid(code):
            known[key] = code

    return known


def _lookup(name: str, known: dict[str, str]) -> str:
    """Штрихкод знакомой строки. Ничего похожего — пусто."""

    if not name:
        return ''

    exact = known.get(name)

    if exact:
        return exact

    best = ''
    score = SIMILARITY

    for candidate, code in known.items():
        # Длины разъехались вдвое — сравнивать посимвольно уже незачем: до
        # порога такой паре не дотянуться, а вызовов экономит много.
        if not 0.5 <= len(candidate) / len(name) <= 2:
            continue

        ratio = SequenceMatcher(None, name, candidate).ratio()

        if ratio > score:
            best, score = code, ratio

    return best
