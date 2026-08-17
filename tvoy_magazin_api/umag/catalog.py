"""Номенклатура магазина у нас под рукой: выгрузка и поиск по ней.

Кабинет ищет товар подстрокой, и на этом всё ломается: карточка называется
короче накладной, слова стоят в другом порядке, регистр другой. Поэтому
номенклатуру выгружаем целиком и ищем сами — нечётко, по общим словам.

Источник — `nom/product/report`: под этим адресом в кабинете лежит страница
«все товары». Запрос без единого параметра отдаёт весь магазин разом — 35
тысяч карточек за четыре секунды. Номера товара там нет, но для поиска хватает
названия и штрихкода: как только штрихкод попал в строку накладной, карточка
находится уже по нему.
"""

import logging
import re
import threading
from datetime import timedelta

from django.db import close_old_connections, transaction
from django.utils import timezone

from .client import UmagClient, UmagError
from .models import UmagProduct

logger = logging.getLogger(__name__)

# Магазины, которые обновляются прямо сейчас: выгрузка тяжёлая, второй поток
# на тот же магазин не нужен.
_busy: set[int] = set()
_busy_lock = threading.Lock()

# Та самая страница «все товары» из кабинета: GET без параметров.
ALL_PRODUCTS = 'nom/product/report'

# Сколько кандидатов отдаём модели: больше — дороже и без пользы.
LIMIT = 12

# Столько слов спрашиваем у кабинета, когда ищем там: каждое — свой запрос.
QUERIES = 3

# Ниже этого сходства названия считаем разными товарами. Порог невысокий
# намеренно: выбирает всё равно модель, наше дело — не потерять нужное.
SIMILAR = 0.34

# Слова короче ищут пол-магазина: «шт», «гр», «1 л». А вот трёхбуквенные —
# это уже товар: «сыр», «нан», «сок», «чай».
WORD = 3

# По скольким первым буквам считаем слова одинаковыми: окончания в накладной
# и в кабинете расходятся («булочки» против «булочка»), начало — нет.
KEY = 4

# Фасовка поставщика и вес: в карточке кабинета их обычно нет.
NOISE = re.compile(r'\b\d+\s*[xх*]\s*\d+\b|\b\d+\s*(?:гр|г|кг|мл|л|шт|уп|кор|пач)\b|\b\d+[.,]\d+\b')


def normalize(name: str) -> str:
    """Название в виде, пригодном для сравнения.

    Убираем фасовку («1*14», «450гр»), знаки и лишние пробелы: в накладной
    пишут «Пряник шоколадный 450гр Сайрам нан 1*14», а в кабинете лежит
    «Пряник шоколадный сайрам нан».
    """

    lowered = (name or '').lower().replace('ё', 'е')
    without_noise = NOISE.sub(' ', lowered)

    return re.sub(r'\s+', ' ', re.sub(r'[^0-9a-zа-я]+', ' ', without_noise)).strip()


def refresh_later(account) -> None:
    """Обновляет копию в фоне, если она устарела. Расписания снаружи не нужно.

    Разбор накладной ждать выгрузки не должен: полминуты на шесть тысяч
    товаров. Поэтому обновляемся после того, как накладная разобрана, — эта
    успеет по старой копии, а следующая пойдёт уже по свежей.

    Два потока одного магазина сюда не пустит замок: выгрузка тяжёлая, и
    делать её дважды подряд незачем.
    """

    store_id = account.store_id

    if not store_id or not stale(store_id):
        return

    with _busy_lock:
        if store_id in _busy:
            return

        _busy.add(store_id)

    def work() -> None:
        close_old_connections()

        try:
            refresh(UmagClient(account, store_id), store_id)
        except Exception as error:  # noqa: BLE001 — чужой сервис, причин упасть много
            logger.warning('Не удалось обновить номенклатуру магазина %s: %s', store_id, error)
        finally:
            close_old_connections()

            with _busy_lock:
                _busy.discard(store_id)

    threading.Thread(target=work, daemon=True).start()


def refresh(client, store_id: int) -> int:
    """Перекладывает номенклатуру магазина к себе. Возвращает число товаров."""

    rows = _fetch(client)

    if not rows:
        return 0

    products = {}

    for row in rows:
        barcode = str(row.get('barcode') or '').strip()
        name = (row.get('name') or '').strip()

        # Без штрихкода карточку в строку не подставить, без названия — не найти.
        if barcode and name:
            products[barcode] = UmagProduct(
                store_id=store_id,
                barcode=barcode[:64],
                name=name[:255],
                measure=(row.get('measure') or '').strip()[:32],
                search_name=normalize(name)[:255],
            )

    with transaction.atomic():
        # Товар могли удалить в кабинете — держим ровно то, что там сейчас.
        UmagProduct.objects.filter(store_id=store_id).delete()
        UmagProduct.objects.bulk_create(list(products.values()), batch_size=500)

    logger.info('Номенклатура магазина %s обновлена: %s товаров', store_id, len(products))

    return len(products)


def _fetch(client) -> list[dict]:
    """Все товары магазина одним запросом — как на одноимённой странице."""

    try:
        body = client.get(ALL_PRODUCTS)
    except UmagError as error:
        logger.warning('UMAG не отдал номенклатуру: %s', error)
        return []

    return body if isinstance(body, list) else (body or {}).get('data') or []


