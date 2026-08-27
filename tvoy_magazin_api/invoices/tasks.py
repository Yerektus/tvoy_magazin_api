"""Фоновый разбор накладной.

Очередь тут не заводится: распознавание идёт в обычном потоке, а фронт
опрашивает статус накладной. Для одного магазина этого достаточно; если
загрузок станет много, эта функция — то место, откуда задача уедет в брокер.
"""

import logging
import threading
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.utils import timezone

from umag import supply

from . import barcodes, known_barcodes, preview, suppliers
from .models import Invoice, InvoiceLine
from .openrouter import OpenRouterError, Parsed, parse_invoice

logger = logging.getLogger(__name__)

MAX_LINES = 200

#: Насколько старой может быть дата на бумаге. Дальше это ошибка чтения года, а
#: не документ: накладную снимают в день приёмки, изредка — позже разбирают
#: пачку за неделю-другую.
OLDEST = timedelta(days=45)

# Сколько накладных разбираем одновременно. Бесплатные модели живут в общем
# пуле и на пять параллельных запросов отвечают 429, поэтому по умолчанию — одна.
_slots = threading.Semaphore(settings.INVOICE_PARSE_CONCURRENCY)


def schedule(invoice: Invoice) -> None:
    """Ставит разбор в фон после того, как транзакция с накладной закоммитится."""

    if settings.INVOICE_PARSE_INLINE:
        # Для тестов и отладки: разбор идёт прямо в запросе, без потока.
        run(invoice.pk)
        return

    transaction.on_commit(
        lambda: threading.Thread(target=_run_in_thread, args=(invoice.pk,), daemon=True).start()
    )


def _run_in_thread(invoice_id: int) -> None:
    """У потока своё подключение к базе — его нужно закрыть за собой."""

    close_old_connections()
    try:
        run(invoice_id)
    finally:
        close_old_connections()


def run(invoice_id: int) -> None:
    try:
        invoice = Invoice.objects.get(pk=invoice_id)
    except Invoice.DoesNotExist:
        return

    # Пока ждём очереди, накладная остаётся «в очереди» — статус меняем внутри.
    with _slots:
        Invoice.objects.filter(pk=invoice_id).update(status=Invoice.Status.PROCESSING)
        # Объект в памяти о смене статуса не знает, а `_save` сохраняет его
        # целиком — иначе накладная вернулась бы в «очередь» до самого конца.
        invoice.status = Invoice.Status.PROCESSING

        # Снимки готовим до разбора: на упавшую накладную человек всё равно
        # захочет посмотреть глазами.
        prepare_photo(invoice)
        for page in invoice.pages.all():
            prepare_photo(page)

        try:
            parsed = parse_invoice(_for_model(invoice))

            _save(invoice, parsed)
            # Модель прочитала текст и заодно сказала, где у документа верх, —
            # теперь снимок можно повернуть, чтобы его не смотрели боком.
            _turn_upright(invoice, preview.UPRIGHT_TURNS.get(parsed.data.get('top_edge')))
            # БИН на фото читается не всегда: если поставщик знакомый, берём
            # его из прошлой накладной.
            _fill_supplier(invoice)
            # Штрихкод модель читает через раз: колонка узкая, печать бледная.
            # Тот же товар уже приходил — берём код из прошлой накладной.
            _fill_barcodes(invoice)
            # Позиции уже в базе — сводим их с номенклатурой UMAG, пока накладная
            # числится «распознаётся»: готовой она станет уже сопоставленной.
            _match(invoice)
            _finish(invoice)
        except OpenRouterError as error:
            _fail(invoice, str(error))
        except Exception as error:  # noqa: BLE001 — иначе поток умрёт молча
            logger.exception('Не удалось разобрать накладную %s', invoice_id)
            _fail(invoice, f'Внутренняя ошибка: {error}')


def _for_model(invoice: Invoice) -> list[tuple[bytes, str]]:
    """Чем кормить модель — сжатыми снимками всех листов накладной, по порядку.

    `preview` заполнен только у накладных, загруженных когда второй файл ещё
    делался: у них в `image` лежит сырой HEIC, который читают не все модели.
    Новые накладные хранят один снимок, и ветка ниже их не касается.
    """

    if invoice.preview:
        with invoice.preview.open('rb') as jpeg:
            first = (jpeg.read(), 'image/jpeg')
    else:
        first = _read(invoice.image)

    return [first, *(_read(page.image) for page in invoice.pages.all())]


def _read(field) -> tuple[bytes, str]:
    with field.open('rb') as image:
        return image.read(), content_type_for(field.name)


