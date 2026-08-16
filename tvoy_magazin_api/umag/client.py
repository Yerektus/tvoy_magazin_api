"""Клиент кабинета UMAG.

Публичного API у них нет: адреса, заголовки и тела запросов сняты с
веб-кабинета, разбор лежит в `umag-api.md` в корне проекта. Оттуда же две
особенности, из-за которых обычный запрос отвечает 400:

* токен идёт в `Authorization` как есть, без `Bearer`;
* без заголовков `api-ver` и `client-ver` сервер считает клиента устаревшим.

Вход — тоже не то, чего ждёшь: это GET с Basic-авторизацией, а в ответе
приходит `session_token`.
"""

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)


class UmagError(Exception):
    """Ошибка на стороне UMAG — её текст показываем человеку."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        # Код ответа: 422 у них означает «такого нет», а не поломку.
        self.status = status


class UmagAuthError(UmagError):
    """Логин с паролем не подошли или сессия кончилась."""


def sign_in(phone: str, password: str, user_id: int | None = None) -> tuple[str, list[dict]]:
    """Меняет телефон и пароль на токен сессии.

    На один номер бывает заведено несколько сотрудников. Тогда UMAG вместо
    токена отвечает `user_not_selected` и списком, из которого нужно выбрать, —
    вход повторяется с `signInAsUser`. Возвращает токен либо этот список.
    """

    credentials = base64.b64encode(f'{phone}:{password}'.encode()).decode()
    body = _request(
        'GET',
        'org/login/signin',
        params={'signInAsUser': user_id},
        auth=f'Basic {credentials}',
    )

    if not isinstance(body, dict):
        raise UmagAuthError(f'UMAG ответил на вход неожиданно: {str(body)[:200]}')

    if body.get('status') == 'user_not_selected':
        users = [
            {'id': user.get('user_id'), 'label': (user.get('label') or '').strip()}
            for user in body.get('allowed_users') or []
        ]

        if not users:
            raise UmagAuthError('UMAG просит выбрать сотрудника, но список пуст')

        return '', users

    # На входе токен приходит как sessionToken, а при продлении — session_token.
    token = body.get('sessionToken') or body.get('session_token')

    if not token:
        raise UmagAuthError(f'UMAG не вернул токен сессии, а прислал: {sorted(body)}')

    return token, []


def stores(token: str) -> list[dict]:
    """Магазины компании: у одной компании их обычно несколько."""

    body = _request('GET', 'org/store/list', auth=token)
    return body if isinstance(body, list) else []


class UmagClient:
    """Запросы от имени сотрудника. Магазин подставляется сам."""

    def __init__(self, account, store_id: int | None = None):
        self.account = account
        # Накладная знает свой магазин: она уходит туда, где её завели, а не
        # туда, что выбрано в шапке прямо сейчас.
        self.store_id = store_id or account.store_id

    def get(self, path: str, **params):
        return self._call('GET', path, params=params)

    def post(self, path: str, payload: dict | None = None, **params):
        return self._call('POST', path, params=params, payload=payload or {})

    def _call(self, method: str, path: str, params: dict, payload: dict | None = None):
        params = {'storeId': self.store_id, **params}

        try:
            return _request(method, path, params=params, payload=payload, auth=self.account.token)
        except UmagAuthError:
            # Токен живёт недолго и протухает молча — меняем его и повторяем.
            self._refresh()
            return _request(method, path, params=params, payload=payload, auth=self.account.token)

    def _refresh(self) -> None:
        body = _request('GET', 'org/login/refresh-token', auth=self.account.token)
        token = None

        if isinstance(body, dict):
            token = body.get('session_token') or body.get('sessionToken')

        if not token:
            raise UmagAuthError('Сессия UMAG истекла — войдите заново')

        self.account.token = token
        self.account.save(update_fields=('token', 'refreshed_at'))


def _request(
    method: str,
    path: str,
    params: dict | None = None,
    payload: dict | None = None,
    auth: str = '',
):
    """Один запрос к UMAG. Возвращает разобранный JSON или текст ответа."""

    url = settings.UMAG_BASE_URL + path
    query = {key: value for key, value in (params or {}).items() if value is not None}

    if query:
        url = f'{url}?{urllib.parse.urlencode(query)}'

    headers = {
        'Accept': 'application/json',
        'api-ver': settings.UMAG_API_VERSION,
        'client-ver': settings.UMAG_CLIENT_VERSION,
    }

    if auth:
        headers['Authorization'] = auth

    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'

    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=settings.UMAG_TIMEOUT) as response:
            return _parse(response.read())
    except urllib.error.HTTPError as error:
        detail = _detail(error.read())
        logger.warning('UMAG %s %s → %s: %s', method, path, error.code, detail[:200])

        if error.code in (401, 403):
            raise UmagAuthError(detail or 'UMAG не принял токен', error.code) from error

        raise UmagError(detail or f'UMAG ответил {error.code}', error.code) from error
    except urllib.error.URLError as error:
        raise UmagError(f'UMAG недоступен: {error.reason}') from error


def _parse(raw: bytes):
    text = raw.decode(errors='replace')

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Часть ответов приходит обычным текстом — например, отказы вроде
        # «Товара с таким штрихкодом не существует».
        return text.strip()


def _detail(raw: bytes) -> str:
    """Достаёт из тела ошибки фразу, которую не стыдно показать человеку."""

    body = _parse(raw)

    if isinstance(body, str):
        return body[:300]

    if isinstance(body, dict):
        for key in ('detail', 'title', 'message', 'error'):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value[:300]

    return str(body)[:300]
