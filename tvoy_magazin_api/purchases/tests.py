from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APITestCase

from accounts.tests import make_user
from umag.models import UmagAccount

from . import planner
from .models import PurchasePlan

User = get_user_model()


def product(**fields) -> dict:
    """Строка товарного отчёта UMAG."""

    return {
        'productName': 'Пепси 1 л',
        'barcode': 4870145005545,
        'measure': 'шт',
        'saleQuantity': 60,
        'refundQuantity': 0,
        'saleArrivalAmount': 30000,
        'stockQuantity': 10,
        **fields,
    }


class FakeReport:
    """Подменяет сеть: товарный отчёт и поставщиков отдаём сами.

    `by_supplier` — какие товары у какого поставщика: так кабинет отвечает на
    тот же отчёт с `supplierId`.
    """

    def __init__(self, rows, by_supplier=None):
        self.rows = rows
        self.by_supplier = by_supplier or {}
        self.calls = []

    def __call__(self, method, path, params=None, payload=None, auth=''):
        self.calls.append(path)
        params = params or {}

        if path == planner.REPORT:
            supplier = params.get('supplierId')
            rows = self.by_supplier.get(supplier, []) if supplier else self.rows
            return {'count': len(rows), 'data': rows}

        if path == planner.SUPPLIER_REPORT:
            return {'data': [{'supplierName': name} for name in self.suppliers()]}

        if path == planner.AGENTS:
            return [{'id': agent, 'name': name} for agent, name in self.by_supplier_names()]

        return {}

    def suppliers(self) -> list[str]:
        return [name for _, name in self.by_supplier_names()]

    def by_supplier_names(self) -> list[tuple[int, str]]:
        return [(agent, f'Поставщик {agent}') for agent in self.by_supplier]


class PlannerMathTests(SimpleTestCase):
    """Считаем без сети: расход в день, запас и сколько дозаказать."""

    def test_orders_up_to_the_horizon(self):
        # 60 штук за 30 дней — две в день. На две недели нужно 28, есть 10.
        line = planner._line(product(), days=30, horizon=14)

        self.assertEqual(line['per_day'], Decimal('2.000'))
        self.assertEqual(line['cover_days'], Decimal('5.0'))
        self.assertEqual(line['suggested'], Decimal('18'))
        # Закупочная — из суммы прихода на проданное: 30000 / 60.
        self.assertEqual(line['price'], Decimal('500.00'))
        self.assertEqual(line['cost'], Decimal('9000.00'))

    def test_enough_stock_is_not_planned(self):
        # Хватает на месяц вперёд — заказывать нечего.
        self.assertIsNone(planner._line(product(stockQuantity=100), days=30, horizon=14))

    def test_product_without_sales_is_not_planned(self):
        self.assertIsNone(planner._line(product(saleQuantity=0), days=30, horizon=14))

    def test_returns_reduce_the_demand(self):
        # Половину вернули — расход вдвое меньше, чем продажи.
        line = planner._line(product(refundQuantity=30), days=30, horizon=14)
        self.assertEqual(line['per_day'], Decimal('1.000'))

    def test_negative_stock_counts_as_empty_shelf(self):
        # Пересорт в кабинете: минус на остатке — это тот же ноль.
        line = planner._line(product(stockQuantity=-5), days=30, horizon=14)

        self.assertEqual(line['cover_days'], Decimal('0.0'))
        self.assertEqual(line['suggested'], Decimal('28'))

    def test_weighted_goods_are_ordered_with_a_hundredth(self):
        line = planner._line(
            product(measure='кг', saleQuantity=Decimal('3.4'), stockQuantity=0),
            days=30,
            horizon=7,
        )

        # 3.4 кг за 30 дней — 0.79333 кг на неделю, округляем вверх.
        self.assertEqual(line['suggested'], Decimal('0.80'))

    def test_piece_goods_are_ordered_whole(self):
        line = planner._line(product(saleQuantity=31, stockQuantity=0), days=30, horizon=1)

        # Чуть больше одной штуки в день — заказываем две, а не 1.03.
        self.assertEqual(line['suggested'], Decimal('2'))


