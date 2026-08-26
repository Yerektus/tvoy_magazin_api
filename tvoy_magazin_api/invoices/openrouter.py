"""Разбор фото накладной vision-моделью через OpenRouter.

Запросы уходят через urllib из стандартной библиотеки — отдельный HTTP-клиент
ради одного POST ставить не хотелось.
"""

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from typing import NamedTuple

from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = 'https://openrouter.ai/api/v1/chat/completions'

# Провайдер перегружен или отвалился — имеет смысл переспросить.
RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
RETRY_DELAYS = (5, 15, 40)

SYSTEM_PROMPT = """Ты разбираешь фотографии казахстанских товарных накладных
(форма 3-2, «Реализация товаров», товарные чеки, рукописные бланки) и
возвращаешь строгий JSON.

## Что на фото

Снимают телефоном в магазине: лист держат в руке, кадр под углом, бумага
мятая или матричной печати, документ бывает повёрнут на 90 или 180 градусов —
читай при любом повороте. Под основным листом обычно лежат другие бумаги и
обрывки: разбирай только тот документ, который занимает кадр, соседние листы
не трогай. Палец или загиб могут закрыть часть строки — такую строку всё
равно возвращай, а нечитаемые поля ставь null.

## Печать и рукопись

В документе два слоя, и рукописный обычно и есть факт приёмки.

- Рукописная цифра в клетке таблицы важнее печатной. Если печатное
  количество зачёркнуто, а рядом написано другое, в quantity идёт
  рукописное, в quantity_printed — печатное, handwritten — true.
- Галочка, крестик или прочерк в клетке количества подтверждают печатное
  число, а не заменяют его: quantity остаётся печатным.
- Рукопись вне таблицы позицией не является. Подписи и расшифровки
  («Отпустил», «Получил»), название магазина, номер доверенности, пометки
  «нал.», «обмен», пересчёты на полях вида «3190-1630=1560» — всё это в
  lines не попадает.
- Документ бывает рукописным целиком. Тогда позиции читай из рукописи, а
  запись вида «сосиска 8.29 x 1980 = 16414» разбирай как quantity 8.29,
  price 1980, total 16414.

## Строки таблицы

Границу строки задают горизонтальные линейки таблицы, а не строки текста.

- Длинное название в ячейку не влезает и переносится на вторую-третью
  строчку. Это по-прежнему одна позиция: склей перенесённые куски в одно
  название, сверху вниз. «Коктейль Молочный ЧУДО Шоколад 0,2л», а под ним
  «2% БС» — это «Коктейль Молочный ЧУДО Шоколад 0,2л 2% БС». Не две позиции,
  не позиция с названием «2% БС» и не одна только верхняя половина.
- Обрывок вроде «БШ», «2% БС», «БЛ» сам по себе названием не бывает: это
  хвост строки выше, и его место — в её названии.
- Сколько в накладной позиций, видно по колонке «Номер по порядку»: её
  последний номер и есть длина lines. Строк вышло больше — перенос приняли за
  отдельную позицию, меньше — склеили две разные.
- Из-за переноса цифры соседних строк съезжают по вертикали относительно
  названий. Количество, цену и сумму бери из той же строки таблицы, между тех
  же линеек, а не с той высоты, где кончилось название.

## Поля

- Переноси данные ровно так, как они написаны, ничего не додумывай. Нет
  значения в документе — null.
- Числа отдавай числами: разделитель дробной части — точка, пробелы внутри
  чисел убирай («1 333,00» → 1333.00).
- issued_at — дата документа в виде ГГГГ-ММ-ДД («12.08.2026» → 2026-08-12).
- quantity бери из колонки «Отпущено», а не из «Подлежит отпуску».
- price — цена за единицу, total — сумма по строке. Итог по всей накладной
  складывает уже наш код, поэтому важнее аккуратно прочитать суммы строк:
  total документа бери только из строки «Итого» или «на сумму», а нет такой
  строки — ставь null. Сумму последней позиции за итог не выдавай, рукописные
  пересчёты на полях за итог тоже не считай.
- barcode — штрихкод из 8–14 цифр. Номенклатурный номер и внутренний код
  поставщика («R14035», «10000805», «V7586») в barcode не пиши.
- unit оставляй как в бумаге: шт, бут., пачка, кор.
- Если документ помечен «стр. 1 из 3», заполни page и pages: позиции
  остальных страниц окажутся на других фотографиях. Итог на многостраничной
  накладной печатают в самом конце, поэтому при page меньше pages total
  всегда null — числа в нижней строке таблицы там принадлежат позиции.
- confidence — насколько уверенно прочитана строка. Ставь ниже 0.7, когда
  цифра или название под вопросом: такие строки человек сверит с бумагой
  руками, поэтому честная оценка полезнее оптимистичной.
- top_edge — с какой стороны фотографии оказался верхний край документа: тот,
  где напечатана шапка («Форма 3-2», название документа, номер и дата).
  Снимали, не поворачивая телефон, и накладная нередко лежит боком. Отвечай,
  что видишь на кадре: шапка сверху — "top", у правого края — "right", снизу
  (документ вверх ногами) — "bottom", у левого края — "left". Поворот кадра
  на несколько градусов в счёт не идёт: там по-прежнему "top".
"""

