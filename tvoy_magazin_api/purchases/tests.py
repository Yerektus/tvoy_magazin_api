from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.tests import make_user
from umag.client import UmagError
from umag.models import (
    UmagAccount,
    UmagProduct,
    UmagRefund,
    UmagRefundItem,
    UmagSale,
    UmagSaleItem,
    UmagSalesSync,
)
from umag.test_sales import SalesApi

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

    def __call__(self, method, path, params=None, payload=None, form=None, auth=''):
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


class ThreeMonthReport(FakeReport):
    """Повторяет ограничение UMAG: товарный отчёт не принимает больше 90 дней."""

    def __init__(self, rows):
        super().__init__(rows)
        self.report_spans = []

    def __call__(self, method, path, params=None, payload=None, form=None, auth=''):
        if path == planner.REPORT:
            params = params or {}
            span = (params['toTime'] - params['fromTime']) / 86_400_000
            self.report_spans.append(span)

            if span > 90.01:
                raise UmagError('Диапазон дат не может быть больше 3 месяцев')

        return super().__call__(method, path, params, payload, form, auth)


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

    def test_stock_can_be_left_out_of_the_count(self):
        """Перед праздником полку набивают заново, не глядя на остаток."""

        self.install()
        fake = FakeReport([product(saleQuantity=30, stockQuantity=10)])

        with patch('umag.client._request', new=fake):
            self.client.post(
                '/api/purchases/plan/',
                {'days': 30, 'horizon': 10, 'use_stock': False},
                format='json',
            )

        plan = PurchasePlan.objects.get()
        item = plan.items.get()

        self.assertFalse(plan.use_stock)
        # 30 продаж за 30 дней — это одна в день; на десять дней нужно десять,
        # и лежащие на полке десять в расчёт не идут.
        self.assertEqual(str(item.suggested), '10.000')

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

    def test_plan_never_requests_report_beyond_umag_three_month_limit(self):
        self.install()
        fake = ThreeMonthReport([product()])

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                '/api/purchases/plan/',
                {'days': 90, 'horizon': 14},
                format='json',
            )

        self.assertEqual(response.data['status'], PurchasePlan.Status.READY)
        self.assertTrue(fake.report_spans)
        self.assertLessEqual(max(fake.report_spans), 90.01)

    def test_plan_caps_perishable_product_to_safe_horizon(self):
        self.install()
        UmagProduct.objects.create(
            store_id=17795,
            barcode='111',
            name='Молоко пастеризованное',
            category='Молочные продукты',
        )
        fake = FakeReport(
            [
                product(
                    productName='Молоко пастеризованное',
                    barcode=111,
                    saleQuantity=60,
                    stockQuantity=0,
                )
            ]
        )

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                '/api/purchases/plan/',
                {'days': 30, 'horizon': 30},
                format='json',
            )

        item = response.data['items'][0]
        self.assertTrue(item['is_perishable'])
        self.assertEqual(item['shelf_life_days'], 7)
        self.assertEqual(item['purchase_horizon'], 5)
        self.assertEqual(item['suggested'], '10.000')

    def test_plan_uses_synced_transactions_for_forecast(self):
        self.install()
        now = timezone.now()
        sales = UmagSale.objects.bulk_create(
            [
                UmagSale(
                    organization=self.user.organization,
                    store_id=17795,
                    external_id=str(index),
                    occurred_at=now - timedelta(days=60 - index),
                    amount=2000,
                )
                for index in range(60)
            ]
        )
        UmagSaleItem.objects.bulk_create(
            [
                UmagSaleItem(
                    sale=sale,
                    position=1,
                    barcode='4870145005545',
                    name='Пепси 1 л',
                    measure='шт',
                    quantity=4,
                    price=500,
                    total=2000,
                )
                for sale in sales
            ]
        )

        with patch('umag.client._request', new=FakeReport([product()])):
            response = self.client.post(
                '/api/purchases/plan/',
                {'days': 30, 'horizon': 14},
                format='json',
            )

        item = response.data['items'][0]
        self.assertIn(
            item['forecast_model'],
            {'average', 'weighted_average', 'holt', 'holt_winters_weekly'},
        )
        self.assertGreater(Decimal(item['forecast_quantity']), Decimal('50'))
        self.assertGreater(Decimal(item['suggested']), Decimal('40'))
        self.assertIn('holiday_factor', item)

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

    def test_approving_a_supplier_moves_their_lines_out_of_the_plan(self):
        """Одобрили поставщика — его позиции уезжают в отдельный закуп."""

        self.install()
        pepsi = product()
        milk = product(productName='Молоко', barcode=111)

        fake = FakeReport([pepsi, milk], by_supplier={7: [pepsi], 8: [milk]})

        with patch('umag.client._request', new=fake):
            self.client.post('/api/purchases/plan/', {}, format='json')

        response = self.client.post(
            '/api/purchases/plan/approve/',
            {'supplier': 'Поставщик 7'},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['supplier'], 'Поставщик 7')
        self.assertEqual(response.data['items_total'], 1)
        self.assertEqual(response.data['items'][0]['name'], 'Пепси 1 л')

        plan = self.client.get('/api/purchases/plan/').data
        self.assertEqual(plan['items_total'], 1)
        self.assertEqual(plan['items'][0]['supplier'], 'Поставщик 8')

        approved = self.client.get('/api/purchases/approved/').data
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['supplier'], 'Поставщик 7')

    def test_cannot_approve_the_same_supplier_twice(self):
        self.install()
        fake = FakeReport([product()], by_supplier={7: [product()]})

        with patch('umag.client._request', new=fake):
            self.client.post('/api/purchases/plan/', {}, format='json')

        self.client.post('/api/purchases/plan/approve/', {'supplier': 'Поставщик 7'}, format='json')
        again = self.client.post(
            '/api/purchases/plan/approve/',
            {'supplier': 'Поставщик 7'},
            format='json',
        )

        self.assertEqual(again.status_code, 404)

    def test_approve_needs_a_ready_plan(self):
        self.install()
        response = self.client.post(
            '/api/purchases/plan/approve/',
            {'supplier': 'Поставщик 7'},
            format='json',
        )

        self.assertEqual(response.status_code, 409)

    def test_cancel_drops_a_building_plan(self):
        self.install()
        PurchasePlan.objects.create(
            user=self.user,
            store_id=17795,
            store_name='Каратал Ерентал',
            status=PurchasePlan.Status.BUILDING,
        )

        response = self.client.delete('/api/purchases/plan/')

        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get('/api/purchases/plan/').status_code, 204)
        self.assertFalse(PurchasePlan.objects.exists())

    def test_creating_a_plan_keeps_the_previous_one(self):
        """В списке несколько планировок: новую больше не ставим на место старой."""

        self.install()
        fake = FakeReport([product()])

        with patch('umag.client._request', new=fake):
            first = self.client.post(
                '/api/purchases/plan/',
                {'name': 'На неделю', 'horizon': 7},
                format='json',
            )
            second = self.client.post(
                '/api/purchases/plan/',
                {'name': 'На месяц', 'horizon': 30},
                format='json',
            )

        self.assertEqual(PurchasePlan.objects.count(), 2)
        self.assertEqual(first.data['name'], 'На неделю')
        self.assertEqual(second.data['name'], 'На месяц')

        listed = self.client.get('/api/purchases/plans/').data
        self.assertEqual([row['name'] for row in listed], ['На месяц', 'На неделю'])
        self.assertNotIn('items', listed[0])

        opened = self.client.get(f'/api/purchases/plans/{second.data["id"]}/')
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.data['name'], 'На месяц')
        self.assertEqual(opened.data['items'][0]['name'], 'Пепси 1 л')

    def test_plan_list_hides_other_store(self):
        self.install()
        PurchasePlan.objects.create(user=self.user, store_id=999, name='Чужой магазин')

        self.assertEqual(self.client.get('/api/purchases/plans/').data, [])

    def test_plan_detail_is_hidden_from_another_user(self):
        self.install()
        other = make_user(email='other@tvoymagazin.kz', password='tainy-parol-123')
        plan = PurchasePlan.objects.create(user=other, store_id=17795, name='Чужой')

        response = self.client.get(f'/api/purchases/plans/{plan.pk}/')
        self.assertEqual(response.status_code, 404)

    def test_ready_plan_can_be_deleted(self):
        self.install()
        plan = PurchasePlan.objects.create(
            user=self.user,
            store_id=17795,
            status=PurchasePlan.Status.READY,
            name='Старая',
        )

        response = self.client.delete(f'/api/purchases/plans/{plan.pk}/')

        self.assertEqual(response.status_code, 204)
        self.assertFalse(PurchasePlan.objects.filter(pk=plan.pk).exists())

    def test_approve_uses_the_given_plan(self):
        """Одобрение идёт из открытой планировки, а не из самой свежей."""

        self.install()
        pepsi = product()
        milk = product(productName='Молоко', barcode=111)
        fake = FakeReport([pepsi, milk], by_supplier={7: [pepsi], 8: [milk]})

        with patch('umag.client._request', new=fake):
            older = self.client.post('/api/purchases/plan/', {'name': 'Старая'}, format='json')
            self.client.post('/api/purchases/plan/', {'name': 'Новая'}, format='json')

        response = self.client.post(
            '/api/purchases/plan/approve/',
            {'supplier': 'Поставщик 7', 'plan': older.data['id']},
            format='json',
        )

        self.assertEqual(response.status_code, 201)
        old_plan = self.client.get(f'/api/purchases/plans/{older.data["id"]}/').data
        self.assertEqual(old_plan['items_total'], 1)
        self.assertEqual(old_plan['items'][0]['supplier'], 'Поставщик 8')

    def test_cancel_does_not_drop_a_ready_plan(self):
        self.install()
        PurchasePlan.objects.create(
            user=self.user,
            store_id=17795,
            status=PurchasePlan.Status.READY,
        )

        self.assertEqual(self.client.delete('/api/purchases/plan/').status_code, 204)
        self.assertEqual(PurchasePlan.objects.get().status, PurchasePlan.Status.READY)

    def test_cancelled_count_is_not_saved(self):
        """Пока ходили в UMAG, расчёт бросили — готовый план не должен появиться."""

        self.install()
        plan = PurchasePlan.objects.create(
            user=self.user,
            store_id=17795,
            store_name='Каратал Ерентал',
            status=PurchasePlan.Status.BUILDING,
        )

        def drop(*args, **kwargs):
            PurchasePlan.objects.filter(pk=plan.pk).delete()
            return [product()]

        with patch('purchases.planner.report', side_effect=drop):
            from . import tasks

            tasks.run(plan.pk)

        self.assertFalse(PurchasePlan.objects.filter(pk=plan.pk).exists())

    def test_products_are_empty_until_synced(self):
        self.install()

        response = self.client.get('/api/purchases/products/')

        self.assertEqual(response.data['status'], 'idle')
        self.assertEqual(response.data['items'], [])

    def test_products_come_from_sales(self):
        """Список на вкладке — то, что продавали, а не вся номенклатура кабинета."""

        self.install()
        now = timezone.now()
        first = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='1',
            occurred_at=now - timedelta(days=2),
            amount=400,
        )
        second = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='2',
            occurred_at=now,
            amount=200,
        )
        UmagSaleItem.objects.create(
            sale=first,
            position=1,
            barcode='111',
            name='Молоко',
            measure='шт',
            quantity=2,
        )
        UmagSaleItem.objects.create(
            sale=second,
            position=1,
            barcode='111',
            name='Молоко',
            measure='шт',
            quantity=1,
        )
        refund = UmagRefund.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='20',
            occurred_at=now,
            amount=200,
            sale=first,
        )
        UmagRefundItem.objects.create(
            refund=refund,
            position=1,
            barcode='111',
            name='Молоко',
            measure='шт',
            quantity=1,
        )

        response = self.client.get('/api/purchases/products/')
        item = response.data['items'][0]

        self.assertEqual(item['barcode'], '111')
        self.assertEqual(item['name'], 'Молоко')
        self.assertEqual(item['sold'], '2.000')
        self.assertEqual(response.data['items_total'], 1)

    def test_products_sync_loads_umag_sales(self):
        self.install()

        with (
            patch('umag.client._request', new=SalesApi()),
            patch('umag.sales.WINDOW', timedelta(days=20_000)),
        ):
            response = self.client.post('/api/purchases/products/', {}, format='json')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['status'], UmagSalesSync.Status.READY)
        self.assertEqual(response.data['items_total'], 1)
        self.assertEqual(response.data['items'][0]['name'], 'Молоко')
        self.assertEqual(UmagSale.objects.count(), 3)

    def test_plan_skips_sales_sync_when_products_are_ready(self):
        """После выгрузки чеков расчёт больше не ходит в UMAG за историей."""

        self.install()
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.READY,
            synced_at=timezone.now(),
        )

        with (
            patch('umag.sales.sync') as sync,
            patch('umag.client._request', new=FakeReport([product()])),
        ):
            response = self.client.post('/api/purchases/plan/', {}, format='json')

        self.assertEqual(response.data['status'], PurchasePlan.Status.READY)
        sync.assert_not_called()
