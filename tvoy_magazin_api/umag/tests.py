from unittest.mock import patch

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.tests import make_user
from invoices.models import Invoice, InvoiceLine

from . import catalog, supply
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


class FakeUmag:
    """Подменяет сеть: помнит, что и куда ушло."""

    def __init__(self, **answers):
        self.calls = []
        self.answers = answers
        self.fail_on = answers.pop('fail_on', None)

    def __call__(self, method, path, params=None, payload=None, form=None, auth=''):
        # Часть адресов кабинета принимает не JSON, а форму — запоминаем оба вида.
        self.calls.append(
            {'method': method, 'path': path, 'params': params, 'payload': payload or form}
        )

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
        if path == 'nom/category/find-categories':
            return self.answers.get(
                'categories',
                [{'id': 900, 'name': 'Незаданные'}, {'id': 901, 'name': 'Напитки'}],
            )
        if path == 'nom/product/create':
            product = (form or {}).get('productJson') or {}
            return {**product, 'id': 5150001}
        if path == 'nom/product/by-part':
            return self.answers.get('by_part', [])
        if path == 'nom/product/report':
            return self.answers.get('report', [])
        if path == 'nom/product-v1/findNextInnerBarcode':
            return {'barcode': 2110000043940}
        if path == 'opr/supplies/v2/create':
            return {'id': 122693174}
        if path == 'org/agent/create':
            agent = (form or {}).get('agentJson') or {}
            return {**agent, 'id': 2100777}

        return {}

    def payload(self, needle: str):
        return next(call['payload'] for call in self.calls if needle in call['path'])


