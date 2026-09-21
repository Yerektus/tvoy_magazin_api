import base64
import json
import logging
import re

from django.conf import settings

from django.utils import timezone

from invoices.openrouter import OpenRouterError, _cost, _post

from . import export, screen, tools
from .prompts import SYSTEM_PROMPT

logger = logging.getLogger(__name__)

MAX_STEPS = 6


def describe_page(page) -> str:
    if not isinstance(page, dict):
        return ''

    raw = str(page.get('path') or '').strip()
    path = raw.split('?', 1)[0].split('#', 1)[0].strip()
    query = raw.split('?', 1)[1].split('#', 1)[0] if '?' in raw else ''
    title = str(page.get('title') or '').strip()[:80]

    if not re.fullmatch(r'/[a-z0-9_/-]*', path):
        path = ''
        query = ''

    shown = f'{path}?{query}' if path and query else path
    parts = [part for part in path.split('/') if part]
    hint = _page_tool_hint(parts)

    if not title and not hint:
        return ''

    where = f'«{title}»' if title else shown
    extra = f' ({shown})' if shown and title else ''

    lines = [
        f'Человек сейчас на странице {where}{extra}.',
        'Если вопрос про «это», «здесь», «эту страницу» или не уточняет объект — '
        'ответь по данным этой страницы коротко, без соседних тем.',
    ]

    if hint:
        lines.append(hint)

    return ' '.join(lines)


SUGGESTIONS_BLOCK = re.compile(
    r'(?:\n|^)<<<вопросы[ \t]*\n(.*?)(?:\n>>>[ \t]*)?\s*\Z',
    re.DOTALL | re.IGNORECASE,
)
MAX_SUGGESTIONS = 3
MAX_SUGGESTION = 120


def split_suggestions(text: str) -> tuple[str, list[str]]:
    text = (text or '').replace('\r\n', '\n')
    match = SUGGESTIONS_BLOCK.search(text)

    if not match:
        return text.strip(), []

    questions: list[str] = []
    seen: set[str] = set()

    for line in match.group(1).splitlines():
        line = re.sub(r'^(?:\d+[\.\)]|[-—*•])\s*', '', line).strip()
        line = line.strip('«»"\'')

        if not line:
            continue

        key = line.casefold()

        if key in seen:
            continue

        if len(line) > MAX_SUGGESTION:
            line = f'{line[: MAX_SUGGESTION - 1].rstrip()}…'

        seen.add(key)
        questions.append(line)

        if len(questions) == MAX_SUGGESTIONS:
            break

    clean = text[: match.start()].strip()

    return clean, questions


def attach_screen(text: str, path: str) -> str:
    """Прячет путь в ответ. Блок модели выбрасываем — ей адреса писать нельзя."""

    text, questions = split_suggestions(text)
    text, _ = screen.split_screen(text)
    text = f'{text}\n\n<<<{screen.SCREEN_MARK}\n{path}\n>>>'

    if questions:
        text = f'{text}\n<<<вопросы\n' + '\n'.join(questions) + '\n>>>'

    return text


def _page_tool_hint(parts: list[str]) -> str:
    if not parts:
        return ''

    section, *rest = parts
    ident = rest[0] if rest else ''

    if section == 'documents':
        if ident.isdigit():
            return f'Это накладная id={ident}. Сначала позови invoice с этим id.'

        return 'Это список накладных. Начни с invoices или summary. Просят отчёт или Excel — make_report kind=invoices.'

    if section == 'products':
        if ident.isdigit():
            return (
                f'Это карточка товара со штрихкодом {ident}. '
                'Сначала позови umag_product с этим штрихкодом.'
            )

        return (
            'Это каталог товаров магазина. Для остатков и цен зови umag_catalog и umag_product. '
            'Просят отчёт или Excel — make_report kind=sales. '
            'Просят поставить фильтр — set_filters page=products.'
        )

    if section == 'sales':
        return (
            'Продажи смотри через umag_sales, не через накладные. '
            'Просят отчёт или Excel — make_report kind=sales. '
            'Просят сменить период на графике — set_filters page=sales.'
        )

    if section == 'purchases':
        if ident.isdigit():
            return f'Это планировка закупа id={ident}. Сначала позови plan с этим id.'

        return 'Это планирование закупов. Смотри plan. Просят отчёт или Excel — make_report kind=plan.'

    if section == 'settings':
        return 'Это настройки расширений, данных магазина на странице нет.'

    return ''


