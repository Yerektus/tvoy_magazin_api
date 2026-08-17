from unittest.mock import patch

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from invoices.models import Invoice, InvoiceLine
from invoices.openrouter import OpenRouterError, Parsed

from . import catalog, matching
from .client import UmagClient, UmagError
from .models import SupplierLink, UmagAccount, UmagProduct

User = get_user_model()

STORES = [
    {'id': 17795, 'name': 'Каратал Ерентал ', 'deleted': False},
    {'id': 17796, 'name': 'Еркин Ерентал', 'deleted': False},
]

# Поставщики в кабинете задвоены, а БИН не заполнен ни у кого — на этом
# и держится вся возня с выбором контрагента.
AGENTS = {
    'agents': [
        {'id': 1832935, 'name': 'Жасар-Сауда', 'bin': None},
        {'id': 1891473, 'name': 'жасар сауда', 'bin': None},
        {'id': 2000111, 'name': 'ТОО «Гринхаус»', 'bin': None},
    ],
    'count': 3,
}

PRODUCT = {
    # measure — код единицы: 0 штучный товар, 1 весовой, 2 разливной.
    'product': {'id': 132421277, 'name': 'Напиток PEPSI-COLA ПЭТ 1.0', 'measure': 0},
    'productStorePrice': {'arrivalCost': 333, 'sellingPrice': 400},
    'stockQuantity': 165,
}

# Что отдаёт поиск по названию: похожие карточки, из которых выбирает модель.
BY_PART = [
    {'id': 132421277, 'barcode': 4870145005545, 'name': 'Напиток PEPSI-COLA ПЭТ 1.0'},
    {'id': 91, 'barcode': 4870145005552, 'name': 'Напиток PEPSI-COLA ПЭТ 0.5'},
]


def answered(line_id: int, product_id: int | None, confidence: float) -> Parsed:
    """Ответ модели про одну строку — с ценой запроса, как у настоящего."""

    return Parsed(
        {'matches': [{'line_id': line_id, 'product_id': product_id, 'confidence': confidence}]},
        'qwen/qwen3.7-plus',
        0.0002,
    )


def picked(line_id: int, query: str) -> Parsed:
    """Ответ модели о том, чем искать строку в номенклатуре."""

    return Parsed({'queries': [{'line_id': line_id, 'query': query}]}, 'qwen/qwen3.7-plus', 0.0001)


class FakeUmag:
    """Подменяет сеть: помнит, что и куда ушло."""

    def __init__(self, **answers):
        self.calls = []
        self.answers = answers
        self.fail_on = answers.pop('fail_on', None)

    def __call__(self, method, path, params=None, payload=None, auth=''):
        self.calls.append({'method': method, 'path': path, 'params': params, 'payload': payload})

        if self.fail_on and self.fail_on in path:
            raise UmagError('UMAG ответил 500', 500)

        if path == 'org/login/signin':
            # Пока сотрудник не выбран, токена вместо него не будет.
            if self.answers.get('many_users') and not (params or {}).get('signInAsUser'):
                return {
                    'status': 'user_not_selected',
                    'allowed_users': [
                        {'user_id': 33577, 'label': 'Лариса'},
                        {'user_id': 33578, 'label': 'Ержан'},
                    ],
                }

            return {'sessionToken': 'u33577.token', 'userId': 33577, 'restrictMode': False}
        if path == 'org/store/list':
            return STORES
        if path == 'org/agent/list':
            return AGENTS
        if path == 'nom/product/findProductByBarcode':
            barcode = str(params.get('barcode'))
            if barcode in self.answers.get('known', {'4870145005545'}):
                return PRODUCT
            raise UmagError('Товара с таким штрихкодом не существует', 422)
        if path == 'nom/product/by-part':
            return self.answers.get('by_part', [])
        if path == 'nom/product/report':
            return self.answers.get('report', [])
        if path == 'opr/supplies/v2/create':
            return {'id': 122693174}

        return {}

    def payload(self, needle: str):
        return next(call['payload'] for call in self.calls if needle in call['path'])


