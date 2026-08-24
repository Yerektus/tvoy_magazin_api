"""Чем аналитик смотрит данные.

Модель не ходит в базу и не пишет запросов. Она вызывает ручки из этого файла,
а они уже сами отбирают данные — **всегда** по организации того, кто спрашивает.
Организацию модель не передаёт и передать не может: её подставляет сервер.

Отсюда три правила, которые нельзя нарушать:

1. Только чтение. Ни одна ручка ничего не меняет и не удаляет.
2. Отбор по организации ставится здесь, а не приходит из ответа модели.
3. У каждой ручки есть потолок по числу строк: миллион токенов контекста не
   повод отдавать всю базу, а счёт за это платим мы.
"""

from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from invoices.models import Invoice, InvoiceLine
from purchases.models import PurchasePlan

from . import cabinet

#: Больше строк за раз не отдаём никогда — ни по просьбе модели, ни случайно.
MAX_ROWS = 50

#: И не смотрим глубже: за год ассортимент меняется целиком.
MAX_DAYS = 365


def since(days) -> timezone.datetime:
    """Начало периода. Чужие числа приводим к своим границам, а не верим им."""

    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 30

    return timezone.now() - timedelta(days=max(1, min(days, MAX_DAYS)))


def _invoices(user):
    """Накладные организации — основа всех выборок.

    Удалённые не показываем: для человека их нет, значит нет и для аналитика.
    """

    return Invoice.objects.filter(organization=user.organization_id)


def summary(user, days=30) -> dict:
    """Сводка: сколько накладных пришло, на какую сумму, что с ними стало."""

    rows = _invoices(user).filter(created_at__gte=since(days))
    # Псевдонимы не должны совпадать с именами полей: `total=Count('id')` рядом
    # с `Sum('total')` Django понимает как сумму счётчика и отказывается считать.
    counts = rows.aggregate(
        how_many=Count('id'),
        amount=Sum('total'),
        checked=Count('id', filter=Q(checked_at__isnull=False)),
        sent=Count('id', filter=Q(umag_supply_id__isnull=False)),
        failed=Count('id', filter=Q(status=Invoice.Status.FAILED)),
    )

    lines = InvoiceLine.objects.filter(invoice__in=rows).count()

    return {
        'период_дней': int(days),
        'накладных': counts['how_many'],
        'на_сумму': _money(counts['amount']),
        'проверено': counts['checked'],
        'уехало_в_umag': counts['sent'],
        'не_распозналось': counts['failed'],
        'позиций_всего': lines,
    }


def suppliers(user, days=90) -> dict:
    """Поставщики по сумме закупа: у кого берут больше всего."""

    rows = (
        _invoices(user)
        .filter(created_at__gte=since(days))
        .exclude(supplier='')
        .values('supplier')
        .annotate(накладных=Count('id'), сумма=Sum('total'))
        .order_by('-сумма')[:MAX_ROWS]
    )

    return {
        'период_дней': int(days),
        'поставщики': [
            {
                'поставщик': row['supplier'],
                'накладных': row['накладных'],
                'сумма': _money(row['сумма']),
            }
            for row in rows
        ],
    }


def products(user, days=90) -> dict:
    """Что закупают чаще всего — по сумме за период."""

    rows = (
        InvoiceLine.objects.filter(
            invoice__organization=user.organization_id,
            invoice__created_at__gte=since(days),
        )
        .values('name')
        .annotate(строк=Count('id'), количество=Sum('quantity'), сумма=Sum('total'))
        .order_by('-сумма')[:MAX_ROWS]
    )

    return {
        'период_дней': int(days),
        'товары': [
            {
                'товар': row['name'],
                'раз_в_накладных': row['строк'],
                'количество': _number(row['количество']),
                'сумма': _money(row['сумма']),
            }
            for row in rows
        ],
    }


def invoices(user, days=30, supplier='') -> dict:
    """Список накладных: когда, от кого, на сколько и что с ней."""

    rows = _invoices(user).filter(created_at__gte=since(days))

    if supplier:
        rows = rows.filter(supplier__icontains=str(supplier)[:120])

    return {
        'накладные': [
            {
                'id': row.pk,
                'номер': row.number or None,
                'дата': row.issued_at.strftime('%d.%m.%Y') if row.issued_at else None,
                'загружена': row.created_at.strftime('%d.%m.%Y %H:%M'),
                'поставщик': row.supplier or None,
                'сумма': _money(row.total),
                'статус': row.get_status_display(),
                'позиций': row.lines.count(),
            }
            for row in rows.order_by('-created_at')[:MAX_ROWS]
        ],
    }


