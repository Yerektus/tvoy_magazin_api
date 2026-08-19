"""Фоновый разбор накладной.

Очередь тут не заводится: распознавание идёт в обычном потоке, а фронт
опрашивает статус накладной. Для одного магазина этого достаточно; если
загрузок станет много, эта функция — то место, откуда задача уедет в брокер.
"""

import logging
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import close_old_connections, transaction
from django.utils import timezone

from umag import supply

from . import barcodes, preview, suppliers
from .models import Invoice, InvoiceLine
from .openrouter import OpenRouterError, Parsed, parse_invoice

logger = logging.getLogger(__name__)

MAX_LINES = 200

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

        # Снимок готовим до разбора: на упавшую накладную человек всё равно
        # захочет посмотреть глазами.
        prepare_photo(invoice)

        try:
            image, content_type = _for_model(invoice)
            parsed = parse_invoice(image, content_type)

            _save(invoice, parsed)
            # Модель прочитала текст и заодно сказала, где у документа верх, —
            # теперь снимок можно повернуть, чтобы его не смотрели боком.
            _turn_upright(invoice, preview.UPRIGHT_TURNS.get(parsed.data.get('top_edge')))
            # БИН на фото читается не всегда: если поставщик знакомый, берём
            # его из прошлой накладной.
            _fill_supplier(invoice)
            # Позиции уже в базе — сводим их с номенклатурой UMAG, пока накладная
            # числится «распознаётся»: готовой она станет уже сопоставленной.
            _match(invoice)
            _finish(invoice)
        except OpenRouterError as error:
            _fail(invoice, str(error))
        except Exception as error:  # noqa: BLE001 — иначе поток умрёт молча
            logger.exception('Не удалось разобрать накладную %s', invoice_id)
            _fail(invoice, f'Внутренняя ошибка: {error}')


def _for_model(invoice: Invoice) -> tuple[bytes, str]:
    """Чем кормить модель — сжатым снимком накладной.

    `preview` заполнен только у накладных, загруженных когда второй файл ещё
    делался: у них в `image` лежит сырой HEIC, который читают не все модели.
    Новые накладные хранят один снимок, и ветка ниже их не касается.
    """

    if invoice.preview:
        with invoice.preview.open('rb') as jpeg:
            return jpeg.read(), 'image/jpeg'

    with invoice.image.open('rb') as image:
        return image.read(), content_type_for(invoice.image.name)


def prepare_photo(invoice: Invoice) -> bool:
    """Сжимает загруженный снимок, заменяя им сырой файл.

    Снимок на накладную один: сырой оригинал с телефона втрое тяжелее, а
    нужен ровно тем же — и браузеру, и модели, и будущей обучающей выборке.
    Ровно его потом и довернём, когда модель скажет, где у документа верх.

    Возвращает False, если обрабатывать было нечего или не получилось.
    """

    if not preview.needed_for(invoice.image.name):
        return False

    try:
        source = Path(invoice.image.path)
    except (NotImplementedError, ValueError):
        # Хранилище без локальных путей — обрабатывать нечего.
        return False

    jpeg = preview.compress(source)
    if jpeg is None:
        return False

    return _replace_photo(invoice, source, jpeg)


def _turn_upright(invoice: Invoice, degrees) -> None:
    """Доворачивает снимок так, как прочитала модель: боком его никто не смотрит.

    Углы тут кратны 90 — это перестановка пикселей, документ от неё не портится.
    Модель смотрит уже на повёрнутый снимок при следующем разборе и отвечает 0,
    поэтому «распознать заново» второй раз ничего не крутит.
    """

    if degrees not in preview.TURNS:
        return

    try:
        source = Path(invoice.image.path)
    except (NotImplementedError, ValueError):
        return

    turned = preview.upright(source, degrees)
    if turned is None:
        return

    _replace_photo(invoice, source, turned)


def _replace_photo(invoice: Invoice, source: Path, jpeg: bytes) -> bool:
    """Кладёт новый снимок в то же поле и стирает прежний файл."""

    previous = invoice.image.name
    invoice.image.save(f'{source.stem}.jpg', ContentFile(jpeg), save=True)
    invoice.image.storage.delete(previous)

    return True


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
    invoice.issued_at = _date(payload.get('issued_at'))
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


def _match(invoice: Invoice) -> None:
    """Сводит позиции с номенклатурой UMAG, если он подключён.

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