def invoice_for(user, **fields) -> Invoice:
    invoice = Invoice.objects.create(
        created_by=user,
        status=Invoice.Status.CHECKED,
        supplier=fields.pop('supplier', 'ТОО «Жасар-Сауда»'),
        number='4000108981',
        issued_at='2026-08-08',
        total='2140.00',
        **fields,
    )
    InvoiceLine.objects.create(
        invoice=invoice,
        position=1,
        name='Напиток PEPSI-COLA ПЭТ 1.0*12',
        barcode='4870145005545',
        quantity='4.000',
        unit='бут.',
        price='535.50',
        total='2142.00',
    )
    return invoice


class ProductQueryTests(SimpleTestCase):
    """Чем ищем товар в кабинете: порядок запросов решает, найдётся ли он."""

    def test_product_word_goes_before_the_longest_one(self):
        # «Ромашка» длиннее, но по ней находятся прокладки и соль для ванны,
        # а карточка называется «коржик ромашка».
        self.assertEqual(
            matching._queries('Коржик Ромашка Сайрам нан 500 гр 1*12 шт'),
            ['Коржик Ромашка', 'Коржик', 'Ромашка'],
        )

    def test_two_first_words_find_the_card_as_is(self):
        self.assertEqual(
            matching._queries('Пряник шоколадный 450гр Сайрам нан 1*14')[0],
            'Пряник шоколадный',
        )

    def test_short_words_and_digits_are_not_searched(self):
        # «нан», «гр», «500» ищут пол-магазина.
        self.assertEqual(matching._queries('Круасан нан 300 гр'), ['Круасан'])

    def test_line_without_words_asks_nothing(self):
        self.assertEqual(matching._queries('1*12'), [])


class CatalogTests(APITestCase):
    """Поиск по своей копии номенклатуры: кабинет ищет подстрокой и промахивается."""

    def setUp(self):
        for barcode, name in (
            ('4870215285914', 'Пряник шоколадный сайрам нан'),
            ('4870215280490', 'КОРЖИК сайрам нан'),
            ('4870000000001', 'Водка зеленая марка традиционная'),
            ('4870000000002', 'Samyang Cheese Ramen 120g.'),
            ('4870000000003', 'Сыр чиз 350гр'),
        ):
            UmagProduct.objects.create(
                store_id=17795,
                barcode=barcode,
                name=name,
                search_name=catalog.normalize(name),
            )

        self.finder = catalog.finder(17795)

    def first(self, name: str) -> str:
        found = self.finder.find(name)
        return found[0]['name'] if found else ''

    def test_finds_card_written_differently(self):
        """В накладной строка длиннее и в другом регистре — товар тот же."""

        self.assertEqual(
            self.first('Пряник шоколадный 450гр Сайрам нан 1*14'),
            'Пряник шоколадный сайрам нан',
        )
        self.assertEqual(self.first('Коржик Сайрам нан 500 гр 1*12'), 'КОРЖИК сайрам нан')

    def test_finds_through_quotes_and_word_order(self):
        """Кавычки обрывали подстроку, а порядок слов ей мешал."""

        self.assertEqual(
            self.first('Водка "Зеленая марка Традиционная рецептура" 40%'),
            'Водка зеленая марка традиционная',
        )
        self.assertEqual(
            self.first('рамен SAMYANG Ramen CHEESE (сырный, желтая пачка)'),
            'Samyang Cheese Ramen 120g.',
        )

    def test_three_letter_word_is_a_product_too(self):
        """«сыр», «нан», «сок» — это товар, а не мусор вроде «шт» и «гр»."""

        self.assertEqual(self.first('Ассорти «ЧИЗ»'), 'Сыр чиз 350гр')

    def test_nothing_alike_returns_nothing(self):
        self.assertEqual(self.finder.find('Jabeg'), [])

    def test_missing_catalog_is_not_an_empty_one(self):
        """Копии магазина нет — пусть работает поиск в самом кабинете."""

        self.assertIsNone(catalog.finder(17796))

    def test_copy_older_than_a_day_asks_to_be_refreshed(self):
        UmagProduct.objects.filter(store_id=17795).update(
            updated_at=timezone.now() - timedelta(days=2)
        )

        self.assertTrue(catalog.stale(17795))
        # Магазин не выгружали ни разу — это тоже повод обновиться.
        self.assertTrue(catalog.stale(17796))
        self.assertFalse(catalog.stale(17795, days=3))

    def test_fresh_copy_is_not_refreshed(self):
        """Свежую копию не трогаем: выгрузка тяжёлая, а товары за час не меняются."""

        account = UmagAccount.objects.create(
            user=User.objects.create_user(email='fresh@tvoymagazin.kz', password='tainy-parol-123'),
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )

        with patch('umag.catalog.refresh') as refresh:
            catalog.refresh_later(account)

        refresh.assert_not_called()

    def test_refresh_replaces_what_the_store_had(self):
        fake = FakeUmag(
            report=[
                {'name': 'Хлеб черный Ер', 'barcode': 4870000000009, 'measure': 'шт'},
                # Без штрихкода товар не предложить — такие пропускаем.
                {'name': 'Пицца', 'barcode': None, 'measure': 'шт'},
            ]
        )
        account = UmagAccount.objects.create(
            user=User.objects.create_user(email='sync@tvoymagazin.kz', password='tainy-parol-123'),
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )

        with patch('umag.client._request', new=fake):
            count = catalog.refresh(UmagClient(account, 17795), 17795)

        self.assertEqual(count, 1)
        # Прежние пять карточек магазина ушли: держим то, что в кабинете сейчас.
        self.assertEqual(
            list(UmagProduct.objects.filter(store_id=17795).values_list('name', flat=True)),
            ['Хлеб черный Ер'],
        )


class UmagAccountTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')
        self.client.force_authenticate(self.user)

    def test_connect_exchanges_password_for_token(self):
        with patch('umag.client._request', new=FakeUmag()):
            response = self.client.post(
                '/api/umag/account/',
                {'phone': '7474419654', 'password': 'ochen-tainy'},
                format='json',
            )

        self.assertEqual(response.status_code, 200)
        # Магазина два, поэтому подключение есть, а отправлять ещё некуда.
        self.assertFalse(response.data['connected'])
        self.assertEqual(len(response.data['stores']), 2)

        account = UmagAccount.objects.get(user=self.user)
        self.assertEqual(account.token, 'u33577.token')
        self.assertIsNone(account.store_id)

    def test_several_employees_on_one_phone(self):
        fake = FakeUmag(many_users=True)

        with patch('umag.client._request', new=fake):
            asked = self.client.post(
                '/api/umag/account/',
                {'phone': '7474419654', 'password': 'ochen-tainy'},
                format='json',
            )

            # Пока сотрудник не выбран, вход не завершён и в базе пусто.
            self.assertEqual(len(asked.data['users']), 2)
            self.assertFalse(UmagAccount.objects.filter(user=self.user).exists())

            done = self.client.post(
                '/api/umag/account/',
                {'phone': '7474419654', 'password': 'ochen-tainy', 'user_id': 33577},
                format='json',
            )

        self.assertNotIn('users', done.data)
        self.assertEqual(UmagAccount.objects.get(user=self.user).token, 'u33577.token')

    def test_password_is_not_stored(self):
        with patch('umag.client._request', new=FakeUmag()):
            self.client.post(
                '/api/umag/account/',
                {'phone': '7474419654', 'password': 'ochen-tainy'},
                format='json',
            )

        stored = UmagAccount.objects.values().get(user=self.user)
        self.assertNotIn('ochen-tainy', str(stored))

    def test_store_choice_finishes_connection(self):
        UmagAccount.objects.create(user=self.user, phone='7474419654', token='u33577.token')

        with patch('umag.client._request', new=FakeUmag()):
            response = self.client.patch('/api/umag/account/', {'store_id': 17795}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['connected'])
        self.assertEqual(response.data['store_name'], 'Каратал Ерентал')


class UmagSupplyTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')
        self.client.force_authenticate(self.user)
        self.account = UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )
        self.invoice = invoice_for(self.user)

    def test_preflight_wants_supplier_chosen(self):
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['ready'])
        # Два «Жасар-Сауда» — сами не выбираем, спрашиваем человека.
        self.assertEqual(len(response.data['supplier']['candidates']), 2)
        self.assertEqual(response.data['lines'][0]['status'], 'ok')

    def test_model_picks_product_for_line_without_barcode(self):
        self.invoice.lines.update(barcode='')
        fake = FakeUmag(by_part=BY_PART)
        line_id = self.invoice.lines.get().pk

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms', return_value=picked(line_id, 'Напиток PEPSI-COLA')
        ), patch(
            'umag.matching.match_products',
            return_value=answered(line_id, 132421277, 0.9),
        ) as model:
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        line = response.data['lines'][0]
        # Штрихкод подставлен — строка сопоставлена наравне с теми, что с бумаги.
        self.assertEqual(line['status'], 'ok')
        self.assertEqual(line['product_name'], 'Напиток PEPSI-COLA ПЭТ 1.0')
        self.assertEqual(line['stock'], 165)
        # Уверенность остаётся при строке: видно, что цифра не из бумаги.
        self.assertEqual(line['confidence'], 0.9)
        self.assertNotIn('Не сопоставлены позиции', ' '.join(response.data['problems']))

        # Модель спрашивают один раз на накладную и вместе с кандидатами.
        self.assertEqual(model.call_count, 1)
        asked = model.call_args.args[0]
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]['id'], line_id)
        self.assertEqual([product['id'] for product in asked[0]['candidates']], [132421277, 91])

        # Выбор лёг в саму строку — и цена запроса в стоимость документа.
        stored = self.invoice.lines.get()
        self.assertEqual(stored.barcode, '4870145005545')
        self.assertEqual(stored.umag_product_id, 132421277)
        self.assertEqual(stored.umag_confidence, 0.9)
        self.invoice.refresh_from_db()
        # Оба запроса к модели — выбор слова и выбор товара — идут в стоимость.
        self.assertEqual(str(self.invoice.cost), '0.000300')

    def test_half_sure_model_still_fills_the_line(self):
        """Половины уверенности хватает: строку человек всё равно сверяет глазами."""

        self.invoice.lines.update(barcode='')
        fake = FakeUmag(by_part=BY_PART)
        line_id = self.invoice.lines.get().pk

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms', return_value=picked(line_id, 'Напиток PEPSI-COLA')
        ), patch(
            'umag.matching.match_products',
            return_value=answered(line_id, 132421277, 0.55),
        ):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        line = response.data['lines'][0]
        self.assertEqual(line['status'], 'ok')
        # Уверенность остаётся при строке: видно, что цифра не из бумаги.
        self.assertEqual(line['confidence'], 0.55)
        self.assertEqual(self.invoice.lines.get().barcode, '4870145005545')

    def test_unsure_model_suggests_nothing(self):
        self.invoice.lines.update(barcode='')
        fake = FakeUmag(by_part=BY_PART)
        line_id = self.invoice.lines.get().pk

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms', return_value=picked(line_id, 'Напиток PEPSI-COLA')
        ), patch(
            'umag.matching.match_products',
            return_value=answered(line_id, 132421277, 0.4),
        ):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        # Догадка на четвёрку — не догадка: неверный товар уедет в остатки.
        self.assertEqual(response.data['lines'][0]['suggested_barcode'], '')
        self.assertIsNone(self.invoice.lines.get().umag_product_id)

    def test_model_chooses_what_to_search_by(self):
        """Слово выбирает модель: в строке товар перемешан с фасовкой."""

        self.invoice.lines.update(barcode='', name='Пряник шоколадный 450гр Сайрам нан 1*14')
        fake = FakeUmag(by_part=BY_PART)
        line_id = self.invoice.lines.get().pk

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms', return_value=picked(line_id, 'Пряник шоколадный сайрам')
        ) as words, patch(
            'umag.matching.match_products',
            return_value=answered(line_id, 132421277, 0.9),
        ):
            self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        # Спрашиваем один раз на накладную, до похода в кабинет.
        self.assertEqual(words.call_count, 1)
        self.assertEqual(words.call_args.args[0], [{'id': line_id, 'name': 'Пряник шоколадный 450гр Сайрам нан 1*14'}])

        # Искали именно тем, что выбрала модель, а не первыми двумя словами.
        searched = [
            call['params']['namePart']
            for call in fake.calls
            if call['path'] == 'nom/product/by-part'
        ]
        self.assertEqual(searched[0], 'Пряник шоколадный сайрам')

    def test_search_falls_back_to_our_rules_when_model_is_silent(self):
        """Модель не ответила — ищем по своим правилам, а не бросаем строку."""

        self.invoice.lines.update(barcode='', name='Коржик Ромашка Сайрам нан 500 гр 1*12')
        fake = FakeUmag(by_part=BY_PART)
        line_id = self.invoice.lines.get().pk

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms',
            side_effect=OpenRouterError('Не задан OPENROUTER_API_KEY'),
        ), patch(
            'umag.matching.match_products',
            return_value=answered(line_id, 132421277, 0.9),
        ):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        searched = [
            call['params']['namePart']
            for call in fake.calls
            if call['path'] == 'nom/product/by-part'
        ]
        self.assertEqual(searched[0], 'Коржик Ромашка')
        self.assertEqual(response.data['lines'][0]['status'], 'ok')

    def test_local_catalog_replaces_the_search_in_umag(self):
        """Копия номенклатуры есть — в кабинет за поиском не ходим вовсе."""

        self.invoice.lines.update(barcode='', name='Пряник шоколадный 450гр Сайрам нан 1*14')
        name = 'Пряник шоколадный сайрам нан'
        UmagProduct.objects.create(
            store_id=17795,
            barcode='4870215285914',
            name=name,
            search_name=catalog.normalize(name),
        )
        fake = FakeUmag(by_part=BY_PART, known={'4870215285914'})
        line_id = self.invoice.lines.get().pk

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms'
        ) as words, patch(
            'umag.matching.match_products',
            return_value=answered(line_id, 1, 0.9),
        ) as model:
            self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        # Ни поиска по подстроке, ни выбора слова для него: и то и другое было
        # нужно только чтобы угодить поиску кабинета.
        self.assertNotIn('nom/product/by-part', [call['path'] for call in fake.calls])
        words.assert_not_called()

        # Модель выбирала из того, что нашлось у нас.
        asked = model.call_args.args[0]
        self.assertEqual([item['name'] for item in asked[0]['candidates']], [name])

        # Штрихкод из карточки лёг в строку — дальше она живёт как с бумаги.
        self.assertEqual(self.invoice.lines.get().barcode, '4870215285914')

    def test_chosen_product_is_not_asked_twice(self):
        self.invoice.lines.update(
            barcode='',
            umag_product_id=132421277,
            umag_product_name='Напиток PEPSI-COLA ПЭТ 1.0',
            umag_barcode='4870145005545',
            umag_confidence=0.75,
        )
        fake = FakeUmag(by_part=BY_PART)

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms'
        ) as words, patch('umag.matching.match_products') as model:
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        model.assert_not_called()
        # Прежний выбор берётся из строки и вписывается без нового запроса.
        self.assertEqual(response.data['lines'][0]['status'], 'ok')
        self.assertEqual(self.invoice.lines.get().barcode, '4870145005545')

    def test_model_failure_leaves_line_to_the_human(self):
        self.invoice.lines.update(barcode='')
        fake = FakeUmag(by_part=BY_PART)
        line_id = self.invoice.lines.get().pk

        with patch('umag.client._request', new=fake), patch(
            'umag.matching.search_terms', return_value=picked(line_id, 'Напиток PEPSI-COLA')
        ), patch(
            'umag.matching.match_products',
            side_effect=OpenRouterError('Не задан OPENROUTER_API_KEY'),
        ):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['lines'][0]['suggested_barcode'], '')

    def test_barcode_match_is_remembered_in_the_line(self):
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        line = response.data['lines'][0]
        self.assertEqual(line['status'], 'ok')
        self.assertEqual(line['product_name'], 'Напиток PEPSI-COLA ПЭТ 1.0')
        self.assertEqual(line['stock'], 165)
        self.assertEqual(line['measure'], 'шт')

        stored = self.invoice.lines.get()
        self.assertEqual(stored.umag_product_id, 132421277)
        self.assertEqual(stored.umag_confidence, 1.0)
        # Единица переехала из карточки: в бумаге были «бут.».
        self.assertEqual(stored.unit, 'шт')

    def test_unknown_barcode_blocks_sending(self):
        self.invoice.lines.update(barcode='0000000000000')
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.post(f'/api/umag/invoices/{self.invoice.pk}/', {}, format='json')

        self.assertEqual(response.status_code, 422)
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.umag_supply_id)

    def test_push_creates_draft_and_remembers_supplier(self):
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['supply_id'], 122693174)
        self.assertIn('122693174', response.data['url'])

        header = fake.payload('/edit')
        self.assertEqual(header['supplierId'], 1832935)
        self.assertEqual(header['docTime'], '2026-08-08T00:00:00')
        self.assertIn('4000108981', header['comment'])

        products = fake.payload('add-products')['products']
        self.assertEqual(
            products,
            [
                {
                    'barcode': 4870145005545,
                    'quantity': 4.0,
                    'discount': 0,
                    'type': 0,
                    'arrivalCost': 535.5,
                    'sellingPrice': 400,
                }
            ],
        )

    def test_barcode_is_cleaned_before_sending(self):
        # Модель дописывает к штрихкоду единицы измерения — в UMAG идут цифры.
        self.invoice.lines.update(barcode='4870145005545 шт')
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(fake.payload('add-products')['products'][0]['barcode'], 4870145005545)

        # Приёмку не проводим: это делает человек в UMAG.
        self.assertNotIn('provide', [call['path'] for call in fake.calls])

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.umag_supply_id, 122693174)
        self.assertIsNotNone(self.invoice.umag_pushed_at)

        # Выбор поставщика запомнился — на следующей накладной спрашивать нечего.
        link = SupplierLink.objects.get(store_id=17795, agent_id=1832935)
        self.assertEqual(link.name, 'жасар сауда')

    def test_invoice_goes_to_its_own_store(self):
        """Магазин переключили после загрузки — приёмка уходит туда, где накладную завели."""

        self.invoice.umag_store_id = 17796
        self.invoice.umag_store_name = 'Еркин Ерентал'
        self.invoice.save(update_fields=('umag_store_id', 'umag_store_name'))

        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        # Ни один запрос не ушёл в текущий магазин сотрудника (17795).
        self.assertEqual({call['params'].get('storeId') for call in fake.calls}, {17796})
        # Связка поставщика тоже принадлежит магазину накладной.
        self.assertTrue(SupplierLink.objects.filter(store_id=17796, agent_id=1832935).exists())

    def test_push_records_store_of_older_invoice(self):
        """Накладную завели до подключения UMAG — запоминаем, куда она уехала."""

        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.umag_store_id, 17795)
        self.assertEqual(self.invoice.umag_store_name, 'Каратал Ерентал')

    def test_remembered_supplier_is_used_next_time(self):
        SupplierLink.objects.create(
            store_id=17795,
            name='жасар сауда',
            agent_id=1832935,
            agent_name='Жасар-Сауда',
        )
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        self.assertTrue(response.data['ready'])
        self.assertEqual(response.data['supplier']['agent_id'], 1832935)

    def test_broken_products_call_removes_the_draft(self):
        fake = FakeUmag(fail_on='add-products')

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn('/delete', [call['path'][-7:] for call in fake.calls])

        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.umag_supply_id)

    def test_unchecked_invoice_is_not_sent(self):
        self.invoice.status = Invoice.Status.DONE
        self.invoice.save(update_fields=('status',))

        with patch('umag.client._request', new=FakeUmag()):
            response = self.client.post(f'/api/umag/invoices/{self.invoice.pk}/', {}, format='json')

        self.assertEqual(response.status_code, 409)

    def test_invoice_is_sent_only_once(self):
        self.invoice.umag_supply_id = 122693174
        self.invoice.save(update_fields=('umag_supply_id',))

        with patch('umag.client._request', new=FakeUmag()):
            response = self.client.post(f'/api/umag/invoices/{self.invoice.pk}/', {}, format='json')

        self.assertEqual(response.status_code, 409)

    def test_without_account_button_is_refused(self):
        self.account.delete()

        response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')
        self.assertEqual(response.status_code, 409)

    def test_other_user_invoice_is_hidden(self):
        other = User.objects.create_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
        UmagAccount.objects.create(
            user=other,
            phone='7770000000',
            token='u1.token',
            store_id=17795,
        )
        self.client.force_authenticate(other)

        response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')
        self.assertEqual(response.status_code, 404)
