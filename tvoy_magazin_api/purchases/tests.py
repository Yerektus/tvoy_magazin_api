from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from accounts.tests import make_user
from umag.client import UmagError
from umag.demand import rebuild_store
from umag.models import (
    UmagAccount,
    UmagProduct,
    UmagRefund,
    UmagRefundItem,
    UmagSale,
    UmagSaleItem,
    UmagSalesSync,
    UmagSoldProduct,
)
from umag.test_sales import SalesApi

from . import analytics, forecast, planner
from .models import PurchasePlan, PurchasePlanItem

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

    def test_leap_day_shifts_to_february_28(self):
        self.assertEqual(forecast.shift_years(date(2024, 2, 29), 1), date(2023, 2, 28))


class AnalyticsTurnoverTests(SimpleTestCase):
    @patch('purchases.analytics.UmagClient')
    def test_turnover_reads_report_sum_and_receipt_count(self, client_cls):
        """Выручка минус возвраты, прибыль из маржи, средний чек из числа чеков."""

        client_cls.return_value.get.side_effect = [
            {
                'sum': {
                    'saleSellingAmount': 1000,
                    'saleArrivalAmount': 700,
                    'refundSellingAmount': 100,
                    'refundArrivalAmount': 70,
                    'marginAmount': 270,
                }
            },
            {'count': 8, 'sales': []},
        ]
        account = type('Account', (), {'ready': True, 'store_id': 17795})()
        today = date(2026, 9, 17)
        totals = analytics._fetch_turnover(account, today, today)

        self.assertEqual(totals['revenue'], Decimal('900.00'))
        self.assertEqual(totals['profit'], Decimal('270.00'))
        self.assertEqual(totals['visitors'], 8)
        self.assertEqual(totals['average_check'], Decimal('112.50'))

    @patch('purchases.analytics.UmagClient')
    def test_daily_revenue_reads_report_for_each_day(self, client_cls):
        """Каждый день — свой запрос отчёта: в копии нет цен."""

        cache.clear()
        amounts = {
            analytics._range_millis(date(2026, 9, 16), date(2026, 9, 16))[0]: (1000, 100),
            analytics._range_millis(date(2026, 9, 17), date(2026, 9, 17))[0]: (400, 0),
        }

        def report(path, **params):
            selling, refund = amounts[params['fromTime']]
            return {'sum': {'saleSellingAmount': selling, 'refundSellingAmount': refund}}

        client_cls.return_value.get.side_effect = report
        account = type('Account', (), {'ready': True, 'store_id': 17795})()
        days = analytics._daily_revenue(account, date(2026, 9, 16), date(2026, 9, 17))

        self.assertEqual(days[date(2026, 9, 16)], Decimal('900.00'))
        self.assertEqual(days[date(2026, 9, 17)], Decimal('400.00'))
        self.assertEqual(client_cls.return_value.get.call_count, 2)


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

    def _sold(self, barcode, name, quantity, *, external_id=None, days_ago=0):
        """Один чек в выбранном магазине — чтобы собрать копию продаж."""

        sale = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id=external_id or barcode,
            occurred_at=timezone.now() - timedelta(days=days_ago),
        )
        UmagSaleItem.objects.create(
            sale=sale,
            position=1,
            barcode=barcode,
            name=name,
            measure='шт',
            quantity=quantity,
        )
        return sale

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
                )
                for sale in sales
            ]
        )

        rebuild_store(self.user.organization, 17795)

        with patch('umag.client._request', new=FakeReport([product()])):
            response = self.client.post(
                '/api/purchases/plan/',
                {'days': 30, 'horizon': 14},
                format='json',
            )

        item = response.data['items'][0]
        self.assertIn(item['forecast_model'], forecast.MODELS)
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

    def test_plan_reuses_supplier_from_the_last_count(self):
        """Второй расчёт не ходит в UMAG за поставщиками, если они уже известны."""

        self.install()
        pepsi = product()
        fake = FakeReport([pepsi], by_supplier={7: [pepsi]})

        with patch('umag.client._request', new=fake):
            self.client.post('/api/purchases/plan/', {}, format='json')

        again = FakeReport([pepsi])

        with patch('umag.client._request', new=again):
            response = self.client.post('/api/purchases/plan/', {}, format='json')

        self.assertEqual(response.data['items'][0]['supplier'], 'Поставщик 7')
        self.assertNotIn(planner.SUPPLIER_REPORT, again.calls)
        self.assertNotIn(planner.AGENTS, again.calls)

    def test_plan_does_not_lookup_seasonal_products_in_umag(self):
        """Прошлогодний товар берём из номенклатуры, а не по одному из кабинета."""

        self.install()
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.READY,
            history_from=timezone.now() - timedelta(days=400),
            synced_until=timezone.now(),
            synced_at=timezone.now(),
        )
        UmagProduct.objects.create(
            store_id=17795,
            barcode='999',
            name='Ёлка',
            measure='шт',
        )
        last_year = forecast.shift_years(timezone.localdate(), 1)
        sales = UmagSale.objects.bulk_create(
            [
                UmagSale(
                    organization=self.user.organization,
                    store_id=17795,
                    external_id=f'tree-{index}',
                    occurred_at=timezone.make_aware(
                        datetime.combine(last_year + timedelta(days=index), time(12, 0))
                    ),
                )
                for index in range(14)
            ]
        )
        UmagSaleItem.objects.bulk_create(
            [
                UmagSaleItem(
                    sale=sale,
                    position=1,
                    barcode='999',
                    name='Ёлка',
                    measure='шт',
                    quantity=20,
                )
                for sale in sales
            ]
        )
        rebuild_store(self.user.organization, 17795)

        fake = FakeReport([product()])

        with patch('umag.client._request', new=fake):
            response = self.client.post(
                '/api/purchases/plan/',
                {'days': 30, 'horizon': 14},
                format='json',
            )

        self.assertNotIn('nom/product/findProductByBarcode', fake.calls)
        names = {item['name'] for item in response.data['items']}
        self.assertIn('Ёлка', names)

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

    def test_stuck_products_sync_becomes_ready_when_sales_exist(self):
        """Поток умер, статус остался syncing — кнопка не должна крутиться вечно."""

        self.install()
        occurred = timezone.now() - timedelta(days=1)
        sale = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='1',
            occurred_at=occurred,
        )
        UmagSaleItem.objects.create(
            sale=sale,
            position=1,
            barcode='111',
            name='Молоко',
            measure='шт',
            quantity=2,
        )
        UmagSale.objects.filter(pk=sale.pk).update(
            updated_at=timezone.now() - timedelta(minutes=20),
        )
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.SYNCING,
        )

        response = self.client.get('/api/purchases/products/')

        self.assertEqual(response.data['status'], UmagSalesSync.Status.READY)
        self.assertEqual(response.data['items_total'], 1)
        state = UmagSalesSync.objects.get()
        self.assertEqual(state.status, UmagSalesSync.Status.READY)
        self.assertEqual(state.synced_until, occurred)

    def test_stuck_products_sync_without_sales_fails(self):
        self.install()
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.SYNCING,
        )

        response = self.client.get('/api/purchases/products/')

        self.assertEqual(response.data['status'], UmagSalesSync.Status.FAILED)
        self.assertIn('прервалась', response.data['error'])

    def test_live_products_sync_stays_syncing(self):
        self.install()
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.SYNCING,
            heartbeat_at=timezone.now(),
        )

        response = self.client.get('/api/purchases/products/')

        self.assertEqual(response.data['status'], UmagSalesSync.Status.SYNCING)

    def test_recent_sale_writes_keep_sync_alive(self):
        """Месяц чеков качается дольше трёх минут — это не мёртвый поток."""

        self.install()
        UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='1',
            occurred_at=timezone.now() - timedelta(days=1),
        )
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.SYNCING,
            heartbeat_at=timezone.now() - timedelta(minutes=4),
        )

        response = self.client.get('/api/purchases/products/')

        self.assertEqual(response.data['status'], UmagSalesSync.Status.SYNCING)

    def test_stale_products_sync_can_be_restarted(self):
        self.install()
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.SYNCING,
        )

        with patch('purchases.tasks.schedule_sales_sync') as schedule:
            response = self.client.post('/api/purchases/products/', {}, format='json')

        schedule.assert_called_once()
        self.assertEqual(response.data['status'], UmagSalesSync.Status.SYNCING)
        self.assertIsNotNone(UmagSalesSync.objects.get().heartbeat_at)

    def test_live_products_sync_is_not_restarted(self):
        self.install()
        UmagSalesSync.objects.create(
            organization=self.user.organization,
            store_id=17795,
            status=UmagSalesSync.Status.SYNCING,
            heartbeat_at=timezone.now(),
        )

        with patch('purchases.tasks.schedule_sales_sync') as schedule:
            self.client.post('/api/purchases/products/', {}, format='json')

        schedule.assert_not_called()

    def test_products_come_from_sales(self):
        """Список на вкладке — то, что продавали, а не вся номенклатура кабинета."""

        self.install()
        now = timezone.now()
        first = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='1',
            occurred_at=now - timedelta(days=2),
        )
        second = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='2',
            occurred_at=now,
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
            sale=first,
            sale_occurred_at=first.occurred_at,
        )
        UmagRefundItem.objects.create(
            refund=refund,
            position=1,
            barcode='111',
            quantity=1,
        )

        rebuild_store(self.user.organization, 17795)

        response = self.client.get('/api/purchases/products/')
        item = response.data['items'][0]

        self.assertEqual(item['barcode'], '111')
        self.assertEqual(item['name'], 'Молоко')
        self.assertEqual(item['measure'], 'шт')
        self.assertEqual(item['sold'], '2.000')
        self.assertIn('forecast_error', item)
        self.assertEqual(response.data['items_total'], 1)
        self.assertEqual(response.data['page'], 1)
        self.assertEqual(response.data['page_size'], 50)

    def test_products_are_paginated_and_sorted(self):
        """Список режется и сортируется на сервере — все 10 тысяч на клиент не едут."""

        self.install()
        now = timezone.now()

        for barcode, name, quantity, days in (
            ('111', 'Хлеб', 1, 3),
            ('222', 'Молоко', 5, 2),
            ('333', 'Айран', 3, 1),
        ):
            sale = UmagSale.objects.create(
                organization=self.user.organization,
                store_id=17795,
                external_id=barcode,
                occurred_at=now - timedelta(days=days),
            )
            UmagSaleItem.objects.create(
                sale=sale,
                position=1,
                barcode=barcode,
                name=name,
                measure='шт',
                quantity=quantity,
            )

        rebuild_store(self.user.organization, 17795)

        page = self.client.get(
            '/api/purchases/products/',
            {'page': 2, 'page_size': 2, 'sort': 'sold', 'order': 'desc'},
        )

        self.assertEqual(page.data['items_total'], 3)
        self.assertEqual(page.data['page'], 2)
        self.assertEqual(page.data['page_size'], 2)
        self.assertEqual([item['barcode'] for item in page.data['items']], ['111'])

        names = self.client.get(
            '/api/purchases/products/',
            {'sort': 'name', 'order': 'asc'},
        )
        self.assertEqual(
            [item['name'] for item in names.data['items']],
            ['Айран', 'Молоко', 'Хлеб'],
        )

        codes = self.client.get(
            '/api/purchases/products/',
            {'sort': 'barcode', 'order': 'asc'},
        )
        self.assertEqual(
            [item['barcode'] for item in codes.data['items']],
            ['111', '222', '333'],
        )

        found = self.client.get('/api/purchases/products/', {'q': 'Молоко'})
        self.assertEqual(found.data['items_total'], 1)
        self.assertEqual(found.data['items'][0]['name'], 'Молоко')

        found = self.client.get('/api/purchases/products/', {'barcode': '222'})
        self.assertEqual(found.data['items_total'], 1)
        self.assertEqual(found.data['items'][0]['name'], 'Молоко')

        recent = self.client.get(
            '/api/purchases/products/',
            {'last_from': (now - timedelta(days=1)).date().isoformat()},
        )
        self.assertEqual(recent.data['items_total'], 1)
        self.assertEqual(recent.data['items'][0]['name'], 'Айран')

        sold = self.client.get(
            '/api/purchases/products/',
            {'sold_from': '3', 'sold_to': '5'},
        )
        self.assertEqual(
            [item['name'] for item in sold.data['items']],
            ['Молоко', 'Айран'],
        )

    def test_products_filter_by_several_accuracy_levels(self):
        """Точность в фильтре можно сложить: высокая и «нет данных» сразу."""

        self.install()
        self._sold('111', 'Хлеб', 1)
        self._sold('222', 'Молоко', 1)
        self._sold('333', 'Айран', 1)
        self._sold('444', 'Сок', 1)
        rebuild_store(self.user.organization, 17795)

        today = timezone.localdate()
        UmagSoldProduct.objects.filter(barcode='111').update(
            forecast_error=Decimal('0.10'),
            forecast_on=today,
        )
        UmagSoldProduct.objects.filter(barcode='222').update(
            forecast_error=Decimal('0.40'),
            forecast_on=today,
        )
        UmagSoldProduct.objects.filter(barcode='333').update(
            forecast_error=Decimal('0.80'),
            forecast_on=today,
        )
        UmagSoldProduct.objects.filter(barcode='444').update(
            forecast_error=None,
            forecast_on=today,
        )

        high = self.client.get('/api/purchases/products/', {'accuracy': 'high'})
        self.assertEqual([item['name'] for item in high.data['items']], ['Хлеб'])

        mixed = self.client.get(
            '/api/purchases/products/',
            {'accuracy': 'high,none', 'sort': 'name', 'order': 'asc'},
        )
        self.assertEqual(
            [item['name'] for item in mixed.data['items']],
            ['Сок', 'Хлеб'],
        )

        bad = self.client.get('/api/purchases/products/', {'accuracy': 'high,unknown'})
        self.assertEqual(bad.status_code, 400)

    def test_product_forecast_error_is_cached(self):
        """Ошибку прогноза считаем один раз в день — список не гоняет модели повторно."""

        self.install()
        self._sold('111', 'Молоко', 2)
        rebuild_store(self.user.organization, 17795)

        first = self.client.get('/api/purchases/products/')
        stored = UmagSoldProduct.objects.get()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(stored.forecast_on, timezone.localdate())
        self.assertEqual(
            first.data['items'][0]['forecast_error'],
            None
            if stored.forecast_error is None
            else format(stored.forecast_error, 'f'),
        )

        with patch('purchases.products.demand_forecast.for_products') as mocked:
            second = self.client.get('/api/purchases/products/')

        mocked.assert_not_called()
        self.assertEqual(
            second.data['items'][0]['forecast_error'],
            first.data['items'][0]['forecast_error'],
        )

    def test_product_forecast_error_is_recomputed_when_sales_change(self):
        """Новые чеки сбрасывают копию — иначе точность смотрит вчерашний ряд."""

        self.install()
        self._sold('111', 'Молоко', 2)
        rebuild_store(self.user.organization, 17795)
        self.client.get('/api/purchases/products/')

        stored = UmagSoldProduct.objects.get()
        cached = stored.forecast_error
        self.assertIsNotNone(stored.forecast_on)

        self._sold('111', 'Молоко', 1, external_id='2', days_ago=1)
        rebuild_store(self.user.organization, 17795)
        stored.refresh_from_db()

        self.assertIsNone(stored.forecast_on)
        self.assertEqual(stored.forecast_error, cached)

        with patch(
            'purchases.products.demand_forecast.for_products',
            wraps=forecast.for_products,
        ) as mocked:
            self.client.get('/api/purchases/products/')

        mocked.assert_called_once()
        stored.refresh_from_db()
        self.assertEqual(stored.forecast_on, timezone.localdate())

    def test_product_forecast_error_is_recomputed_next_day(self):
        """На новый день ряд другой — вчерашнюю ошибку не показываем."""

        self.install()
        self._sold('111', 'Молоко', 2)
        rebuild_store(self.user.organization, 17795)
        self.client.get('/api/purchases/products/')

        stored = UmagSoldProduct.objects.get()
        stored.forecast_on = timezone.localdate() - timedelta(days=1)
        stored.save(update_fields=('forecast_on',))

        with patch(
            'purchases.products.demand_forecast.for_products',
            wraps=forecast.for_products,
        ) as mocked:
            self.client.get('/api/purchases/products/')

        mocked.assert_called_once()
        stored.refresh_from_db()
        self.assertEqual(stored.forecast_on, timezone.localdate())

    def test_products_skip_forecast_while_sales_sync(self):
        """Пока чеки качаются, модели не гоняем — но последнюю точность не прячем."""

        self.install()
        self._sold('111', 'Молоко', 2)
        rebuild_store(self.user.organization, 17795)
        first = self.client.get('/api/purchases/products/')
        stored = UmagSoldProduct.objects.get()
        UmagSalesSync.objects.update_or_create(
            organization=self.user.organization,
            store_id=17795,
            defaults={
                'status': UmagSalesSync.Status.SYNCING,
                'heartbeat_at': timezone.now(),
                'error': '',
            },
        )
        stored.forecast_on = None
        stored.save(update_fields=('forecast_on',))

        with patch('purchases.products.demand_forecast.for_products') as mocked:
            response = self.client.get('/api/purchases/products/')

        mocked.assert_not_called()
        self.assertEqual(response.data['status'], 'syncing')
        self.assertEqual(
            response.data['items'][0]['forecast_error'],
            first.data['items'][0]['forecast_error'],
        )

    def test_products_take_measure_from_catalog_when_sale_lost_it(self):
        """В чеке ноль — штуки, а `0 or ''` его стирал. Берём единицу из номенклатуры."""

        self.install()
        now = timezone.now()
        sale = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='1',
            occurred_at=now,
        )
        UmagSaleItem.objects.create(
            sale=sale,
            position=1,
            barcode='111',
            name='Молоко',
            measure='',
            quantity=2,
        )
        UmagProduct.objects.create(
            store_id=17795,
            barcode='111',
            name='Молоко',
            measure='0',
        )

        rebuild_store(self.user.organization, 17795)

        response = self.client.get('/api/purchases/products/')

        self.assertEqual(response.data['items'][0]['measure'], 'шт')

    def test_product_detail_returns_history_and_forecast(self):
        """Карточка товара — дневные продажи и прогноз на горизонт."""

        self.install()
        now = timezone.now()

        for offset in range(21):
            sale = UmagSale.objects.create(
                organization=self.user.organization,
                store_id=17795,
                external_id=str(offset),
                occurred_at=now - timedelta(days=21 - offset),
            )
            UmagSaleItem.objects.create(
                sale=sale,
                position=1,
                barcode='2110000003685',
                name='Яйцо каратал',
                measure='шт',
                quantity=10 + (offset % 3),
            )

        rebuild_store(self.user.organization, 17795)

        response = self.client.get(
            '/api/purchases/products/2110000003685/',
            {'horizon': 7, 'history_days': 14},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['barcode'], '2110000003685')
        self.assertEqual(response.data['name'], 'Яйцо каратал')
        self.assertEqual(response.data['horizon'], 7)
        self.assertLessEqual(len(response.data['history']), 14)
        self.assertIsNotNone(response.data['forecast'])
        self.assertEqual(len(response.data['forecast']['series']), 7)
        self.assertEqual(len(response.data['forecast']['fitted']), len(response.data['history']))
        self.assertGreater(Decimal(response.data['forecast']['quantity']), 0)
        self.assertGreater(
            max(Decimal(row['sold']) for row in response.data['history']),
            0,
        )

        chosen = self.client.get(
            '/api/purchases/products/2110000003685/',
            {'horizon': 7, 'history_days': 14, 'model': 'average'},
        )

        self.assertEqual(chosen.status_code, 200)
        self.assertEqual(chosen.data['forecast']['model'], 'average')

        sales = self.client.get(
            '/api/purchases/products/2110000003685/',
            {'history_days': 14, 'forecast': False},
        )

        self.assertEqual(sales.status_code, 200)
        self.assertIsNone(sales.data['forecast'])
        self.assertGreater(len(sales.data['history']), 0)

    def test_product_detail_missing_is_404(self):
        self.install()

        response = self.client.get('/api/purchases/products/missing/')

        self.assertEqual(response.status_code, 404)

    def test_product_detail_includes_supplier_from_plan(self):
        """Поставщика берём из последней готовой планировки этого магазина."""

        self.install()
        now = timezone.now()
        sale = UmagSale.objects.create(
            organization=self.user.organization,
            store_id=17795,
            external_id='1',
            occurred_at=now,
        )
        UmagSaleItem.objects.create(
            sale=sale,
            position=1,
            barcode='2110000003685',
            name='Яйцо каратал',
            measure='шт',
            quantity=10,
        )
        rebuild_store(self.user.organization, 17795)

        plan = PurchasePlan.objects.create(
            user=self.user,
            store_id=17795,
            status=PurchasePlan.Status.READY,
            built_at=now,
        )
        PurchasePlanItem.objects.create(
            plan=plan,
            position=1,
            barcode='2110000003685',
            name='Яйцо каратал',
            supplier='Поставщик 7',
            sold=10,
            stock=0,
            per_day=1,
            suggested=14,
        )

        response = self.client.get('/api/purchases/products/2110000003685/')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['barcode'], '2110000003685')
        self.assertEqual(response.data['supplier'], 'Поставщик 7')

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

    def test_sales_analytics_summarizes_the_period(self):
        """Сводка за окно: товары, скидки, топ, категории и сравнение с прошлым."""

        self.install()
        organization = self.user.organization

        def sell(barcode, name, quantity, days_ago, *, promo=False):
            sale = UmagSale.objects.create(
                organization=organization,
                store_id=17795,
                external_id=f'{barcode}-{days_ago}',
                occurred_at=_noon(days_ago),
            )
            UmagSaleItem.objects.create(
                sale=sale,
                position=1,
                barcode=barcode,
                name=name,
                measure='шт',
                quantity=quantity,
                on_promo=promo,
            )

        sell('111', 'Молоко', 10, 1)
        sell('111', 'Молоко', 5, 2, promo=True)
        sell('222', 'Хлеб', 2, 1)
        sell('111', 'Молоко', 20, 10)

        UmagProduct.objects.create(
            store_id=17795,
            barcode='111',
            name='Молоко',
            measure='шт',
            category='Молочные',
        )
        UmagProduct.objects.create(
            store_id=17795,
            barcode='222',
            name='Хлеб',
            measure='шт',
            category='Выпечка',
        )
        rebuild_store(organization, 17795)

        totals = {
            'revenue': Decimal('10000.00'),
            'profit': Decimal('3000.00'),
            'visitors': 40,
            'average_check': Decimal('250.00'),
        }

        with (
            patch('purchases.analytics._fetch_turnover', return_value=totals),
            patch('purchases.analytics._daily_revenue', side_effect=_fixed_daily_revenue),
        ):
            response = self.client.get('/api/purchases/analytics/', {'days': 7})
            data = response.data

            self.assertEqual(response.status_code, 200)
            self.assertTrue(data['has_sales'])
            self.assertEqual(data['days'], 7)
            self.assertEqual(data['sku_count'], 2)
            self.assertEqual(data['sold'], '17.000')
            self.assertEqual(data['promo_share'], '0.294')
            self.assertEqual(data['trend'], '-0.150')
            self.assertEqual(data['revenue'], '10000.00')
            self.assertEqual(data['profit'], '3000.00')
            self.assertEqual(data['visitors'], 40)
            self.assertEqual(data['average_check'], '250.00')
            self.assertEqual(len(data['history']), 7)
            self.assertTrue(all(row['revenue'] == '100.00' for row in data['history']))
            self.assertEqual(len(data['weekdays']), 7)
            self.assertEqual(len(data['hours']), 24)
            self.assertEqual(data['hours'][12]['sold'], '17.000')
            self.assertEqual(
                {row['name']: row['sold'] for row in data['categories']},
                {'Молочные': '15.000', 'Выпечка': '2.000'},
            )

            empty = self.client.get('/api/purchases/analytics/', {'days': 400})
            self.assertEqual(empty.status_code, 400)

            today = timezone.localdate()
            recent = self.client.get(
                '/api/purchases/analytics/',
                {
                    'start': (today - timedelta(days=1)).isoformat(),
                    'end': today.isoformat(),
                },
            )

        self.assertEqual(recent.status_code, 200)
        self.assertEqual(recent.data['days'], 2)
        self.assertEqual(len(recent.data['history']), 2)
        self.assertEqual(recent.data['sold'], '12.000')

    def test_sales_analytics_groups_hours_from_receipts(self):
        """Утренний и вечерний чек попадают в свой столбец, возврат снимает час продажи."""

        self.install()
        organization = self.user.organization
        morning = UmagSale.objects.create(
            organization=organization,
            store_id=17795,
            external_id='morning',
            occurred_at=_at(1, 8),
        )
        UmagSaleItem.objects.create(
            sale=morning,
            position=1,
            barcode='111',
            name='Молоко',
            measure='шт',
            quantity=4,
        )
        evening = UmagSale.objects.create(
            organization=organization,
            store_id=17795,
            external_id='evening',
            occurred_at=_at(1, 21),
        )
        UmagSaleItem.objects.create(
            sale=evening,
            position=1,
            barcode='111',
            name='Молоко',
            measure='шт',
            quantity=6,
        )
        refund = UmagRefund.objects.create(
            organization=organization,
            store_id=17795,
            external_id='morning-back',
            sale=morning,
            sale_occurred_at=morning.occurred_at,
            occurred_at=_at(0, 10),
        )
        UmagRefundItem.objects.create(
            refund=refund,
            position=1,
            barcode='111',
            quantity=1,
        )
        rebuild_store(organization, 17795)

        with patch(
            'purchases.analytics._fetch_turnover',
            return_value={
                'revenue': None,
                'profit': None,
                'visitors': None,
                'average_check': None,
            },
        ), patch('purchases.analytics._daily_revenue', side_effect=_fixed_daily_revenue):
            response = self.client.get('/api/purchases/analytics/', {'days': 7})

        self.assertEqual(response.status_code, 200)
        hours = {row['hour']: row['sold'] for row in response.data['hours']}
        self.assertEqual(hours[8], '3.000')
        self.assertEqual(hours[21], '6.000')
        self.assertEqual(hours[12], '0.000')

    def test_sales_analytics_is_empty_without_store(self):
        response = self.client.get('/api/purchases/analytics/')

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['has_sales'])
        self.assertEqual(response.data['sku_count'], 0)
        self.assertEqual(len(response.data['history']), 30)
        self.assertEqual(len(response.data['weekdays']), 7)
        self.assertEqual(len(response.data['hours']), 24)
        self.assertTrue(all(row['sold'] == '0.000' for row in response.data['hours']))


def _fixed_daily_revenue(account, start, end):
    """Заглушка выручки: по 100 ₸ на каждый день, без похода в UMAG."""

    days = {}
    day = start

    while day <= end:
        days[day] = Decimal('100.00')
        day += timedelta(days=1)

    return days


def _noon(days_ago: int):
    """Полдень выбранного дня в Алматы — продажа точно попадает в нужную дату."""

    return _at(days_ago, 12)


def _at(days_ago: int, hour: int):
    """Выбранный час в Алматы — чтобы столбец «по времени» не съезжал из‑за UTC."""

    from zoneinfo import ZoneInfo

    day = timezone.localdate() - timedelta(days=days_ago)
    return datetime.combine(day, time(hour, 0), tzinfo=ZoneInfo('Asia/Almaty'))