# Строгий json_schema требует, чтобы каждое свойство было перечислено в
# required, — необязательных полей тут нет, «нет значения» передаётся как null.
RESPONSE_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'supplier', 'supplier_bin', 'number', 'issued_at',
        'total', 'doc_kind', 'top_edge', 'page', 'pages', 'lines',
    ],
    'properties': {
        'supplier': {'type': ['string', 'null'], 'description': 'Организация-отправитель'},
        'supplier_bin': {'type': ['string', 'null'], 'description': 'БИН или ИИН поставщика'},
        'number': {'type': ['string', 'null'], 'description': 'Номер документа'},
        'issued_at': {'type': ['string', 'null'], 'description': 'Дата документа в формате ГГГГ-ММ-ДД'},
        'total': {
            'type': ['number', 'null'],
            'description': 'Итог из строки «Итого» документа, если он там напечатан',
        },
        'doc_kind': {
            'type': 'string',
            'enum': ['printed', 'mixed', 'handwritten'],
            'description': 'printed — всё напечатано, mixed — печать с рукописными правками, '
                           'handwritten — документ заполнен от руки',
        },
        'top_edge': {
            'type': 'string',
            'enum': ['top', 'right', 'bottom', 'left'],
            'description': 'С какой стороны кадра оказался верхний край документа — тот, '
                           'где напечатана шапка. Ровный снимок — top',
        },
        'page': {'type': ['integer', 'null'], 'description': 'Номер страницы, если он указан'},
        'pages': {'type': ['integer', 'null'], 'description': 'Всего страниц, если указано'},
        'lines': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'name', 'barcode', 'quantity', 'quantity_printed',
                    'unit', 'price', 'total', 'handwritten', 'confidence',
                ],
                'properties': {
                    'name': {'type': 'string'},
                    'barcode': {'type': ['string', 'null']},
                    'quantity': {
                        'type': ['number', 'null'],
                        'description': 'Фактически принятое количество: рукописная правка важнее печати',
                    },
                    'quantity_printed': {
                        'type': ['number', 'null'],
                        'description': 'Напечатанное количество, если рукой исправили другое',
                    },
                    'unit': {'type': ['string', 'null']},
                    'price': {'type': ['number', 'null']},
                    'total': {'type': ['number', 'null']},
                    'handwritten': {
                        'type': 'boolean',
                        'description': 'Хотя бы одно значение строки прочитано из рукописи',
                    },
                    'confidence': {
                        'type': 'number',
                        'description': 'Уверенность в строке, от 0 до 1',
                    },
                },
            },
        },
    },
}




