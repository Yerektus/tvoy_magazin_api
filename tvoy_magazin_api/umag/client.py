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
import http.client
import json
import logging
import threading
import time
import urllib.parse

from django.conf import settings

logger = logging.getLogger(__name__)

# Закрытый API иногда закрывает чтение по таймауту. GET безопасно повторить:
# он не создаёт приёмки, товары или контрагентов.
GET_RETRY_DELAYS = (1, 3)

# Одно HTTPS-соединение на поток: выгрузка чеков — тысячи GET, и TLS на каждый
# из них дороже самого ответа. Сломанное keep-alive просто открываем заново.
_local = threading.local()
_DEAD_CONNECTION = (
    http.client.RemoteDisconnected,
    http.client.CannotSendRequest,
    http.client.ResponseNotReady,
    http.client.IncompleteRead,
    ConnectionResetError,
    BrokenPipeError,
)


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
        for attempt, delay in enumerate((*GET_RETRY_DELAYS, None)):
            try:
                return self._call('GET', path, params=params)
            except UmagError as error:
                # 4xx и 5xx — настоящий ответ кабинета, повторять его без
                # изменения запроса бессмысленно. Повторяем только сеть/таймаут.
                if error.status is not None or delay is None:
                    raise

                logger.warning(
                    'Повторяем UMAG GET %s после сетевой ошибки (%s/%s): %s',
                    path,
                    attempt + 1,
                    len(GET_RETRY_DELAYS),
                    error,
                )
                time.sleep(delay)

        raise AssertionError('цикл повторов GET должен завершиться')

    def post(self, path: str, payload: dict | None = None, **params):
        return self._call('POST', path, params=params, payload=payload or {})

    def post_form(self, path: str, form: dict, **params):
        """POST обычной формой — так кабинет шлёт контрагентов и импорт товаров."""

        return self._call('POST', path, params=params, form=form)

    def delete(self, path: str, **params):
        return self._call('DELETE', path, params=params)

    def _call(
        self,
        method: str,
        path: str,
        params: dict,
        payload: dict | None = None,
        form: dict | None = None,
    ):
        params = {'storeId': self.store_id, **params}
        call = dict(params=params, payload=payload, form=form)

        try:
            return _request(method, path, **call, auth=self.account.token)
        except UmagAuthError:
            # Токен живёт недолго и протухает молча — меняем его и повторяем.
            self._refresh()
            return _request(method, path, **call, auth=self.account.token)

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
    form: dict | None = None,
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
        'Connection': 'keep-alive',
    }

    if auth:
        headers['Authorization'] = auth

    data = None
    if form is not None:
        # Часть кабинета шлёт не JSON, а обычную форму: значения-объекты в ней
        # лежат JSON-строками. На JSON такие адреса отвечают 415.
        data = urllib.parse.urlencode(
            {
                key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
                for key, value in form.items()
            }
        ).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded'
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'

    parsed = urllib.parse.urlsplit(url)
    target = parsed.path + (f'?{parsed.query}' if parsed.query else '')

    try:
        status, raw = _send(parsed, method, target, headers, data)
    except TimeoutError as error:
        _drop_connection()
        raise UmagError('UMAG не ответил вовремя') from error
    except OSError as error:
        _drop_connection()
        raise UmagError(f'UMAG недоступен: {error}') from error

    if status >= 400:
        detail = _detail(raw)
        logger.warning('UMAG %s %s → %s: %s', method, path, status, detail[:200])

        if status in (401, 403):
            raise UmagAuthError(detail or 'UMAG не принял токен', status)

        raise UmagError(detail or f'UMAG ответил {status}', status)

    return _parse(raw)


def _send(parsed, method: str, target: str, headers: dict, data: bytes | None):
    """Отправляет запрос по keep-alive. Мёртвое соединение открываем ещё раз."""

    try:
        return _exchange(parsed, method, target, headers, data)
    except _DEAD_CONNECTION:
        _drop_connection()
        # Повтор POST мог бы создать вторую приёмку. GET безопасно повторить.
        if method != 'GET':
            raise
        return _exchange(parsed, method, target, headers, data)


def _exchange(parsed, method: str, target: str, headers: dict, data: bytes | None):
    conn = _connection(parsed)
    conn.request(method, target, body=data, headers=headers)
    response = conn.getresponse()
    raw = response.read()

    if response.will_close:
        _drop_connection()

    return response.status, raw


def _connection(parsed):
    key = (parsed.scheme, parsed.hostname, parsed.port)
    slot = getattr(_local, 'slot', None)

    if slot is not None and slot[0] == key:
        return slot[1]

    _drop_connection()
    timeout = settings.UMAG_TIMEOUT
    host = parsed.hostname or ''
    port = parsed.port

    if parsed.scheme == 'https':
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    _local.slot = (key, conn)
    return conn


def _drop_connection() -> None:
    slot = getattr(_local, 'slot', None)

    if slot is None:
        return

    try:
        slot[1].close()
    except Exception:  # noqa: BLE001 — закрыть мёртвый сокет важнее причины
        pass

    _local.slot = None


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
