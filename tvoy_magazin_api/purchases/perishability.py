"""Ограничение закупа скоропортящихся товаров.

UMAG не хранит срок годности в карточке. Поэтому уверенные продуктовые группы
получают консервативный срок автоматически, а конкретный штрихкод можно
переопределить в локальной номенклатуре.
"""

from dataclasses import dataclass
from math import ceil

from umag.models import UmagProduct


@dataclass(frozen=True)
class Restriction:
    shelf_life_days: int
    purchase_days: int
    source: str


# Сначала исключения: слово «молоко» есть и у продукта, который хранится
# полгода. Не ограничиваем автоматически то, что явно долго хранится.
STABLE = (
    'ультрапастер',
    'uht',
    'стерилизован',
    'сухое молоко',
    'сухие сливки',
    'консерв',
    'заморож',
    'мороженое',
    'длительного хранения',
)

# Порядок важен: более короткий срок проверяется раньше.
RULES = (
    (
        2,
        (
            'готовая еда',
            'готовые блюда',
            'кулинария',
            'салат готов',
            'суши',
            'роллы',
        ),
    ),
    (
        3,
        (
            'охлажденная рыба',
            'охлаждённая рыба',
            'свежая рыба',
            'охлажденное мясо',
            'охлаждённое мясо',
            'фарш',
            'субпродукт',
            'хлеб',
            'лепешка',
            'лепёшка',
        ),
    ),
    (
        5,
        (
            'выпечка',
            'пирожное',
            'торт',
            'свежевыжат',
        ),
    ),
    (
        7,
        (
            'молочная продукция',
            'молочные продукты',
            'кисломолоч',
            'пастеризован',
            'кефир',
            'айран',
            'сметана',
            'творог',
            'сливки',
            'свежие овощи',
            'свежие фрукты',
            'зелень',
            'ягоды',
        ),
    ),
    (14, ('яйца', 'яйцо куриное')),
)


def for_rows(store_id: int, rows: list[dict]) -> dict[str, Restriction]:
    barcodes = {str(row.get('barcode') or '') for row in rows}
    products = {
        product.barcode: product
        for product in UmagProduct.objects.filter(store_id=store_id)
        if product.barcode in barcodes
    }
    result = {}

    for row in rows:
        barcode = str(row.get('barcode') or '')
        product = products.get(barcode)
        restriction = classify(
            name=(product.name if product else '') or row.get('productName') or '',
            category=(product.category if product else '') or row.get('category') or '',
            subcategory=(product.subcategory if product else '')
            or row.get('subCategory')
            or '',
            shelf_life_days=product.shelf_life_days if product else None,
        )

        if restriction:
            result[barcode] = restriction

    return result


def classify(
    *,
    name: str,
    category: str = '',
    subcategory: str = '',
    shelf_life_days: int | None = None,
) -> Restriction | None:
    """Ручное значение приоритетно; 0 явно отключает ограничение."""

    if shelf_life_days is not None:
        return _restriction(shelf_life_days, 'manual') if shelf_life_days > 0 else None

    text = ' '.join((name, category, subcategory)).lower().replace('ё', 'е')

    if any(marker.replace('ё', 'е') in text for marker in STABLE):
        return None

    for days, markers in RULES:
        if any(marker.replace('ё', 'е') in text for marker in markers):
            return _restriction(days, 'auto')

    return None


def _restriction(shelf_life_days: int, source: str) -> Restriction:
    # Не планируем продажу в последний день срока: нужен запас на приёмку,
    # выкладку и погрешность прогноза.
    buffer = max(1, ceil(shelf_life_days * 0.2))
    purchase_days = max(1, shelf_life_days - buffer)
    return Restriction(shelf_life_days, purchase_days, source)