SUPPLIER_PROMPT = """Ты сверяешь поставщика из накладной со списком тех, с кем
магазин уже работал, и возвращаешь строгий JSON.

## Как выбирать

- Один и тот же поставщик пишется по-разному: «ТОО ЖЕТЫСУ-ТРЕЙД»,
  «Товарищество с ограниченной ответственностью "Жетысу Трейд"» и
  «жетысу трейд» — это он.
- Правовая форма (ТОО, ИП, АО) и кавычки роли не играют, важно само название.
- Похожие названия — разные организации: «Жетысу Сут» и «Жетысу Трейд» это
  два поставщика, а филиал и головная контора — тем более: у них разные БИН.
- Не уверен — ставь id null. Найденный БИН уедет в приёмку и в бухгалтерию,
  чужой там хуже пустого.
- confidence — от 0 до 1, насколько ты уверен в выборе.
"""

SUPPLIER_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['id', 'confidence'],
    'properties': {
        'id': {
            'type': ['integer', 'null'],
            'description': 'id поставщика из списка или null, если это кто-то другой',
        },
        'confidence': {'type': 'number', 'description': 'Уверенность в выборе, от 0 до 1'},
    },
}


class OpenRouterError(RuntimeError):
    """Модель не ответила или вернула не то, что мы просили."""


class Parsed(NamedTuple):
    """Ответ модели вместе с тем, во что он обошёлся."""

    data: dict
    model: str
    # Стоимость запроса в долларах: OpenRouter присылает её в usage.cost.
    cost: float | None


def parse_invoice(pages: list[tuple[bytes, str]]) -> Parsed:
    """Возвращает разобранные данные накладной, имя модели и цену запроса.

    `pages` — листы документа по порядку, каждый парой «байты, тип». Обычно
    лист один; когда позиции не поместились на страницу, их два и больше.

    Все листы уходят в один запрос, а не разбираются по отдельности: шапка
    стоит только на первом, а позиции второго — продолжение той же таблицы.
    Разобрав их порознь, мы получили бы две накладные, у одной из которых нет
    ни поставщика, ни номера.
    """

    if not pages:
        raise OpenRouterError('Нечего разбирать: у накладной нет ни одного листа')

    images = [
        {
            'type': 'image_url',
            'image_url': {
                'url': f'data:{content_type};base64,{base64.b64encode(image).decode()}'
            },
        }
        for image, content_type in pages
    ]

    text = (
        'Разбери накладную с фотографии. Сначала найди на листе '
        'таблицу позиций и посмотри, где печать, а где рукописные правки.'
    )

    if len(pages) > 1:
        text = (
            f'Это одна накладная на {len(pages)} листах, снятая по частям. '
            'Шапка — поставщик, номер, дата — есть только на первом листе, '
            'на остальных продолжается таблица позиций. Собери все позиции со '
            'всех листов в один список по порядку и не повторяй те, что уже '
            'выписал. Посмотри, где печать, а где рукописные правки.'
        )

    payload = {
        'model': settings.OPENROUTER_VISION_MODEL,
        'temperature': 0,
        # Накладная на 30 позиций — это несколько тысяч токенов JSON. По умолчанию
        # провайдер обрывает ответ на середине, и разбор падает на json.loads.
        'max_tokens': 8000,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {
                'role': 'user',
                'content': [{'type': 'text', 'text': text}, *images],
            },
        ],
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': 'invoice', 'strict': True, 'schema': RESPONSE_SCHEMA},
        },
    }

    return _ask(payload)


def match_supplier(name: str, candidates: list[dict]) -> Parsed:
    """Ищет того же поставщика среди тех, с кем уже работали.

    `candidates` — `[{'id': …, 'name': …}]`, ответ — выбранный id или null.
    """

    payload = {
        'model': settings.OPENROUTER_MATCH_MODEL,
        'temperature': 0,
        'max_tokens': 500,
        'messages': [
            {'role': 'system', 'content': SUPPLIER_PROMPT},
            {
                'role': 'user',
                'content': 'Поставщик из накладной и те, с кем магазин уже работал:\n'
                + json.dumps({'supplier': name, 'candidates': candidates}, ensure_ascii=False),
            },
        ],
        'response_format': {
            'type': 'json_schema',
            'json_schema': {'name': 'supplier', 'strict': True, 'schema': SUPPLIER_SCHEMA},
        },
    }

    return _ask(payload)