def reply(
    user, history: list[dict], think: bool = False, page=None, files=None
) -> tuple[str, float | None]:
    if not settings.OPENROUTER_API_KEY:
        raise OpenRouterError('Не задан OPENROUTER_API_KEY')

    today = timezone.localdate().isoformat()
    messages = [
        {
            'role': 'system',
            'content': f'{SYSTEM_PROMPT}\n\nСегодня {today}. Даты в фильтрах пиши YYYY-MM-DD.',
        }
    ]
    hint = describe_page(page)
    screens: list[screen.Screen] = []

    if hint:
        messages.append({'role': 'system', 'content': hint})

    messages.extend(history)
    spent = 0.0
    collected = files if files is not None else []

    for _ in range(MAX_STEPS):
        payload = {
            'model': settings.OPENROUTER_ASSISTANT_MODEL,
            'temperature': 0.2,
            'max_tokens': 900,
            'messages': messages,
            'tools': tools.SCHEMAS,
        }

        if think:
            payload['reasoning'] = {'enabled': True, 'exclude': True}

        body = _post(payload, settings.OPENROUTER_API_KEY)

        spent += _cost(body) or 0

        try:
            answer = body['choices'][0]['message']
        except (KeyError, IndexError) as error:
            raise OpenRouterError(f'Неожиданный ответ: {str(body)[:300]}') from error

        calls = answer.get('tool_calls') or []

        if not calls:
            text = (answer.get('content') or '').strip()
            text = text or 'Не получилось собрать ответ. Спросите иначе.'

            if screens:
                text = attach_screen(text, screens[-1].path)

            return text, spent or None
        
        messages.append(answer)

        for call in calls:
            messages.append(
                {
                    'role': 'tool',
                    'tool_call_id': call.get('id'),
                    'content': json.dumps(
                        _run(user, call, collected, screens),
                        ensure_ascii=False,
                    ),
                }
            )

    text = 'Слишком долго ищу ответ. Спросите про что-то одно.'

    if screens:
        text = attach_screen(text, screens[-1].path)

    return text, spent or None


def with_image(text: str, image, content_type: str = 'image/jpeg') -> list[dict]:
    parts = [
        {
            'type': 'image_url',
            'image_url': {
                'url': f'data:{content_type};base64,{base64.b64encode(image).decode()}'
            },
        }
    ]

    if text:
        parts.insert(0, {'type': 'text', 'text': text})

    return parts


def _run(user, call: dict, files=None, screens=None) -> dict:
    """Зовёт одну ручку. Что бы модель ни попросила, дальше словаря не уйдёт."""

    name = (call.get('function') or {}).get('name')
    handler = tools.HANDLERS.get(name)

    if handler is None:
        return {'ошибка': f'Нет такой функции: {name}'}

    try:
        arguments = json.loads((call['function'].get('arguments') or '{}'))
    except json.JSONDecodeError:
        arguments = {}

    if not isinstance(arguments, dict):
        arguments = {}

    # Лишние ключи выбрасываем: ручка принимает только то, что объявила, и
    # `organization` среди этого нет — её ставит сама ручка по пользователю.
    allowed = handler.__code__.co_varnames[: handler.__code__.co_argcount]
    arguments = {key: value for key, value in arguments.items() if key in allowed and key != 'user'}

    try:
        result = handler(user, **arguments)
    except Exception as error:  # noqa: BLE001 — модель не должна ронять запрос
        logger.exception('Аналитик не смог вызвать %s', name)
        return {'ошибка': f'Не получилось посмотреть: {error}'}

    # Excel в JSON модели не кладём: бинарник ей не прочитать, а в чат файл
    # прикрепляет сервер по этому списку.
    if isinstance(result, export.Attachment):
        if files is not None:
            files.append(result)

        return result.summary

    if isinstance(result, screen.Screen):
        if screens is not None:
            screens.append(result)

        return result.summary

    return result
