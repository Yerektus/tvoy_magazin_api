"""Excel-отчёт, который аналитик кладёт в чат.

В контекст модели уходит только сводка: сколько строк и как файл называется.
Сама таблица человеку в переписке не нужна — её откроют в Excel.
"""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from invoices.models import Invoice
from purchases.models import PurchasePlan

from . import cabinet, xlsx

#: В файл помещается столько же, сколько аналитик вообще видит в кабинете.
MAX_ROWS = cabinet.SCAN


class Attachment:
    """Готовый файл. В JSON модели не попадает — его прикрепляет сервер."""

    def __init__(self, name: str, content: bytes, summary: dict):
        self.name = name
        self.content = content
        self.summary = summary


def make_report(user, kind='sales', days=30) -> Attachment | dict:
    """Собирает книгу по тому, о чём спросили.

    `kind` — sales, invoices или plan. Чужое слово считаем продажами: это
    самый частый «отчёт», а выдуманный тип лучше не ронять разговор.
    """

    kind = str(kind or 'sales').strip().lower()

    if kind == 'invoices':
        return _invoices(user, days)

    if kind == 'plan':
        return _plan(user)

    return _sales(user, days)


def _sales(user, days) -> Attachment | dict:
    rows, error = cabinet._sold(user, days)

    if error:
        return error

    days = cabinet._days(days)
    rows.sort(key=lambda row: cabinet._float(row.get('saleSellingAmount')), reverse=True)
    lines = [cabinet._row(row, days) for row in rows[:MAX_ROWS]]
    name = f'Продажи за {days} дн.xlsx'

    return _file(
        name,
        'Продажи',
        [
            'Товар',
            'Штрихкод',
            'Единица',
            'Продано',
            'Выручка',
            'Маржа',
            'Наценка %',
            'Остаток',
            'Хватит дней',
        ],
        [
            [
                line['товар'],
                line['штрихкод'],
                line['единица'],
                line['продано'],
                line['выручка'],
                line['маржа'],
                line['наценка_%'],
                line['остаток'],
                line['хватит_дней'],
            ]
            for line in lines
        ],
        {'период_дней': days, 'строк': len(lines)},
    )


def _invoices(user, days) -> Attachment:
    days = cabinet._days(days)
    rows = (
        Invoice.objects.filter(
            organization=user.organization_id,
            created_at__gte=timezone.now() - timedelta(days=days),
        )
        .annotate(позиций=Count('lines'))
        .order_by('-created_at')[:MAX_ROWS]
    )
    name = f'Накладные за {days} дн.xlsx'

    return _file(
        name,
        'Накладные',
        ['Номер', 'Дата', 'Поставщик', 'Сумма', 'Статус', 'Позиций'],
        [
            [
                row.number or None,
                row.issued_at.strftime('%d.%m.%Y') if row.issued_at else None,
                row.supplier or None,
                cabinet._money(row.total),
                row.get_status_display(),
                row.позиций,
            ]
            for row in rows
        ],
        {'период_дней': days, 'строк': len(rows)},
    )


def _plan(user) -> Attachment | dict:
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

    items = list(row.items.order_by('position')[:MAX_ROWS])
    name = 'План закупа.xlsx'

    return _file(
        name,
        'Закуп',
        ['Товар', 'Остаток', 'Хватит дней', 'Заказать', 'Единица', 'На сумму'],
        [
            [
                item.name,
                cabinet._number(item.stock),
                cabinet._number(item.cover_days),
                cabinet._number(item.suggested),
                item.measure or None,
                cabinet._money(item.cost),
            ]
            for item in items
        ],
        {'строк': len(items), 'план': row.name or None},
    )


def _file(name: str, sheet: str, headers: list[str], rows: list[list], extra: dict) -> Attachment:
    return Attachment(
        name=name,
        content=xlsx.book(sheet, headers, rows),
        summary={
            'готово': True,
            'файл': name,
            **extra,
            'подсказка': (
                'Файл уйдёт в чат сам. В ответе одна короткая строка: '
                'что за отчёт и сколько позиций. Таблицу в чат не пиши.'
            ),
        },
    )
