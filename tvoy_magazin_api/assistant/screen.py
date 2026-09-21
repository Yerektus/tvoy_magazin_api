"""Фильтры кабинета, которые аналитик выставляет по просьбе человека.

Это не запись в магазин: данные не меняются. Ручка собирает безопасный адрес
страницы, а кабинет сам ставит поля. Путь уходит в чат скрытым блоком — как
следующие вопросы, — и приложение по нему переходит.

Чужой адрес сюда не попадёт: разрешены только товары и продажи, и только свои
ключи фильтров. Модель путь не пишет — его подставляет сервер после вызова.
"""

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode

from django.utils import timezone

#: Как у остальных выборок аналитика: глубже года не смотрим.
MAX_DAYS = 365
MAX_QUERY = 120
MAX_PATH = 400

PRODUCTS = 'products'
SALES = 'sales'
PAGES = (PRODUCTS, SALES)

PRODUCT_KEYS = frozenset(
    {'q', 'barcode', 'last_from', 'last_to', 'sold_from', 'sold_to', 'accuracy'}
)
SALES_KEYS = frozenset({'from', 'to'})
ACCURACY = frozenset({'high', 'medium', 'low', 'none'})

SCREEN_MARK = 'экран'


class Screen:
    """Куда перейти. В JSON модели — сводка, путь приложению кладёт сервер."""

    def __init__(self, path: str, summary: dict):
        self.path = path
        self.summary = summary


def set_filters(
    user,
    page='',
    date_from='',
    date_to='',
    days=None,
    sold_from=None,
    sold_to=None,
    only_sold=False,
    query='',
    barcode='',
    accuracy='',
) -> Screen | dict:
    """Собирает фильтры страницы. Пустые параметры — сброс."""

    del user
    page = _page(page, sold_from, sold_to, only_sold, query, barcode, accuracy)
    start, end = _dates(date_from, date_to, days)
    sold = _bounds(sold_from, sold_to, only_sold)
    name = str(query or '').strip()[:MAX_QUERY]
    code = str(barcode or '').strip()[:MAX_QUERY]
    levels = _accuracy(accuracy)

    if page == SALES:
        if start is None and end is None:
            return _screen(f'/{SALES}', {})

        if start is None:
            start = end
        if end is None:
            end = timezone.localdate()

        params = {'from': start.isoformat(), 'to': end.isoformat()}
        return _screen(_path(f'/{SALES}', params), params)

    params = {}

    if name:
        params['q'] = name
    if code:
        params['barcode'] = code
    if start is not None:
        params['last_from'] = start.isoformat()
    if end is not None:
        params['last_to'] = end.isoformat()
    if sold[0] is not None:
        params['sold_from'] = _qty_text(sold[0])
    if sold[1] is not None:
        params['sold_to'] = _qty_text(sold[1])
    if levels:
        params['accuracy'] = ','.join(levels)

    return _screen(_path(f'/{PRODUCTS}', params), params)


def split_screen(text: str) -> tuple[str, str | None]:
    """Достаёт путь из ответа и проверяет, что его безопасно открывать."""

    text = (text or '').replace('\r\n', '\n')
    match = re.search(
        rf'(?:\n|^)<<<{SCREEN_MARK}[ \t]*\n(/[^\n]+)\s*>>>[ \t]*',
        text,
        re.IGNORECASE,
    )

    if not match:
        return text.strip(), None

    path = match.group(1).strip()
    clean = (text[: match.start()] + text[match.end() :]).strip()

    return clean, path if is_safe(path) else None


def is_safe(path: str) -> bool:
    """Только товары и продажи, только свои ключи, без чужих адресов."""

    path = (path or '').strip()

    if not path or len(path) > MAX_PATH or '\n' in path or '//' in path:
        return False

    route, sep, query = path.partition('?')

    if route not in (f'/{PRODUCTS}', f'/{SALES}'):
        return False

    if not sep:
        return True

    allowed = SALES_KEYS if route == f'/{SALES}' else PRODUCT_KEYS

    try:
        pairs = parse_qsl(query, keep_blank_values=False, strict_parsing=True)
    except ValueError:
        return False

    if not pairs:
        return False

    seen: set[str] = set()

    for key, value in pairs:
        if key not in allowed or key in seen or not value or len(value) > MAX_QUERY:
            return False

        seen.add(key)

    return True


def _screen(path: str, filters: dict) -> Screen:
    return Screen(
        path,
        {
            'готово': True,
            'страница': path,
            'фильтры': filters,
            'подсказка': (
                'Кабинет выставит фильтры сам. В ответе коротко скажи, что стоит. '
                'Ссылку и путь не пиши.'
            ),
        },
    )


def _path(route: str, params: dict) -> str:
    query = urlencode(params)

    return f'{route}?{query}' if query else route


def _page(page, sold_from, sold_to, only_sold, query, barcode, accuracy) -> str:
    name = str(page or '').strip().lower()

    if _flag(only_sold) or sold_from not in (None, '') or sold_to not in (None, ''):
        return PRODUCTS

    if str(query or '').strip() or str(barcode or '').strip() or str(accuracy or '').strip():
        return PRODUCTS

    return name if name in PAGES else PRODUCTS


def _dates(date_from, date_to, days) -> tuple[date | None, date | None]:
    start = _day(date_from)
    end = _day(date_to)

    if start is None and end is None:
        span = _span(days)

        if span is not None:
            return span

    if start is not None and end is not None and start > end:
        start, end = end, start

    return start, end


def _span(days) -> tuple[date, date] | None:
    if days in (None, ''):
        return None

    try:
        count = int(days)
    except (TypeError, ValueError):
        return None

    count = max(1, min(count, MAX_DAYS))
    today = timezone.localdate()

    return today - timedelta(days=count - 1), today


def _day(value) -> date | None:
    text = str(value or '').strip()[:10]

    if len(text) != 10:
        return None

    try:
        found = date.fromisoformat(text)
    except ValueError:
        return None

    today = timezone.localdate()
    oldest = today - timedelta(days=MAX_DAYS)

    if found > today:
        return today

    if found < oldest:
        return oldest

    return found


def _bounds(sold_from, sold_to, only_sold) -> tuple[Decimal | None, Decimal | None]:
    low = _qty(sold_from)
    high = _qty(sold_to)

    if _flag(only_sold) and low is None:
        low = Decimal(1)

    if low is not None and high is not None and low > high:
        low, high = high, low

    return low, high


def _qty(value) -> Decimal | None:
    if value in (None, ''):
        return None

    text = str(value).strip().replace(' ', '').replace(',', '.')

    try:
        number = Decimal(text)
    except InvalidOperation:
        return None

    if number < 0:
        return Decimal(0)

    return number.normalize()


def _qty_text(value: Decimal) -> str:
    text = format(value, 'f')

    if '.' in text:
        text = text.rstrip('0').rstrip('.')

    return text or '0'


def _accuracy(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value]
    else:
        parts = [part.strip() for part in str(value or '').split(',')]

    found = []
    seen: set[str] = set()

    for part in parts:
        if part in ACCURACY and part not in seen:
            seen.add(part)
            found.append(part)

    return found


def _flag(value) -> bool:
    if isinstance(value, bool):
        return value

    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'да'}
