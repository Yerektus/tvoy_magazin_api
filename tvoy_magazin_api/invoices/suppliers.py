"""БИН поставщика по названию — из прошлых накладных.

В UMAG у контрагентов поле БИН есть, но не заполнено ни у кого: в кабинете
магазина 0 из 614 поставщиков с БИН. Поэтому единственный источник — наши же
разобранные накладные: тот же поставщик уже приезжал, и тогда БИН с бумаги
прочитался.

Сначала ищем точное совпадение по названию, потом спрашиваем модель:
«ТОО ЖЕТЫСУ-ТРЕЙД» и «Товарищество с ограниченной ответственностью
"Жетысу Трейд"» — один и тот же поставщик, а «Жетысу Сут» — уже другой.
"""

import logging
from decimal import Decimal

from umag.supply import normalize

from .models import Invoice
from .openrouter import OpenRouterError, match_supplier

logger = logging.getLogger(__name__)

# Ниже этого модель сама себе не верит: чужой БИН уедет в бухгалтерию.
CONFIDENCE = 0.8

# Сколько поставщиков показываем модели: длиннее список — дороже и без толку.
CANDIDATES = 25


def fill(invoice) -> None:
    """Дописывает БИН, если с бумаги он не прочитался, а название есть."""

    if invoice.supplier_bin or not invoice.supplier:
        return

    known = _known(invoice)

    if not known:
        return

    name = normalize(invoice.supplier)
    exact = next((row for row in known if normalize(row['name']) == name), None)

    if exact:
        _save(invoice, exact)
        return

    chosen = _guess(invoice, known)

    if chosen:
        _save(invoice, chosen)


def _known(invoice) -> list[dict]:
    """Поставщики с БИН из прошлых накладных той же организации.

    Не «того же сотрудника»: поставщик один на магазин, и БИН, однажды
    прочитанный сменщиком, годится и накладной, которую завёл хозяин.
    """

    rows = (
        Invoice.objects.filter(organization=invoice.organization_id)
        .exclude(pk=invoice.pk)
        .exclude(supplier='')
        .exclude(supplier_bin='')
        # Свежие важнее: у поставщика мог смениться БИН.
        .order_by('-created_at')
        .values_list('supplier', 'supplier_bin')
    )

    seen: dict[str, dict] = {}

    for name, code in rows:
        key = normalize(name)

        if key and key not in seen:
            seen[key] = {'name': name, 'bin': code}

    return list(seen.values())[:CANDIDATES]


def _guess(invoice, known: list[dict]) -> dict | None:
    """Спрашивает модель, кто из знакомых поставщиков привёз эту накладную."""

    candidates = [{'id': index, 'name': row['name']} for index, row in enumerate(known)]

    try:
        answer = match_supplier(invoice.supplier, candidates)
    except OpenRouterError as error:
        logger.warning('Модель не узнала поставщика накладной %s: %s', invoice.pk, error)
        return None

    _charge(invoice, answer.cost)

    data = answer.data if isinstance(answer.data, dict) else {}
    chosen = _int(data.get('id'))

    if chosen is None or not 0 <= chosen < len(known):
        return None

    return known[chosen] if _confidence(data.get('confidence')) >= CONFIDENCE else None


def _save(invoice, supplier: dict) -> None:
    invoice.supplier_bin = supplier['bin'][:32]
    invoice.supplier_bin_auto = True
    invoice.save(update_fields=('supplier_bin', 'supplier_bin_auto'))


def _charge(invoice, cost) -> None:
    """Запрос платный — он тоже идёт в стоимость документа."""

    if not cost:
        return

    invoice.cost = (invoice.cost or Decimal(0)) + Decimal(str(cost))
    invoice.save(update_fields=('cost',))


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