def invoice(user, id=None) -> dict:
    """Одна накладная целиком, со всеми позициями."""

    row = _invoices(user).filter(pk=id).first()

    if row is None:
        return {'ошибка': 'Такой накладной нет'}

    return {
        'номер': row.number or None,
        'дата': row.issued_at.strftime('%d.%m.%Y') if row.issued_at else None,
        'поставщик': row.supplier or None,
        'бин': row.supplier_bin or None,
        'сумма': _money(row.total),
        'статус': row.get_status_display(),
        'позиции': [
            {
                'название': line.name,
                'штрихкод': line.barcode or None,
                'количество': _number(line.quantity),
                'единица': line.unit or None,
                'цена': _money(line.price),
                'сумма': _money(line.total),
            }
            for line in row.lines.all()[:MAX_ROWS]
        ],
    }


def plan(user) -> dict:
    """Последний план закупа: что заканчивается и сколько дозаказать."""

    row = (
        PurchasePlan.objects.filter(
            user__organization=user.organization_id,
            status=PurchasePlan.Status.READY,
        )
        .order_by('-built_at')
        .first()
    )

    if row is None:
        return {'ошибка': 'План закупа ещё не считали'}

    items = row.items.order_by('position')[:MAX_ROWS]

    return {
        'магазин': row.store_name or None,
        'посчитан': row.built_at.strftime('%d.%m.%Y') if row.built_at else None,
        'продажи_за_дней': row.days,
        'закуп_на_дней': row.horizon,
        'позиций_требует_заказа': row.items_total,
        'сумма_закупа': _money(row.total_cost),
        'кончились_совсем': row.items.filter(stock__lte=0).count(),
        'первые_позиции': [
            {
                'товар': item.name,
                'остаток': _number(item.stock),
                'хватит_на_дней': _number(item.cover_days),
                'заказать': _number(item.suggested),
                'единица': item.measure or None,
                'на_сумму': _money(item.cost),
            }
            for item in items
        ],
    }


def parsing(user, days=30) -> dict:
    """Как работает распознавание: сколько стоит и часто ли ошибается."""

    rows = _invoices(user).filter(created_at__gte=since(days))
    money = rows.exclude(cost=None).aggregate(total=Sum('cost'), avg=Avg('cost'))

    lines = InvoiceLine.objects.filter(invoice__in=rows)
    matched = lines.exclude(umag_product_id=None).count()
    total = lines.count()

    return {
        'период_дней': int(days),
        'потрачено_на_разбор_usd': _number(money['total'], digits=4),
        'средняя_цена_разбора_usd': _number(money['avg'], digits=4),
        'позиций_всего': total,
        'сведено_с_товаром_umag': matched,
        'штрихкод_подставил_ии': lines.exclude(umag_confidence=None)
        .exclude(umag_confidence=1)
        .count(),
    }


def _money(value):
    return None if value is None else round(float(value), 2)


def _number(value, digits=3):
    if value is None:
        return None

    number = round(float(value), digits)
    return int(number) if number == int(number) else number


#: Что модель видит как доступные ей действия. Описания читает она же, поэтому
#: пишем их так, чтобы было понятно, когда что звать.
SCHEMAS = [
    *cabinet.SCHEMAS,
    {
        'type': 'function',
        'function': {
            'name': 'summary',
            'description': 'Сводка по накладным за период: сколько пришло, на какую '
            'сумму, сколько проверено и уехало в UMAG. С этого стоит начинать.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'days': {'type': 'integer', 'description': 'За сколько дней, 1–365'},
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'suppliers',
            'description': 'Поставщики по сумме закупа за период, от большего к меньшему.',
            'parameters': {
                'type': 'object',
                'properties': {'days': {'type': 'integer'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'products',
            'description': 'Что закупают чаще и на большие суммы за период.',
            'parameters': {
                'type': 'object',
                'properties': {'days': {'type': 'integer'}},
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'invoices',
            'description': 'Список накладных за период, при желании — только от одного '
            'поставщика. Отдаёт и id, по которому можно посмотреть накладную целиком.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'days': {'type': 'integer'},
                    'supplier': {
                        'type': 'string',
                        'description': 'Часть названия поставщика',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'invoice',
            'description': 'Одна накладная со всеми позициями. id берут из списка.',
            'parameters': {
                'type': 'object',
                'properties': {'id': {'type': 'integer'}},
                'required': ['id'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'plan',
            'description': 'Последний план закупа: что заканчивается, сколько дозаказать '
            'и на какую сумму.',
            'parameters': {'type': 'object', 'properties': {}},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'parsing',
            'description': 'Как работает само распознавание: во сколько обошлось и часто '
            'ли товар не нашёлся в номенклатуре UMAG.',
            'parameters': {
                'type': 'object',
                'properties': {'days': {'type': 'integer'}},
            },
        },
    },
]

#: Имя из ответа модели → функция. Ничего, кроме этого словаря, не вызывается:
#: выдумает модель имя — получит отказ, а не попытку что-то исполнить.
HANDLERS = {
    **cabinet.HANDLERS,
    'summary': summary,
    'suppliers': suppliers,
    'products': products,
    'invoices': invoices,
    'invoice': invoice,
    'plan': plan,
    'parsing': parsing,
}
