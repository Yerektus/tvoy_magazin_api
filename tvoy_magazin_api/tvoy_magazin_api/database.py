"""Настройки базы из адреса в переменной окружения.

Railway, как и почти любой хостинг, отдаёт базу одной строкой `DATABASE_URL`
вида `postgresql://user:пароль@host:5432/railway`. Отдельную библиотеку ради
разбора этой строки тянуть незачем: `urllib` умеет всё, что нужно, а пароль в
ней бывает с процентными экранированиями — их нужно раскрыть.
"""

from urllib.parse import unquote, urlparse

# Соединение живёт между запросами: база в другом контейнере, и каждый новый
# коннект — это лишний сетевой круг и рукопожатие TLS.
CONN_MAX_AGE = 600


def from_url(url: str) -> dict:
    """Настройки Django для одной базы. Схему не разбираем: у нас только Postgres."""

    parsed = urlparse(url)
    options = {}

    # Railway внутри своей сети шифрование не требует, а снаружи — требует.
    # Значение подставляем только если его задали: иначе решает сам драйвер.
    sslmode = _query(parsed.query, 'sslmode')

    if sslmode:
        options['sslmode'] = sslmode

    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': parsed.path.lstrip('/'),
        'USER': unquote(parsed.username or ''),
        'PASSWORD': unquote(parsed.password or ''),
        'HOST': parsed.hostname or '',
        'PORT': str(parsed.port or ''),
        'CONN_MAX_AGE': CONN_MAX_AGE,
        'OPTIONS': options,
    }


def _query(query: str, name: str) -> str:
    """Значение параметра из хвоста адреса. Пусто — параметра там нет."""

    for pair in query.split('&'):
        key, _, value = pair.partition('=')

        if key == name:
            return unquote(value)

    return ''