@override_settings(INVOICE_PARSE_INLINE=True)
class PlanningApiTests(APITestCase):
    def setUp(self):
        self.user = make_user(email='shop@tvoymagazin.kz', password='tainy-parol-123')
        self.client.force_authenticate(self.user)

    def connect_umag(self):
        return UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='u33577.token',
            store_id=17795,
            store_name='Каратал Ерентал',
        )

    def install(self):
        self.connect_umag()
        return self.client.post('/api/purchases/access/', {}, format='json')

    def test_extension_needs_umag_first(self):
        response = self.client.post('/api/purchases/access/', {}, format='json')

        self.assertEqual(response.status_code, 409)
        self.assertFalse(self.client.get('/api/purchases/access/').data['connected'])

    def test_extension_connects_before_the_store_is_chosen(self):
        """Магазин выбирают в шапке — подключению расширения он не мешает."""

        UmagAccount.objects.create(user=self.user, phone='7474419654', token='u33577.token')

        self.assertTrue(self.client.post('/api/purchases/access/', {}, format='json').data['connected'])

    def test_extension_connects_and_disconnects(self):
        self.assertTrue(self.install().data['connected'])
        self.assertTrue(self.client.get('/api/purchases/access/').data['connected'])

        self.assertFalse(self.client.delete('/api/purchases/access/').data['connected'])

    def test_disconnecting_umag_turns_the_extension_off(self):
        """Без UMAG планировать нечем — расширение снимается следом за ним."""

        self.assertTrue(self.install().data['connected'])

        with patch('umag.client._request', new=FakeReport([])):
            self.client.delete('/api/umag/account/')

        self.assertFalse(self.client.get('/api/purchases/access/').data['connected'])

    def test_plan_needs_the_extension(self):
        self.connect_umag()
        response = self.client.post('/api/purchases/plan/', {}, format='json')

        self.assertEqual(response.status_code, 409)

    def test_plan_is_empty_until_counted(self):
        self.install()
        self.assertEqual(self.client.get('/api/purchases/plan/').status_code, 204)

    def test_plan_counts_what_to_order(self):
        self.install()
        fake = FakeReport([
            product(),
            product(productName='Молоко', barcode=111, stockQuantity=200),
        ])

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                '/api/purchases/plan/',
                {'days': 30, 'horizon': 14},
                format='json',
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['status'], PurchasePlan.Status.READY)

        # Молока хватает — в план попал только один товар.
        self.assertEqual(response.data['items_total'], 1)
        item = response.data['items'][0]
        self.assertEqual(item['name'], 'Пепси 1 л')
        self.assertEqual(item['suggested'], '18.000')
        self.assertEqual(response.data['total_cost'], '9000.00')

    def test_plan_knows_the_supplier_of_each_line(self):
        """Закупаются поставщиками — у строки должен быть свой."""

        self.install()
        pepsi = product()
        milk = product(productName='Молоко', barcode=111)

        fake = FakeReport([pepsi, milk], by_supplier={7: [pepsi], 8: [milk]})

        with patch('umag.client._request', new=fake):
            response = self.client.post('/api/purchases/plan/', {}, format='json')

        suppliers = {item['name']: item['supplier'] for item in response.data['items']}
        self.assertEqual(suppliers, {'Пепси 1 л': 'Поставщик 7', 'Молоко': 'Поставщик 8'})

    def test_line_goes_to_the_supplier_who_sells_it_more(self):
        """Товар берут у двоих — в плане остаётся основной поставщик."""

        self.install()
        few = product(saleQuantity=10)
        many = product(saleQuantity=50)

        fake = FakeReport([product()], by_supplier={7: [few], 8: [many]})

        with patch('umag.client._request', new=fake):
            response = self.client.post('/api/purchases/plan/', {}, format='json')

        self.assertEqual(response.data['items'][0]['supplier'], 'Поставщик 8')

    def test_plan_survives_without_suppliers(self):
        """Кабинет не отдал поставщиков — план всё равно нужен."""

        self.install()
        fake = FakeReport([product()])

        with patch('umag.client._request', new=fake):
            response = self.client.post('/api/purchases/plan/', {}, format='json')

        self.assertEqual(response.data['status'], PurchasePlan.Status.READY)
        self.assertEqual(response.data['items'][0]['supplier'], '')

    def test_plan_reports_broken_umag(self):
        self.install()

        with patch('purchases.planner.report', side_effect=planner.PlanError('UMAG недоступен')):
            response = self.client.post('/api/purchases/plan/', {}, format='json')

        self.assertEqual(response.data['status'], PurchasePlan.Status.FAILED)
        self.assertEqual(response.data['error'], 'UMAG недоступен')

    def test_plan_belongs_to_the_chosen_store(self):
        self.install()
        PurchasePlan.objects.create(user=self.user, store_id=999, status=PurchasePlan.Status.READY)

        # План есть, но от другого магазина — этот считаем заново.
        self.assertEqual(self.client.get('/api/purchases/plan/').status_code, 204)

    def test_plan_of_other_user_is_hidden(self):
        self.install()
        other = make_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
        PurchasePlan.objects.create(user=other, store_id=17795, status=PurchasePlan.Status.READY)

        self.assertEqual(self.client.get('/api/purchases/plan/').status_code, 204)
