# tvoy-magazin-api

Бэкенд «Твоего магазина»: вход по почте и паролю, приём фотографий накладных и
их разбор vision-моделью через OpenRouter.

Django 6.1 + DRF + SimpleJWT, Python 3.12, зависимости через `uv`.

## Запуск

База — Postgres, такая же, как на хостинге. Локально она живёт в контейнере:

```bash
docker compose up -d          # поднять, данные останутся в томе
docker compose down           # остановить
docker compose down -v        # остановить и стереть данные
```

Адрес базы лежит в `DATABASE_URL` (см. `.env.example`). Убрать эту строку —
Django вернётся на файловый SQLite, и Postgres для работы не понадобится.

```bash
export OPENROUTER_API_KEY=sk-or-...
uv run python tvoy_magazin_api/manage.py migrate
uv run python tvoy_magazin_api/manage.py createsuperuser   # спросит почту и пароль
uv run python tvoy_magazin_api/manage.py runserver
```

Фотографии накладных и оформление админки раздаёт сам Django (nginx перед
ним нет), поэтому при деплое нужен `collectstatic`:

```bash
uv run python tvoy_magazin_api/manage.py collectstatic --noinput
```

Тесты:

```bash
uv run python tvoy_magazin_api/manage.py test accounts invoices
```

Приложения указываются явно: `manage.py` лежит на уровень ниже корня проекта,
и автопоиск тестов из корня их не находит.

Проверить ключ и промпт на конкретном фото, не поднимая сервер:

```bash
uv run python tvoy_magazin_api/manage.py parse_invoice ~/Downloads/nakladnaya.jpeg
```

## Переменные окружения

| Переменная | По умолчанию | Зачем |
| --- | --- | --- |
Настройки читаются из `.env` в корне проекта (см. `.env.example`); переменные
окружения важнее файла.

