from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase

from accounts.tests import make_user

from . import sales
from .client import UmagClient, UmagError
from .models import (
    UmagAccount,
    UmagRefund,
    UmagRefundItem,
    UmagSale,
    UmagSaleItem,
    UmagSalesSync,
)


class SalesApi:
    def __init__(self):
        self.sales = [
            {'id': 10, 'time': 1_757_318_400_000, 'amount': 1200, 'receiptNo': 'A-10'},
            {'id': 11, 'time': 1_757_404_800_000, 'amount': 500, 'receiptNo': 'A-11'},
            {'id': 12, 'time': 1_757_491_200_000, 'amount': 700, 'receiptNo': 'A-12'},
        ]
        self.refunds = [
            {'id': 20, 'saleId': 10, 'time': 1_757_577_600_000, 'amount': 400},
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
                    {'barcode': 4870, 'quantity': 2, 'price': 200, 'priceBefore': 220},
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
            api.sales[0]['amount'] = 1300
            sales.sync(self.account)

        self.assertEqual(UmagSale.objects.count(), 3)
        self.assertEqual(UmagSaleItem.objects.count(), 3)
        self.assertEqual(UmagSale.objects.get(external_id='10').amount, 1300)

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