def lookup(client, store_id: int, name: str, limit: int = LIMIT) -> list[dict]:
    """Ищет товар в самом кабинете — когда в копии его не оказалось.

    Копия собрана из товарного отчёта, а он отдаёт только то, что двигалось за
    три месяца. Залежавшийся товар в него не попадает: карточки «Meter 50гр» в
    кабинете есть, а в отчёте их нет, и строка накладной оставалась ни с чем.

    Кабинет ищет подстрокой и не сортирует — по слову «клубника» вернутся
    любые двадцать клубник. Поэтому спрашиваем по каждому слову, а порядок
    наводим сами. Найденное дописываем в копию: в следующий раз хватит её.
    """

    wanted = normalize(name)
    words = [word for word in wanted.split() if len(word) >= WORD]

    if not words:
        return []

    found: dict[str, str] = {}

    for word in words[:QUERIES]:
        try:
            body = client.get('nom/product/by-part', namePart=word, limit=limit * 2, productTypes=0)
        except UmagError as error:
            logger.warning('UMAG не нашёл товары по «%s»: %s', word, error)
            continue

        for product in body if isinstance(body, list) else []:
            barcode = str(product.get('barcode') or '')
            product_name = (product.get('name') or '').strip()

            if barcode and product_name:
                found[barcode] = product_name

    if not found:
        return []

    _save_found(store_id, found)

    scored = sorted(
        (
            (similarity(wanted, normalize(product_name)), barcode, product_name)
            for barcode, product_name in found.items()
        ),
        key=lambda item: -item[0],
    )

    return [
        {'id': index, 'name': product_name, 'barcode': barcode, 'measure': ''}
        for index, (score, barcode, product_name) in enumerate(scored[:limit], start=1)
        if score >= SIMILAR
    ]


def _save_found(store_id: int, products: dict[str, str]) -> None:
    """Дописывает в копию то, что нашлось живьём: она сама себя достраивает."""

    UmagProduct.objects.bulk_create(
        [
            UmagProduct(
                store_id=store_id,
                barcode=barcode[:64],
                name=name[:255],
                search_name=normalize(name)[:255],
            )
            for barcode, name in products.items()
        ],
        ignore_conflicts=True,
        batch_size=100,
    )


def stale(store_id: int, days: int = 1) -> bool:
    """Пора ли обновлять копию номенклатуры этого магазина.

    Пустая копия тоже устаревшая: магазин ещё ни разу не выгружали.
    """

    last = (
        UmagProduct.objects.filter(store_id=store_id)
        .order_by('-updated_at')
        .values_list('updated_at', flat=True)
        .first()
    )

    return last is None or last < timezone.now() - timedelta(days=days)


def finder(store_id: int, fresh_days: int = 7):
    """Поиск по нашей копии номенклатуры. Копии нет или она старая — `None`.

    Устаревшей копией пользоваться можно, а вот молча подсовывать
    прошлогоднюю — нет: пусть тогда работает поиск в самом кабинете.
    """

    products = UmagProduct.objects.filter(
        store_id=store_id,
        updated_at__gte=timezone.now() - timedelta(days=fresh_days),
    ).values_list('barcode', 'name', 'measure', 'search_name')

    rows = list(products)

    return Finder(rows) if rows else None


class Finder:
    """Нечёткий поиск по названиям: слово в общем — уже повод сравнивать.

    Сравнивать строку накладной со всеми шестью тысячами карточек дорого,
    поэтому сначала отбираем по общему слову, а считаем сходство уже на
    десятках оставшихся.
    """

    def __init__(self, rows: list[tuple]):
        self.rows = rows
        self.by_word: dict[str, list[int]] = {}

        for position, (_, _, _, search_name) in enumerate(rows):
            for word in self._words(search_name):
                self.by_word.setdefault(word, []).append(position)

    def find(self, name: str, limit: int = LIMIT) -> list[dict]:
        """Товары, похожие названием на строку накладной."""

        wanted = normalize(name)
        words = self._words(wanted)

        if not words:
            return []

        seen: set[int] = set()

        for word in words:
            seen.update(self.by_word.get(word, ()))

        scored = []

        for position in seen:
            barcode, product_name, measure, search_name = self.rows[position]
            score = similarity(wanted, search_name)

            if score >= SIMILAR:
                scored.append((score, barcode, product_name, measure))

        scored.sort(key=lambda item: -item[0])

        return [
            {'id': index, 'name': product_name, 'barcode': barcode, 'measure': measure}
            for index, (_, barcode, product_name, measure) in enumerate(scored[:limit], start=1)
        ]

    @staticmethod
    def _words(text: str) -> set[str]:
        """Слова, по которым имеет смысл искать: короткие есть в каждом товаре."""

        return {word[:KEY] for word in text.split() if len(word) >= WORD}


def similarity(first: str, second: str) -> float:
    """Сходство названий по общим словам — оно устойчивее посимвольного.

    «Коржик Ромашка Сайрам нан» и «КОРЖИК сайрам нан» посимвольно расходятся
    заметно, а по словам совпадают на две трети.

    Считаем в обе стороны сразу. Только доля от карточки не годится: карточка
    «мармелад» из одного слова совпала бы с любой мармеладной строкой на все
    сто. Только доля от строки — тоже: длинная карточка, где случайно есть
    нужное слово, обгонит короткую и точную. Поэтому берём их среднее
    гармоническое: высоко оказывается то, что похоже с обеих сторон.
    """

    left = Finder._words(first)
    right = Finder._words(second)
    common = len(left & right)

    if not common:
        return 0.0

    covers_line = common / len(left)
    covers_card = common / len(right)

    return 2 * covers_line * covers_card / (covers_line + covers_card)