| `OPENROUTER_API_KEY` | — | Ключ OpenRouter, без него разбор падает с ошибкой |
| `OPENROUTER_VISION_MODEL` | `openai/gpt-5.6-luna` | Модель распознавания накладной с фото |
| `OPENROUTER_MATCH_MODEL` | `deepseek/deepseek-v4-flash` | Модель сопоставления позиций и поставщиков |
| `OPENROUTER_FALLBACK_MODELS` | — | Запасные модели через запятую, если основная занята |
| `OPENROUTER_TIMEOUT` | `120` | Таймаут запроса к модели, секунды |
| `INVOICE_PARSE_CONCURRENCY` | `1` | Сколько накладных разбирается одновременно |
| `INVOICE_PARSE_INLINE` | — | `1` — разбирать прямо в запросе, без фонового потока |
| `DATABASE_URL` | — | Адрес Postgres. Пусто — работает файловый SQLite |
| `DJANGO_DB_NAME` | `db.sqlite3` | Файл SQLite, когда `DATABASE_URL` не задан |
| `DJANGO_MEDIA_ROOT` | `media/` рядом с кодом | Куда складывать фотографии: на хостинге — примонтированный том |
| `ACCESS_TOKEN_HOURS` | `12` | Время жизни access-токена |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:4200,...` | Откуда пускаем фронт |
| `DJANGO_DEBUG` | выключена | `1` — трейсбеки в браузере. В проде не включать |
| `DJANGO_STATIC_ROOT` | `staticfiles/` | Куда `collectstatic` кладёт оформление админки |
| `CSRF_TRUSTED_ORIGINS` | — | Домен админки, иначе вход не пройдёт проверку CSRF |
| `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` | dev-значения | Для продакшена задать обязательно |

## API

Авторизация — один access-токен, refresh не выдаётся. Заголовок:
`Authorization: Bearer <access>`.

| Метод | Адрес | Что делает |
| --- | --- | --- |
| `POST` | `/api/auth/login/` | `{email, password}` → `{access, user}` |
| `GET` | `/api/auth/me/` | Текущий пользователь |
| `GET` | `/api/invoices/` | Свои накладные, постранично по 20 |
| `POST` | `/api/invoices/` | `multipart/form-data` с полем `image` → `{id, status: "pending"}` |
| `GET` | `/api/invoices/<id>/` | Накладная с позициями и статусом разбора |
| `POST` | `/api/invoices/<id>/retry/` | Перезапустить разбор |
| `DELETE` | `/api/invoices/<id>/` | Удалить накладную |
| `GET/POST/PATCH/DELETE` | `/api/umag/account/` | Подключение к UMAG: состояние, вход `{phone, password}`, выбор магазина `{store_id}`, отключение |
| `GET` | `/api/umag/stores/` | Магазины компании в UMAG |
| `GET` | `/api/umag/invoices/<id>/` | Что мешает отправить накладную: поставщик и каждая строка |
| `POST` | `/api/umag/invoices/<id>/` | Создать черновик приёмки, `{agent_id}` — выбранный поставщик |

### Накладная в UMAG

Проверенная накладная уходит в кабинет UMAG черновиком приёмки: `create` →
`edit` → `add-products`. Проведение (`provide`) мы не вызываем никогда — это
движение по складу и деньгам, его делает человек в кабинете.

Отправка возможна, когда каждая строка нашлась в номенклатуре UMAG по
штрихкоду, а поставщик выбран. Товары заводит человек в самом UMAG; если у
строки нет штрихкода, но по названию нашёлся ровно один товар — его штрихкод
предлагается подставить.

Вход у каждого сотрудника свой — по номеру телефона, как в самом кабинете:
пароль обменивается на токен сессии и не сохраняется. Адреса и тела запросов кабинета — в `umag-api.md` в корне проекта.

| Переменная | По умолчанию | Зачем |
| --- | --- | --- |
| `UMAG_BASE_URL` | `https://api.umag.kz/rest/cabinet/` | Адрес кабинета |
| `UMAG_API_VERSION` | `1.4` | Заголовок `api-ver`, без него ответ 400 |
| `UMAG_CLIENT_VERSION` | `angular_cabinet_20.0.24` | Заголовок `client-ver` |
| `UMAG_TIMEOUT` | `30` | Таймаут запроса, секунды |
| `UMAG_SUPPLY_URL` | `https://web.umag.kz/store/0/supplies/{id}/edit` | Ссылка на черновик для интерфейса |

### Как идёт разбор

`POST /api/invoices/` отвечает сразу, распознавание уходит в фоновый поток.
Фронт опрашивает `GET /api/invoices/<id>/` и смотрит `status`:

`pending` → `processing` → `done` либо `failed` (текст в поле `error`).

Очередь живёт в памяти процесса. Если сервер перезапустить во время разбора,
накладные останутся в `pending` — вернуть их в очередь:

```bash
uv run python tvoy_magazin_api/manage.py requeue_invoices          # застрявшие
uv run python tvoy_magazin_api/manage.py requeue_invoices --failed # и упавшие
```

Ошибки провайдера (429, 502, 503, 504 и таймауты) повторяются трижды с паузами
5, 15 и 40 секунд — бесплатные модели регулярно отвечают отказом из-за общего пула.

Пример готовой накладной:

```json
{
  "id": 3,
  "status": "done",
  "supplier": "ТОО «ЖЕТЫСУ-ТРЕЙД»",
  "supplier_bin": "140940011293",
  "number": "4000108981",
  "issued_at": "2026-08-08",
  "total": "32142.00",
  "lines": [
    {
      "position": 1,
      "name": "Напиток PEPSI-COLA ПЭТ 1.0*12",
      "barcode": "4870145005545",
      "quantity": "60.000",
      "unit": "бут.",
      "price": "535.50",
      "total": "32130.00"
    }
  ]
}
```

Сохраняются только те поля, что реально есть в бумаге. Остаток, наценка,
продажная цена и скидка сюда не попадают — их место на стороне товара.