def prepare_photo(holder) -> bool:
    """Сжимает загруженный снимок, заменяя им сырой файл.

    `holder` — накладная или её лист: у обоих снимок лежит в поле `image`, и
    делать с ним нужно одно и то же. Копии снимка не держим: сырой оригинал с
    телефона втрое тяжелее, а нужен ровно тем же — и браузеру, и модели, и
    будущей обучающей выборке. Ровно его потом и довернём, когда модель скажет,
    где у документа верх.

    Возвращает False, если обрабатывать было нечего или не получилось.
    """

    if not preview.needed_for(holder.image.name):
        return False

    try:
        source = Path(holder.image.path)
    except (NotImplementedError, ValueError):
        # Хранилище без локальных путей — обрабатывать нечего.
        return False

    jpeg = preview.compress(source)
    if jpeg is None:
        return False

    return _replace_photo(holder, source, jpeg)


def _turn_upright(invoice: Invoice, degrees) -> None:
    """Доворачивает снимок так, как прочитала модель: боком его никто не смотрит.

    Углы тут кратны 90 — это перестановка пикселей, документ от неё не портится.
    Модель смотрит уже на повёрнутый снимок при следующем разборе и отвечает 0,
    поэтому «распознать заново» второй раз ничего не крутит.
    """

    if degrees not in preview.TURNS:
        return

    # Листы одной накладной снимают одинаково, поэтому доворачиваем их на тот
    # же угол: модель называет верх документа, а не каждой страницы отдельно.
    for holder in [invoice, *invoice.pages.all()]:
        try:
            source = Path(holder.image.path)
        except (NotImplementedError, ValueError):
            continue

        turned = preview.upright(source, degrees)
        if turned is not None:
            _replace_photo(holder, source, turned)


def _replace_photo(holder, source: Path, jpeg: bytes) -> bool:
    """Кладёт новый снимок в то же поле и стирает прежний файл."""

    previous = holder.image.name
    holder.image.save(f'{source.stem}.jpg', ContentFile(jpeg), save=True)
    holder.image.storage.delete(previous)
    _make_thumbnail(holder)

    return True


def _make_thumbnail(holder) -> None:
    """Обновляет маленькую копию снимка — ту, что показывает список.

    Только у самой накладной: в списке видно первый лист, а остальные страницы
    открывают уже в просмотрщике. Делается после каждой замены снимка, включая
    доворот, — иначе в списке накладная так и лежала бы боком.
    """

    if not isinstance(holder, Invoice):
        return

    try:
        source = Path(holder.image.path)
    except (NotImplementedError, ValueError):
        return

    small = preview.thumbnail(source)

    if small is None:
        return

    previous = holder.thumbnail.name
    holder.thumbnail.save(f'{source.stem}.jpg', ContentFile(small), save=True)

    if previous:
        holder.thumbnail.storage.delete(previous)


