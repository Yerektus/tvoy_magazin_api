from decimal import Decimal

from django.test import SimpleTestCase

from . import perishability, planner


class PerishabilityTests(SimpleTestCase):
    def test_dairy_is_limited_automatically(self):
        limit = perishability.classify(
            name='Молоко пастеризованное 2,5%',
            category='Молочные продукты',
        )

        self.assertEqual(limit.shelf_life_days, 7)
        self.assertEqual(limit.purchase_days, 5)
        self.assertEqual(limit.source, 'auto')

    def test_long_life_milk_is_not_limited_by_name(self):
        self.assertIsNone(
            perishability.classify(
                name='Молоко ультрапастеризованное UHT',
                category='Молочные продукты',
            )
        )

    def test_manual_shelf_life_overrides_automatic_rule(self):
        limit = perishability.classify(
            name='Хлеб',
            shelf_life_days=10,
        )

        self.assertEqual(limit.shelf_life_days, 10)
        self.assertEqual(limit.purchase_days, 8)
        self.assertEqual(limit.source, 'manual')

    def test_zero_manual_value_disables_limit(self):
        self.assertIsNone(
            perishability.classify(
                name='Молоко пастеризованное',
                shelf_life_days=0,
            )
        )

    def test_plan_orders_only_for_safe_shelf_life_horizon(self):
        row = {
            'productName': 'Молоко пастеризованное',
            'barcode': 111,
            'measure': 'шт',
            'saleQuantity': 60,
            'refundQuantity': 0,
            'saleArrivalAmount': 30000,
            'stockQuantity': 0,
        }
        limit = perishability.classify(name=row['productName'])
        line = planner._line(
            row,
            days=30,
            horizon=30,
            restriction=limit,
        )

        # Две штуки в день, но молоко закупается лишь на безопасные пять дней,
        # а не на весь месячный горизонт.
        self.assertEqual(line['suggested'], Decimal('10'))
        self.assertTrue(line['is_perishable'])
        self.assertEqual(line['shelf_life_days'], 7)
        self.assertEqual(line['purchase_horizon'], 5)
