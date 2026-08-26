"""Штрихкод по названию — из прошлых накладных той же организации.

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
"""

from difflib import SequenceMatcher

from . import barcodes
from .models import InvoiceLine

#: Насколько названия должны совпасть, чтобы штрихкод считался тем же. Подобрано
#: по накладным: 0.9 склеивает «450гр» с «450 гр», но разводит «ваниль» и
#: «шоколад» — они отличаются сильнее.
SIMILARITY = 0.9

#: Сколько прошлых строк держим в памяти за раз. Больше — дольше сравнивать, а
#: свежие всё равно полезнее: у товара мог смениться штрихкод.
KNOWN = 2000


def fill(invoice) -> None:
    """Дописывает штрихкоды строкам, у которых их не прочитали с бумаги."""

    empty = [line for line in invoice.lines.all() if not line.barcode]

    if not empty:
        return

    known = _known(invoice)

    if not known:
        return

    filled = []

    for line in empty:
        code = _lookup(normalize(line.name), known)

        if code:
            line.barcode = code
            line.barcode_auto = True
            filled.append(line)

    if filled:
        InvoiceLine.objects.bulk_update(filled, ('barcode', 'barcode_auto'))


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