def content_type_for(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith('.png'):
        return 'image/png'
    if lowered.endswith('.webp'):
        return 'image/webp'
    if lowered.endswith(('.heic', '.heif')):
        # Запасной путь: конвертировать оказалось нечем, отправляем как есть.
        return 'image/heic'
    if lowered.endswith('.pdf'):
        return 'application/pdf'
    return 'image/jpeg'


@transaction.atomic
def _save(invoice: Invoice, parsed: Parsed) -> None:
    payload = parsed.data
    lines = [
        _line(invoice, index, line)
        for index, line in enumerate((payload.get('lines') or [])[:MAX_LINES], start=1)
        if isinstance(line, dict)
    ]

    invoice.supplier = _text(payload.get('supplier'), 255)
    invoice.supplier_bin = _text(payload.get('supplier_bin'), 32)
    invoice.number = _text(payload.get('number'), 64)
    invoice.issued_at = _issued_at(payload.get('issued_at'), invoice)
    invoice.total = _total(lines) or _decimal(payload.get('total'))
    invoice.raw_response = payload
    invoice.model = parsed.model
    # Распознали заново — прибавляем к тому, что документ стоил раньше.
    invoice.cost = (invoice.cost or Decimal(0)) + (_decimal(parsed.cost) or Decimal(0))
    invoice.error = ''
    invoice.save()

    invoice.lines.all().delete()
    InvoiceLine.objects.bulk_create(lines)


def _fill_supplier(invoice: Invoice) -> None:
    """Ищет БИН поставщика по названию. Не нашёлся — накладная и так разобрана."""

    try:
        suppliers.fill(invoice)
    except Exception as error:  # noqa: BLE001 — сеть и чужая модель
        logger.warning('Не удалось подобрать БИН для накладной %s: %s', invoice.pk, error)


def _fill_barcodes(invoice: Invoice) -> None:
    """Дописывает штрихкоды по прошлым накладным. Не нашлось — строку сверит
    человек: он и так проходит накладную глазами перед отправкой."""

    try:
        known_barcodes.fill(invoice)
    except Exception as error:  # noqa: BLE001 — подбор не должен ронять разбор
        logger.warning('Не удалось подобрать штрихкоды для накладной %s: %s', invoice.pk, error)


def _match(invoice: Invoice) -> None:
    """Сводит позиции с номенклатурой UMAG, если он подключён.

    Саму приёмку отсюда не заводим: в UMAG накладную отправляет человек
    кнопкой. Здесь только готовим почву, чтобы к моменту, когда он откроет
    карточку, позиции уже были сведены.

    Кабинет недоступен — накладная всё равно распознана: сопоставление
    повторится на вкладке «Проверка», ронять из-за него разбор незачем.
    """

    try:
        supply.match_lines(invoice)
    except Exception as error:  # noqa: BLE001 — чужой сервис, причин упасть много
        logger.warning('Не удалось сопоставить накладную %s с UMAG: %s', invoice.pk, error)


def _finish(invoice: Invoice) -> None:
    """Готово: накладная распознана и сведена с кабинетом."""

    Invoice.objects.filter(pk=invoice.pk).update(
        status=Invoice.Status.DONE,
        processed_at=timezone.now(),
    )


def _line(invoice: Invoice, position: int, line: dict) -> InvoiceLine:
    quantity = _decimal(line.get('quantity'))
    price = _decimal(line.get('price'))
    total = _decimal(line.get('total'))

    # Сумму строки модель то читает, то нет — там, где её просто не напечатали,
    # считаем сами.
    if total is None and quantity is not None and price is not None:
        total = quantity * price

    return InvoiceLine(
        invoice=invoice,
        position=position,
        name=_text(line.get('name'), 255) or 'Без названия',
        barcode=_barcode(line.get('barcode')),
        quantity=quantity,
        unit=_text(line.get('unit'), 32),
        price=price,
        total=total,
    )


def _barcode(value) -> str:
    """Штрихкод, если контрольная цифра сходится. Иначе — пусто.

    Модель нет-нет да и примет за штрихкод номенклатурный номер из соседней
    колонки. Такую строку честнее оставить без кода: её сопоставят по названию,
    а неверный код молча привёл бы в приёмку чужой товар. Прочитанное с фото
    никуда не пропадает — оно остаётся в `raw_response`.
    """

    code = _text(value, 64)

    return code if barcodes.valid(code) else ''


def _total(lines: list[InvoiceLine]):
    """Итог по накладной — сумма строк, а не то, что модель прочла внизу листа.

    Печатное «Итого» остаётся в raw_response: с ним удобно сверяться, когда
    расхождение указывает на плохо распознанную позицию.
    """

    sums = [line.total for line in lines if line.total is not None]
    return sum(sums) if sums else None


def _fail(invoice: Invoice, message: str) -> None:
    Invoice.objects.filter(pk=invoice.pk).update(
        status=Invoice.Status.FAILED,
        error=message[:1000],
        processed_at=timezone.now(),
    )


def _text(value, limit: int) -> str:
    if value is None:
        return ''
    return str(value).strip()[:limit]


def _decimal(value):
    if value is None or value == '':
        return None

    if isinstance(value, str):
        value = value.replace(' ', '').replace(' ', '').replace(',', '.')

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _issued_at(value, invoice: Invoice):
    """Дата документа: с бумаги, а не прочиталась — день, когда его сняли.

    Год модель путает: «27.08.2026» на бледной печати читается как «27.08.2020»,
    и такую накладную кабинет не принимает — приход раньше проведённой
    инвентаризации он отклоняет. Отличить опечатку модели от настоящей старой
    накладной по самому числу нельзя, но накладную снимают в день приёмки, и
    дата из позапрошлой пятилетки — это ошибка чтения, а не документ.

    Границы широкие: месяц назад — обычное дело (бумагу нашли в пачке), а
    завтрашним числом накладные не выписывают.
    """

    scanned = timezone.localdate(invoice.created_at or timezone.now())
    issued = _date(value)

    if issued is None or not scanned - OLDEST <= issued <= scanned:
        return scanned

    return issued


def _date(value):
    if not value:
        return None

    text = str(value).strip()
    for pattern in ('%Y-%m-%d', '%d.%m.%Y', '%d.%m.%y'):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue

    return None