def _ask(payload: dict) -> Parsed:
    """Общий хвост запроса: ключ, отправка и разбор ответа."""

    api_key = settings.OPENROUTER_API_KEY

    if not api_key:
        raise OpenRouterError('Не задан OPENROUTER_API_KEY')

    model = payload['model']

    if settings.OPENROUTER_FALLBACK_MODELS:
        payload = {**payload, 'models': [model, *settings.OPENROUTER_FALLBACK_MODELS]}

    body = _post(payload, api_key)

    try:
        content = body['choices'][0]['message']['content']
    except (KeyError, IndexError) as error:
        raise OpenRouterError(f'Неожиданный ответ OpenRouter: {str(body)[:500]}') from error

    # Пустой `content` — не наша ошибка формата, а модель, которая всё сказала
    # в рассуждении и вслух не ответила ничего. Так отвечают модели без
    # поддержки строгой схемы; без этой проверки дальше падало `AttributeError`
    # на пустоте, и в логе вместо причины была строчка про `NoneType`.
    if not content:
        raise OpenRouterError(f'Модель {body.get("model") or model} ответила пустотой')

    # Какая модель ответила на самом деле — при подмене из списка это важно.
    return Parsed(_load_json(content), body.get('model') or model, _cost(body))


def _cost(body: dict) -> float | None:
    """Цена запроса в долларах — сколько OpenRouter списал за это фото."""

    usage = body.get('usage')
    if not isinstance(usage, dict):
        return None

    try:
        return float(usage['cost'])
    except (KeyError, TypeError, ValueError):
        return None


def _post(payload: dict, api_key: str) -> dict:
    """Шлёт запрос, переспрашивая при перегрузке провайдера.

    Бесплатные модели живут в общем пуле и регулярно отвечают 429 — одна
    попытка тут почти ничего не значит.
    """

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        },
        method='POST',
    )

    last_error = ''

    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            with urllib.request.urlopen(request, timeout=settings.OPENROUTER_TIMEOUT) as response:
                body = json.loads(response.read().decode())

            # OpenRouter умеет отдавать 200 с ошибкой внутри тела — например,
            # когда провайдер не ответил вовремя. Такое тоже стоит переспросить.
            error_body = body.get('error')
            if not error_body:
                return body

            code = error_body.get('code') if isinstance(error_body, dict) else None
            last_error = str(error_body)[:500]

            if code not in RETRY_STATUSES or delay is None:
                raise OpenRouterError(last_error)
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors='replace')[:500]
            last_error = f'OpenRouter ответил {error.code}: {detail}'

            if error.code not in RETRY_STATUSES or delay is None:
                raise OpenRouterError(last_error) from error
        except urllib.error.URLError as error:
            last_error = f'OpenRouter недоступен: {error.reason}'

            if delay is None:
                raise OpenRouterError(last_error) from error

        logger.warning('Попытка %s не удалась (%s), ждём %s с', attempt, last_error[:120], delay)
        time.sleep(delay)

    raise OpenRouterError(last_error)


def _load_json(content: str) -> dict:
    """Модель иногда оборачивает JSON в ```json — снимаем обёртку."""

    text = content.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
        text = text.rsplit('```', 1)[0]

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        logger.warning('Модель вернула не JSON: %s', text[:500])
        raise OpenRouterError('Модель вернула не JSON') from error

    # Часть моделей отдаёт накладную единственным элементом массива, даже когда
    # схема просит объект, — разворачиваем.
    if isinstance(parsed, list) and len(parsed) == 1:
        parsed = parsed[0]

    if not isinstance(parsed, dict):
        raise OpenRouterError('Модель вернула не объект')

    return parsed
