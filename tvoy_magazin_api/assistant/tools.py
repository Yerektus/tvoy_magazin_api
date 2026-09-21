"""Чем аналитик смотрит данные.

Модель не ходит в базу и не пишет запросов. Она вызывает ручки из этого файла,
а они уже сами отбирают данные — **всегда** по организации того, кто спрашивает.
Организацию модель не передаёт и передать не может: её подставляет сервер.

Отсюда три правила, которые нельзя нарушать:

1. Только чтение. Ни одна ручка ничего не меняет в магазине и не удаляет.
   `set_filters` тоже ничего не пишет: она только говорит кабинету, какие
   поля показать.
2. Отбор по организации ставится здесь, а не приходит из ответа модели.
3. У каждой ручки есть потолок по числу строк: миллион токенов контекста не
   повод отдавать всю базу, а счёт за это платим мы.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from invoices.models import Invoice, InvoiceLine
from invoices.openrouter import OpenRouterError, _post
from purchases.models import PurchasePlan

from . import cabinet, export, screen

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


def plan(user, id=None) -> dict:
    """План закупа: что заканчивается и сколько дозаказать.

    Без id — последняя готовая планировка организации. С id — та, которую
    человек открыл, даже если она ещё считается.
    """

    rows = PurchasePlan.objects.filter(user__organization=user.organization_id)

    if id is not None:
        try:
            pk = int(id)
        except (TypeError, ValueError):
            return {'ошибка': 'Такой планировки нет'}

        row = rows.filter(pk=pk).first() if pk > 0 else None

        if row is None:
            return {'ошибка': 'Такой планировки нет'}
    else:
        row = rows.filter(status=PurchasePlan.Status.READY).order_by('-built_at').first()

        if row is None:
            return {'ошибка': 'План закупа ещё не считали'}

    if row.status != PurchasePlan.Status.READY:
        return {
            'id': row.pk,
            'название': row.name or None,
            'статус': row.get_status_display(),
            'ошибка': row.error or None,
        }

    items = row.items.order_by('position')[:MAX_ROWS]

    return {
        'id': row.pk,
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


#: Длиннее не ищем: это название товара или короткий вопрос, не сочинение.
WEB_QUERY = 200


def search_web(user, query='') -> dict:
    """Публичный поиск: состав, бренд, что это за товар. Не цифры магазина."""

    del user
    text = str(query or '').strip()[:WEB_QUERY]

    if not text:
        return {'ошибка': 'Пустой запрос'}

    if not settings.OPENROUTER_API_KEY:
        return {'ошибка': 'Поиск в интернете не настроен'}

    payload = {
        'model': settings.OPENROUTER_ASSISTANT_MODEL,
        'temperature': 0,
        'max_tokens': 500,
        'plugins': [{'id': 'web', 'max_results': 5}],
        'messages': [
            {
                'role': 'user',
                'content': (
                    'Найди в интернете факты по запросу и перескажи коротко. '
                    'Если ничего нет — так и скажи. Запрос: '
                    + text
                ),
            }
        ],
    }

    try:
        body = _post(payload, settings.OPENROUTER_API_KEY)
    except OpenRouterError as error:
        return {'ошибка': f'Поиск недоступен: {error}'}

    try:
        message = body['choices'][0]['message']
    except (KeyError, IndexError, TypeError):
        return {'ошибка': 'Поиск ничего не вернул'}

    found = (message.get('content') or '').strip()
    sources = _web_sources(message)

    if not found and not sources:
        return {'запрос': text, 'результаты': [], 'комментарий': 'Ничего не нашлось'}

    return {'запрос': text, 'найдено': found, 'источники': sources}


def _web_sources(message: dict) -> list[dict]:
    """Ссылки, которые OpenRouter приложил к ответу поиска."""

    sources = []

    for item in message.get('annotations') or []:
        if not isinstance(item, dict):
            continue

        citation = item.get('url_citation') if isinstance(item.get('url_citation'), dict) else item
        url = str(citation.get('url') or '')[:300]
        title = str(citation.get('title') or '')[:160]

        if url:
            sources.append({'название': title or url, 'url': url})

    return sources[:8]


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
            'description': 'План закупа: что заканчивается, сколько дозаказать '
            'и на какую сумму. Без id — последняя готовая; id берут со страницы '
            'планировки, которую человек открыл.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'id': {
                        'type': 'integer',
                        'description': 'id планировки, если человек на её странице',
                    },
                },
            },
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
    {
        'type': 'function',
        'function': {
            'name': 'set_filters',
            'description': 'Выставить фильтры на странице кабинета. Звать, когда '
            'просят поставить фильтр, показать за период, отсеять по количеству '
            'продаж или сбросить фильтры. Товары — page=products: дата последней '
            'продажи и сколько продано. График продаж — page=sales: только период. '
            'Кабинет применит сам, путь в ответ писать не надо.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'page': {
                        'type': 'string',
                        'enum': ['products', 'sales'],
                        'description': 'Таблица товаров или график продаж',
                    },
                    'date_from': {
                        'type': 'string',
                        'description': 'Начало периода, YYYY-MM-DD',
                    },
                    'date_to': {
                        'type': 'string',
                        'description': 'Конец периода, YYYY-MM-DD',
                    },
                    'days': {
                        'type': 'integer',
                        'description': 'Последние N дней, если точных дат нет',
                    },
                    'sold_from': {
                        'type': 'number',
                        'description': 'Минимум проданного количества',
                    },
                    'sold_to': {
                        'type': 'number',
                        'description': 'Максимум проданного количества',
                    },
                    'only_sold': {
                        'type': 'boolean',
                        'description': 'Только то, что продавалось — минимум 1',
                    },
                    'query': {
                        'type': 'string',
                        'description': 'Часть названия товара',
                    },
                    'barcode': {
                        'type': 'string',
                        'description': 'Часть штрихкода',
                    },
                    'accuracy': {
                        'type': 'string',
                        'description': 'Точность прогноза: high, medium, low, none — через запятую',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'make_report',
            'description': 'Собрать Excel-отчёт и положить файл в чат. Звать, когда '
            'просят отчёт, выгрузку, Excel или файл. kind: sales — продажи '
            'и остатки, invoices — накладные, plan — план закупа. Таблицу в '
            'чат не пиши — файл уйдёт сам.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'kind': {
                        'type': 'string',
                        'enum': ['sales', 'invoices', 'plan'],
                        'description': 'Какой отчёт. По умолчанию продажи.',
                    },
                    'days': {
                        'type': 'integer',
                        'description': 'За сколько дней, 1–365. Для плана не нужен.',
                    },
                },
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_web',
            'description': 'Поиск в интернете: состав, бренд, что это за товар, '
            'похожие названия, сайт производителя. Не продажи и не остатки '
            'магазина — их смотри своими функциями.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {
                        'type': 'string',
                        'description': 'Что искать, как в поисковой строке',
                    },
                },
                'required': ['query'],
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
    'make_report': export.make_report,
    'set_filters': screen.set_filters,
    'search_web': search_web,
}
