from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from accounts.tests import make_user

from . import demand, sales
from .client import UmagClient, UmagError
from .models import (
    UmagAccount,
    UmagDailyDemand,
    UmagRefund,
    UmagRefundItem,
    UmagSale,
    UmagSaleItem,
    UmagSalesSync,
    UmagSoldProduct,
)


class SalesApi:
    def __init__(self, *, quantity=2, old=False, promo=False):
        now = int(
            (
                datetime(2024, 1, 15, tzinfo=UTC)
                if old
                else datetime.now(UTC)
            ).timestamp()
            * 1000
        )
        day = 86_400_000
        self.quantity = quantity
        self.promo = promo
        self.sales = [
            {'id': 10, 'time': now - 3 * day, 'amount': 1200, 'receiptNo': 'A-10'},
            {'id': 11, 'time': now - 2 * day, 'amount': 500, 'receiptNo': 'A-11'},
            {'id': 12, 'time': now - 1 * day, 'amount': 700, 'receiptNo': 'A-12'},
        ]
        self.refunds = [
            {'id': 20, 'saleId': 10, 'time': now, 'amount': 400},
        ]
        self.calls = []

    def __call__(self, method, path, params=None, payload=None, form=None, auth=''):
        params = params or {}
        self.calls.append((path, params))

        if path == sales.SALES:
            first = params['first']
            page = self.sales[first : first + params['pageSize']]
            return {'count': len(self.sales), 'sales': page}

        if path.startswith('opr/sale/get/'):
            external_id = int(path.rsplit('/', 1)[-1])
            header = next(row for row in self.sales if row['id'] == external_id)
            return {
                'sale': header,
                'saleProducts': [
                    {'barcode': 4870, 'quantity': self.quantity, 'price': 200, 'priceBefore': 220 if self.promo else 200},
                ],
                'products': [{'barcode': 4870, 'name': 'Молоко', 'measure': 'шт'}],
            }

        if path == sales.REFUNDS:
            first = params['first']
            page = self.refunds[first : first + params['pageSize']]
            return {'totalCount': len(self.refunds), 'refunds': page}

        if path.startswith('opr/refund/get/'):
            return {
                'refund': self.refunds[0],
                'products': [
                    {
                        'barcode': 4870,
                        'fullName': 'Молоко',
                        'measure': 'шт',
                        'quantity': 1,
                        'price': 200,
                    }
                ],
            }

        return {}


class SalesSyncTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.account = UmagAccount.objects.create(
            user=self.user,
            phone='7474419654',
            token='token',
            store_id=17795,
            store_name='Каратал',
        )

    def test_full_sync_pages_sales_and_saves_refunds(self):
        api = SalesApi()

        with (
            patch('umag.client._request', new=api),
            patch.object(sales, 'PAGE', 2),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            result = sales.sync(self.account, full=True)

        self.assertEqual(result.sales, 3)
        self.assertEqual(result.refunds, 1)
        self.assertEqual(UmagSale.objects.count(), 3)
        self.assertEqual(UmagSaleItem.objects.count(), 3)
        self.assertEqual(UmagRefund.objects.get().sale.external_id, '10')
        self.assertEqual(UmagRefundItem.objects.get().quantity, 1)
        self.assertEqual(UmagSoldProduct.objects.get().sold, Decimal('5.000'))
        self.assertEqual(UmagSalesSync.objects.get().status, UmagSalesSync.Status.READY)
        self.assertEqual(UmagSalesSync.objects.get().status, UmagSalesSync.Status.READY)

        pages = [params['first'] for path, params in api.calls if path == sales.SALES]
        self.assertEqual(pages, [0, 2])

    def test_repeated_sync_updates_without_duplicates(self):
        api = SalesApi()

        with (
            patch('umag.client._request', new=api),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            sales.sync(self.account, full=True)
            api.quantity = 3
            sales.sync(self.account)

        self.assertEqual(UmagSale.objects.count(), 3)
        self.assertEqual(UmagSaleItem.objects.count(), 3)
        self.assertEqual(UmagSaleItem.objects.filter(sale__external_id='10').get().quantity, 3)
        self.assertEqual(UmagSoldProduct.objects.get().sold, Decimal('8.000'))

    def test_sales_are_written_in_batches(self):
        """Пачка меньше числа чеков: хвост тоже должен попасть в базу."""

        with (
            patch('umag.client._request', new=SalesApi()),
            patch.object(sales, 'WRITE_BATCH', 2),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            sales.sync(self.account, full=True)

        self.assertEqual(UmagSale.objects.count(), 3)
        self.assertEqual(UmagSaleItem.objects.count(), 3)
        self.assertEqual(UmagRefund.objects.count(), 1)
        self.assertEqual(UmagRefund.objects.get().sale.external_id, '10')

    def test_numeric_measure_code_is_saved_as_unit(self):
        """Ноль в карточке — штуки; через `_text` он превращался в пустую строку."""

        api = SalesApi()

        def with_code(method, path, params=None, payload=None, form=None, auth=''):
            body = api(method, path, params, payload, form, auth)
            if path.startswith('opr/sale/get/'):
                body['products'][0]['measure'] = 0
            return body

        with (
            patch('umag.client._request', new=with_code),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            sales.sync(self.account, full=True)

        self.assertEqual(UmagSaleItem.objects.first().measure, 'шт')

    def test_broken_pagination_stops_instead_of_looping(self):
        api = SalesApi()

        def repeated(method, path, params=None, payload=None, form=None, auth=''):
            if path == sales.SALES:
                return {'count': 4, 'sales': api.sales[:2]}
            return api(method, path, params, payload, form, auth)

        with (
            patch('umag.client._request', new=repeated),
            patch.object(sales, 'PAGE', 2),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
            self.assertRaisesRegex(RuntimeError, 'пагинацию'),
        ):
            sales.sync(self.account, full=True)

        self.assertEqual(UmagSalesSync.objects.get().status, UmagSalesSync.Status.FAILED)

    def test_full_history_is_split_into_monthly_windows(self):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        finish = datetime(2026, 4, 15, tzinfo=UTC)
        windows = list(sales._windows(start, finish))

        self.assertGreater(len(windows), 1)
        self.assertTrue(all(right - left <= timedelta(days=31) for left, right in windows))
        self.assertEqual(windows[0][0], start)
        self.assertEqual(windows[-1][1], finish)

    def test_network_get_is_retried_after_timeout(self):
        with (
            patch(
                'umag.client._request',
                side_effect=[UmagError('UMAG не ответил вовремя'), {'ok': True}],
            ) as request,
            patch('umag.client.time.sleep') as sleep,
        ):
            result = UmagClient(self.account).get('report/list-product-report')

        self.assertEqual(result, {'ok': True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_list_with_lines_skips_sale_get(self):
        """Позиции уже в списке — отдельный get/{id} на каждый чек не нужен."""

        api = SalesApi()

        def with_lines(method, path, params=None, payload=None, form=None, auth=''):
            if path == sales.SALES:
                first = params['first']
                page = [
                    {
                        **row,
                        'saleProducts': [
                            {
                                'barcode': 4870,
                                'quantity': 2,
                                'price': 200,
                                'priceBefore': 200,
                            },
                        ],
                        'products': [{'barcode': 4870, 'name': 'Молоко', 'measure': 'шт'}],
                    }
                    for row in api.sales[first : first + params['pageSize']]
                ]
                return {'count': len(api.sales), 'sales': page}
            if path.startswith('opr/sale/get/'):
                raise AssertionError('карточка чека уже была в списке')
            return api(method, path, params, payload, form, auth)

        with (
            patch('umag.client._request', new=with_lines),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            result = sales.sync(self.account, full=True)

        self.assertEqual(result.sales, 3)
        self.assertEqual(UmagSaleItem.objects.count(), 3)
        self.assertEqual(UmagSaleItem.objects.first().name, 'Молоко')

    def test_missing_sale_list_falls_back_to_list_without_products(self):
        api = SalesApi()

        def missing_list(method, path, params=None, payload=None, form=None, auth=''):
            if path == sales.SALES:
                raise UmagError('нет такого', 404)
            if path == sales.SALES_FALLBACK:
                return api(method, sales.SALES, params, payload, form, auth)
            return api(method, path, params, payload, form, auth)

        with (
            patch('umag.client._request', new=missing_list),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            result = sales.sync(self.account, full=True)

        self.assertEqual(result.sales, 3)
        self.assertEqual(UmagSale.objects.count(), 3)

    def test_server_page_cap_does_not_drop_the_rest(self):
        """UMAG может отдать 50 при pageSize=100 — это не последняя страница."""

        api = SalesApi()

        def capped(method, path, params=None, payload=None, form=None, auth=''):
            if path == sales.SALES:
                first = params['first']
                return {'sales': api.sales[first : first + 2]}
            return api(method, path, params, payload, form, auth)

        with (
            patch('umag.client._request', new=capped),
            patch.object(sales, 'PAGE', 10),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            sales.sync(self.account, full=True)

        self.assertEqual(UmagSale.objects.count(), 3)

    def test_first_sync_reads_two_years_not_the_whole_calendar(self):
        api = SalesApi()

        with (
            patch('umag.client._request', new=api),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            sales.sync(self.account)

        from_times = [params['fromTime'] for path, params in api.calls if path == sales.SALES]
        year_2001 = int(datetime(2001, 1, 1, tzinfo=UTC).timestamp() * 1000)

        self.assertTrue(from_times)
        self.assertGreater(min(from_times), year_2001)

    def test_old_receipts_are_replaced_with_daily_demand(self):
        """История старше недели живёт в агрегате, сырые чеки выкидываем."""

        with (
            patch('umag.client._request', new=SalesApi(old=True)),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            sales.sync(self.account, full=True)

        self.assertEqual(UmagSale.objects.count(), 0)
        self.assertEqual(UmagRefund.objects.count(), 0)
        self.assertEqual(UmagSoldProduct.objects.get().sold, Decimal('5.000'))
        self.assertGreater(UmagDailyDemand.objects.count(), 0)

    def test_discounted_lines_are_folded_into_daily_demand(self):
        """Скидку помним в агрегате: сырой чек потом выкинем."""

        with (
            patch('umag.client._request', new=SalesApi(promo=True)),
            patch.object(sales, 'WINDOW', timedelta(days=20_000)),
        ):
            sales.sync(self.account, full=True)

        self.assertTrue(UmagSaleItem.objects.filter(on_promo=True).exists())
        self.assertGreater(UmagDailyDemand.objects.filter(promo_quantity__gt=0).count(), 0)

    def test_late_refund_reduces_already_folded_day_once(self):
        organization = self.user.organization
        old = timezone.now() - timedelta(days=20)
        UmagDailyDemand.objects.create(
            organization=organization,
            store_id=17795,
            barcode='4870',
            day=timezone.localtime(old).date(),
            quantity=Decimal('5'),
        )
        UmagSoldProduct.objects.create(
            organization=organization,
            store_id=17795,
            barcode='4870',
            name='Молоко',
            sold=Decimal('5'),
            last_sold=old,
        )
        refund = UmagRefund.objects.create(
            organization=organization,
            store_id=17795,
            external_id='20',
            sale_external_id='10',
            sale_occurred_at=old,
            occurred_at=timezone.now(),
        )
        UmagRefundItem.objects.create(
            refund=refund,
            position=1,
            barcode='4870',
            quantity=Decimal('2'),
        )

        window_start = timezone.now() - timedelta(days=1)
        demand.rebuild(organization, 17795, window_start, timezone.now())
        demand.rebuild(organization, 17795, window_start, timezone.now())

        self.assertEqual(UmagDailyDemand.objects.get().quantity, Decimal('3.000'))
        self.assertEqual(UmagSoldProduct.objects.get().sold, Decimal('3.000'))
        self.assertTrue(UmagRefund.objects.get().folded)

    def test_https_connection_is_reused(self):
        """Выгрузка чеков не должна открывать TLS на каждый GET."""

        from . import client as umag_client

        class FakeResponse:
            status = 200
            will_close = False

            def read(self):
                return b'{"ok": true}'

        class FakeConn:
            instances = []

            def __init__(self, host, port=None, timeout=None, **kwargs):
                FakeConn.instances.append(self)
                self.calls = 0

            def request(self, method, url, body=None, headers=None):
                self.calls += 1

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

        umag_client._drop_connection()
        FakeConn.instances = []

        try:
            with patch('umag.client.http.client.HTTPSConnection', FakeConn):
                UmagClient(self.account).get('org/store/list')
                UmagClient(self.account).get('org/store/list')
        finally:
            umag_client._drop_connection()

        self.assertEqual(len(FakeConn.instances), 1)
        self.assertEqual(FakeConn.instances[0].calls, 2)
