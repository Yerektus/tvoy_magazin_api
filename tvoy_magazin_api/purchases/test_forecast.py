from datetime import date, timedelta
from decimal import Decimal
from math import pi, sin

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from accounts.tests import make_user
from umag.demand import rebuild_store
from umag.models import UmagRefund, UmagRefundItem, UmagSale, UmagSaleItem

from . import forecast


class ForecastModelTests(SimpleTestCase):
    def test_short_history_uses_average(self):
        result = forecast.predict([2, 1, 3, 2, 2, 1, 3], 7)

        self.assertEqual(result.model, 'average')
        self.assertEqual(result.observations, 7)
        self.assertGreater(result.quantity, 0)

    def test_intermittent_history_allows_croston(self):
        values = [8 if index % 12 == 0 else 0 for index in range(120)]

        self.assertIn('croston_sba', forecast._eligible(values))
        result = forecast.predict(values, 14)
        self.assertIn(
            result.model,
            {'average', 'weighted_average', 'holt', 'croston_sba', 'holt_winters_weekly'},
        )
        self.assertGreaterEqual(result.quantity, 0)

    def test_weekly_pattern_selects_weekly_model(self):
        values = ([12, 2, 2, 2, 2, 2, 2] * 20) + [12, 2, 2, 2, 2, 2, 2]
        result = forecast.predict(values, 14)

        self.assertEqual(result.model, 'holt_winters_weekly')
        self.assertGreater(result.quantity, 0)

    def test_full_year_history_can_select_annual_seasonality(self):
        values = [
            20 + 10 * sin(2 * pi * index / 364) + (4 if index % 7 == 0 else 0)
            for index in range(364 * 3)
        ]
        result = forecast.predict(values, 30)

        self.assertEqual(result.model, 'seasonal_naive_year')
        self.assertGreater(result.quantity, 0)

    def test_holiday_history_increases_upcoming_demand(self):
        first = date(2022, 1, 1)
        last = date(2024, 12, 31)
        dates = [
            first + timedelta(days=offset)
            for offset in range((last - first).days + 1)
        ]
        calendar = {
            date(year, 1, 1): 'Новый год'
            for year in (2022, 2023, 2024, 2025)
        }
        values = [20 if day.month == 1 and day.day == 1 else 10 for day in dates]
        result = forecast.predict(
            values,
            1,
            dates=dates,
            future_dates=[date(2025, 1, 1)],
            holiday_calendar=calendar,
        )

        self.assertGreater(result.holiday_factor, 1)
        self.assertGreater(result.quantity, 10)

    def test_forecast_error_adds_bounded_safety_stock(self):
        values = [1 + ((index * index + index * 3) % 11) for index in range(84)]
        result = forecast.predict(values, 14)

        self.assertGreater(result.safety_stock, 0)
        self.assertLessEqual(result.safety_stock, result.quantity / 2)

    def test_promo_spike_is_clipped_before_forecast(self):
        dates = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(28)]
        values = [10.0] * 27 + [40.0]
        promo = [0.0] * 27 + [40.0]
        clipped = forecast.predict(values, 7, dates=dates, promo_values=promo)
        raw = forecast.predict(values, 7, dates=dates)

        self.assertLess(clipped.quantity, raw.quantity)

    def test_standing_discount_is_kept_as_normal_demand(self):
        """Цена всегда ниже ценника — это не акция, ряд не трогаем."""

        dates = [date(2024, 1, 1) + timedelta(days=offset) for offset in range(28)]
        values = [10.0] * 28
        promo = [10.0] * 28
        standing = forecast.predict(values, 7, dates=dates, promo_values=promo)
        raw = forecast.predict(values, 7, dates=dates)

        self.assertEqual(standing.quantity, raw.quantity)


class ForecastHistoryTests(TestCase):
    def test_refund_reduces_demand_on_original_sale_day(self):
        user = make_user()
        sold_at = timezone.now() - timedelta(days=3)
        sale = UmagSale.objects.create(
            organization=user.organization,
            store_id=17795,
            external_id='sale-1',
            occurred_at=sold_at,
        )
        UmagSaleItem.objects.create(
            sale=sale,
            position=1,
            barcode='4870',
            quantity=Decimal('3'),
        )
        refund = UmagRefund.objects.create(
            organization=user.organization,
            store_id=17795,
            external_id='refund-1',
            sale_external_id='sale-1',
            sale=sale,
            occurred_at=timezone.now() - timedelta(days=1),
        )
        UmagRefundItem.objects.create(
            refund=refund,
            position=1,
            barcode='4870',
            quantity=Decimal('1'),
        )

        rebuild_store(user.organization, 17795)

        history = forecast._daily_quantities(user.organization, 17795)
        sale_day = timezone.localtime(sold_at).date()

        self.assertEqual(history['4870'][sale_day], 2)