def invoice_for(user, **fields) -> Invoice:
    invoice = Invoice.objects.create(
        organization=user.organization,
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
            user=make_user(email='fresh@tvoymagazin.kz', password='tainy-parol-123'),
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
            user=make_user(email='sync@tvoymagazin.kz', password='tainy-parol-123'),
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
        self.user = make_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')
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
        self.user = make_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')
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

    def test_line_without_barcode_is_left_to_the_human(self):
        """По названию товар в кабинете больше не ищем.

        Раньше строку без штрихкода отдавали модели вместе с дюжиной похожих
        карточек, и она регулярно выбирала чужую: в кабинете тысячи товаров, а
        различаются они одним словом. Приход на чужую карточку — пересорт.
        Теперь строка честно остаётся несопоставленной: штрихкод подберётся по
        прошлым накладным при разборе или его впишет человек.
        """

        self.invoice.lines.update(barcode='')
        fake = FakeUmag(by_part=BY_PART)

        with patch('umag.client._request', new=fake):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        self.assertEqual(response.status_code, 200)

        line = response.data['lines'][0]
        self.assertEqual(line['status'], 'no_barcode')
        self.assertEqual(line['suggested_barcode'], '')

        # Ни поиска по названию, ни запроса к модели — и платить не за что.
        self.assertNotIn('nom/product/by-part', [call['path'] for call in fake.calls])
        self.assertIsNone(self.invoice.lines.get().umag_confidence)

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

    def test_unknown_barcode_starts_a_new_product_instead_of_guessing(self):
        """Штрихкод с бумаги главнее названия.

        Его нет в кабинете — значит товар новый: заводим карточку с этим же
        кодом. Приклеить строку к похожему по названию значило бы принять
        приход на чужую карточку, а это пересорт.
        """

        self.invoice.lines.update(barcode='0000000000000')
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(response.status_code, 201)

        # По названию не искали вовсе — ни в кабинете, ни моделью.
        self.assertNotIn('nom/product/by-part', [call['path'] for call in fake.calls])

        card = fake.payload('nom/product/create')['productJson']
        self.assertEqual(card['barcode'], '0000000000000')
        # Код карточки в кабинете — тот же штрихкод.
        self.assertEqual(card['code'], '0000000000000')
        self.assertEqual(card['name'], 'Напиток PEPSI-COLA ПЭТ 1.0*12')
        self.assertEqual(card['categoryId'], 900)

        # И строка ушла в приёмку под своим штрихкодом.
        products = fake.payload('add-products')['products']
        self.assertEqual(products[0]['barcode'], 0)

    def test_new_product_gets_the_price_from_the_invoice(self):
        """Цену на полке ставим равной приходу.

        Своей цены у нового товара нет, а ноль означал бы, что касса отдаст его
        даром. Настоящую наценку магазин выставит сам.
        """

        self.invoice.lines.update(barcode='0000000000000')
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        price = next(
            call['payload']['productStorePriceJson']
            for call in fake.calls
            if call['path'] == 'nom/product/create'
        )

        self.assertEqual(price['arrivalCost'], 535.5)
        self.assertEqual(price['sellingPrice'], 535.5)
        self.assertEqual(price['storeId'], 17795)

    def test_weight_goods_are_created_as_weight_goods(self):
        """Единицу берём из накладной: весовой товар в кабинете отдельного типа."""

        self.invoice.lines.update(barcode='0000000000000', unit='кг')
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        card = fake.payload('nom/product/create')['productJson']

        self.assertEqual(card['measure'], 1)
        self.assertEqual(card['type'], 1)

    def test_nothing_is_created_while_only_looking(self):
        """Осмотр остаётся чтением: заведение товара — дело отправки."""

        self.invoice.lines.update(barcode='0000000000000')
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertNotIn('nom/product/create', [call['path'] for call in fake.calls])

        # И такая строка отправке не мешает: товар заведётся сам. Про
        # поставщика кабинет спросить может — это другой разговор.
        self.assertEqual(response.data['lines'][0]['status'], 'new_product')
        self.assertNotIn(
            'Не сопоставлены позиции',
            ' '.join(response.data['problems']),
        )

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
        # Ссылку на черновик собирает фронт: в адресе кабинета стоит порядковый
        # номер магазина, а список магазинов есть только у него.
        self.assertNotIn('url', response.data)

        header = fake.payload('/edit')
        self.assertEqual(header['supplierId'], 1832935)
        # Дата — с бумаги, время — когда сняли: полночь ставит приход раньше
        # утренних продаж, и товар оказывается проданным до привоза.
        scanned = timezone.localtime(self.invoice.created_at).strftime('%H:%M:%S')
        self.assertEqual(header['docTime'], f'2026-08-08T{scanned}')
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

    def test_new_card_is_filled_from_the_line(self):
        """Что написать в карточке, человек указывает в самой позиции.

        Иначе товар уезжал в «Незаданные» под названием с бумаги и с ценой,
        равной приходу, — и всё это правили потом в кабинете руками.
        """

        line = self.invoice.lines.get()
        line.barcode = '4870145009999'
        line.umag_new_name = 'Пепси 1 л'
        line.umag_new_measure = 2
        line.umag_new_category_id = 901
        line.umag_new_selling_price = 700
        line.save()

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        created = fake.payload('nom/product/create')
        product = created['productJson']
        price = created['productStorePriceJson']

        self.assertEqual(product['name'], 'Пепси 1 л')
        self.assertEqual(product['measure'], 2)
        self.assertEqual(product['categoryId'], 901)
        self.assertEqual(price['sellingPrice'], 700)
        # Цена прихода — из накладной, её человек не выдумывает.
        self.assertEqual(price['arrivalCost'], 535.5)

    def test_missing_product_is_marked_in_the_line(self):
        """Строка знает, что товара в кабинете нет: по этому карточка позиции
        и показывает поля новой карточки."""

        line = self.invoice.lines.get()
        line.barcode = '4870145009999'
        line.save(update_fields=('barcode',))

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        line.refresh_from_db()
        self.assertTrue(line.umag_missing)

        # Штрихкод поправили на знакомый — отметка снимается сама.
        line.barcode = '4870145005545'
        line.save(update_fields=('barcode',))

        with patch('umag.client._request', new=fake):
            self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')

        line.refresh_from_db()
        self.assertFalse(line.umag_missing)

    def test_categories_come_from_the_cabinet(self):
        fake = FakeUmag()

        with patch('umag.client._request', new=fake):
            response = self.client.get('/api/umag/categories/')

        self.assertEqual(
            response.data['categories'],
            [{'id': 900, 'name': 'Незаданные'}, {'id': 901, 'name': 'Напитки'}],
        )

    def test_line_without_barcode_gets_an_inner_code(self):
        """Кода на упаковке нет — берём внутренний у кабинета и заводим товар.

        Раньше такая строка просто не давала отправить накладную, и человек
        заводил товар руками, а потом искал, чем её сопоставить.
        """

        line = self.invoice.lines.get()
        line.barcode = ''
        line.save(update_fields=('barcode',))

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            response = self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(response.status_code, 201)

        created = fake.payload('nom/product/create')['productJson']
        self.assertEqual(created['barcode'], '2110000043940')
        # Внутренний код — своя разновидность карточки, кабинет их различает.
        self.assertEqual(created['type'], 2)

        # Строка накладной знает, под каким кодом уехал товар.
        line.refresh_from_db()
        self.assertEqual(line.barcode, '2110000043940')
        self.assertTrue(line.barcode_auto)

        # И этот же код уходит в приёмку.
        products = fake.payload('add-products')['products']
        self.assertEqual(products[0]['barcode'], 2110000043940)

    def test_two_lines_without_barcodes_get_different_codes(self):
        """Кабинет узнаёт о занятом коде только после создания товара."""

        line = self.invoice.lines.get()
        line.barcode = ''
        line.save(update_fields=('barcode',))
        InvoiceLine.objects.create(
            invoice=self.invoice,
            position=2,
            name='Коржик Ромашка',
            barcode='',
            quantity=3,
            unit='шт',
            price=200,
            total=600,
        )

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        codes = [
            call['payload']['productJson']['barcode']
            for call in fake.calls
            if call['path'] == 'nom/product/create'
        ]

        self.assertEqual(codes, ['2110000043940', '2110000043957'])

    def test_double_lines_go_as_one(self):
        """Модель прочитала строку бумаги дважды — в приёмку уходит одна.

        Кабинет на две строки с одним штрихкодом и ценой отвечает пятисоткой,
        а по смыслу это одна поставка того же товара.
        """

        line = self.invoice.lines.get()
        InvoiceLine.objects.create(
            invoice=self.invoice,
            position=2,
            name=line.name,
            barcode=line.barcode,
            quantity=line.quantity,
            unit=line.unit,
            price=line.price,
            total=line.total,
        )

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        products = fake.payload('add-products')['products']

        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['quantity'], 8)

    def test_failed_step_is_named_in_the_error(self):
        """«Unhandled Server Error» без шага не говорит ничего.

        По названию шага видно хотя бы, шапку кабинет не принял, товар или
        строки, — иначе искать причину негде.
        """

        fake = FakeUmag(fail_on='add-products')

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn('шаг: позиции', response.data['detail'])

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
        """Порядок: сначала «Проверено», потом отправка. Непроверенную не берём."""

        self.invoice.status = Invoice.Status.DONE
        self.invoice.save(update_fields=('status',))

        with patch('umag.client._request', new=FakeUmag()):
            response = self.client.post(
                f'/api/umag/invoices/{self.invoice.pk}/',
                {'agent_id': 1832935},
                format='json',
            )

        self.assertEqual(response.status_code, 409)
        self.invoice.refresh_from_db()
        self.assertIsNone(self.invoice.umag_supply_id)

    def test_unknown_supplier_is_created_in_umag(self):
        """Контрагента, которого в кабинете нет, заводим сами — иначе приёмку не создать."""

        unknown = invoice_for(self.user, supplier='ТОО «КАРАВАН КАРАВАН»')
        unknown.refresh_from_db()

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            supply.push(unknown, self.account)

        created = fake.payload('org/agent/create')['agentJson']
        self.assertEqual(created['name'], 'ТОО «КАРАВАН КАРАВАН»')
        self.assertEqual(created['type'], 'SUPPLIER')
        # Правовую форму берём из названия, а БИН не шлём: к нему кабинет
        # требует ещё и юридическое название.
        self.assertEqual(created['legalType'], 'ТОО')
        self.assertEqual(created['bin'], '')

        # Связка запомнена — второй раз того же поставщика заводить не нужно.
        link = SupplierLink.objects.get(store_id=17795, agent_id=2100777)
        self.assertEqual(link.agent_name, 'ТОО «КАРАВАН КАРАВАН»')

    def test_supplier_with_namesakes_is_not_created(self):
        """Тёзки в кабинете есть — выбирает человек, дубль не плодим."""

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            response = self.client.post(f'/api/umag/invoices/{self.invoice.pk}/', {}, format='json')

        self.assertEqual(response.status_code, 422)
        self.assertNotIn('org/agent/create', [call['path'] for call in fake.calls])

    def test_preflight_creates_nobody(self):
        """Осмотр — это чтение: смотреть на «Проверку» можно сколько угодно."""

        unknown = invoice_for(self.user, supplier='ТОО «КАРАВАН КАРАВАН»')

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            self.client.get(f'/api/umag/invoices/{unknown.pk}/')

        self.assertNotIn('org/agent/create', [call['path'] for call in fake.calls])
        self.assertFalse(SupplierLink.objects.exists())

    def test_legal_type_is_read_from_the_name(self):
        self.assertEqual(supply._legal_type('ТОО «Караван»'), 'ТОО')
        self.assertEqual(supply._legal_type('ИП Немельбаева'), 'ИП')
        self.assertEqual(supply._legal_type('«Товарищество с ограниченной…»'), 'ТОО')
        # Ничего не подсказывает — берём самое частое у поставщиков магазина.
        self.assertEqual(supply._legal_type('Прайм Алко'), 'ТОО')

    def test_account_state_carries_the_store_list(self):
        """Без списка магазинов ссылка на приёмку ведёт не в тот магазин.

        В адресе кабинета стоит порядковый номер магазина, и считает его фронт —
        значит список должен приходить с обычным состоянием, а не только сразу
        после входа.
        """

        # Список кабинет отдаёт при смене магазина — там он и запоминается.
        with patch('umag.client._request', new=FakeUmag()):
            self.client.patch('/api/umag/account/', {'store_id': 17796}, format='json')
            response = self.client.get('/api/umag/account/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [store['id'] for store in response.data['stores']],
            [17795, 17796],
        )

    def test_old_account_gets_its_stores_once(self):
        """Учётки, заведённые до появления поля, дочитывают список один раз.

        Иначе у них он остался бы пустым навсегда, а ссылка на приёмку снова
        вела бы в первый магазин.
        """

        self.assertEqual(self.account.stores, [])

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            first = self.client.get('/api/umag/account/')
            calls_after_first = len(fake.calls)
            self.client.get('/api/umag/account/')

        self.assertEqual([s['id'] for s in first.data['stores']], [17795, 17796])
        # Второе чтение уже бесплатное: список лежит в базе.
        self.assertEqual(len(fake.calls), calls_after_first)

    def test_reading_state_does_not_touch_umag(self):
        """Состояние читается из базы: поход в кабинет тут стоил бы 504.

        Раньше список магазинов запрашивался на каждое чтение. UMAG отвечает не
        быстро, и страница вставала на его время — на проде это заканчивалось
        таймаутом шлюза.
        """

        self.account.stores = [{'id': 17795, 'name': 'Каратал Ерентал'}]
        self.account.save(update_fields=('stores',))

        fake = FakeUmag()
        with patch('umag.client._request', new=fake):
            response = self.client.get('/api/umag/account/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['stores'], self.account.stores)
        self.assertEqual(fake.calls, [], 'чтение состояния не должно ходить в UMAG')

    def test_unknown_supplier_is_not_a_blocker_anymore(self):
        """Осмотр не должен ругаться на то, что отправка починит сама.

        Фронт по первой же проблеме показывает ошибку и до отправки не доходит,
        поэтому «нет в UMAG» здесь означало бы, что контрагент не заведётся уже
        никогда.
        """

        unknown = invoice_for(self.user, supplier='ТОО «КАРАВАН КАРАВАН»')

        with patch('umag.client._request', new=FakeUmag()):
            response = self.client.get(f'/api/umag/invoices/{unknown.pk}/')

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data['ready'], response.data['problems'])
        self.assertEqual(response.data['problems'], [])

    def test_nameless_supplier_still_blocks(self):
        """А вот пустое название заводить нечем — тут нужен человек."""

        nameless = invoice_for(self.user, supplier='')

        with patch('umag.client._request', new=FakeUmag()):
            response = self.client.get(f'/api/umag/invoices/{nameless.pk}/')

        self.assertFalse(response.data['ready'])
        self.assertIn('не распознан', response.data['problems'][0])

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
        other = make_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
        UmagAccount.objects.create(
            user=other,
            phone='7770000000',
            token='u1.token',
            store_id=17795,
        )
        self.client.force_authenticate(other)

        response = self.client.get(f'/api/umag/invoices/{self.invoice.pk}/')
        self.assertEqual(response.status_code, 404)
