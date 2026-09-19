from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

from .forecast import MODELS
from .models import ApprovedPurchase, ApprovedPurchaseItem, PurchasePlan, PurchasePlanItem

# Дольше двух месяцев считать бессмысленно: ассортимент за это время меняется.
MAX_DAYS = 90
MAX_HORIZON = 60
ACCURACY_LEVELS = ('high', 'medium', 'low', 'none')


class PurchasePlanItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = PurchasePlanItem
        fields = (
            'position',
            'barcode',
            'name',
            'measure',
            'supplier',
            'sold',
            'stock',
            'per_day',
            'cover_days',
            'forecast_model',
            'forecast_quantity',
            'forecast_per_day',
            'safety_stock',
            'holiday_factor',
            'forecast_error',
            'is_perishable',
            'shelf_life_days',
            'purchase_horizon',
            'perishability_source',
            'suggested',
            'price',
            'cost',
        )


class PurchasePlanSerializer(serializers.ModelSerializer):
    items = PurchasePlanItemSerializer(many=True, read_only=True)

    class Meta:
        model = PurchasePlan
        fields = (
            'id',
            'name',
            'status',
            'error',
            'store_id',
            'store_name',
            'days',
            'horizon',
            'use_stock',
            'items_total',
            'total_cost',
            'created_at',
            'built_at',
            'items',
        )


class PurchasePlanListSerializer(serializers.ModelSerializer):
    """Строка таблицы планировок: позиции не тащим, их тысячи."""

    class Meta:
        model = PurchasePlan
        fields = (
            'id',
            'name',
            'status',
            'error',
            'store_id',
            'store_name',
            'days',
            'horizon',
            'use_stock',
            'items_total',
            'total_cost',
            'created_at',
            'built_at',
        )


class StoreProductSerializer(serializers.Serializer):
    """Товар из продаж: то, что уходило с кассы, а не вся номенклатура кабинета."""

    barcode = serializers.CharField()
    name = serializers.CharField()
    measure = serializers.CharField(allow_blank=True)
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)
    last_sold = serializers.DateTimeField(allow_null=True)
    forecast_error = serializers.DecimalField(
        max_digits=8,
        decimal_places=3,
        allow_null=True,
    )


class ProductsQuerySerializer(serializers.Serializer):
    """Страница списка товаров: поиск, сортировка и номер страницы."""

    q = serializers.CharField(required=False, allow_blank=True, default='')
    barcode = serializers.CharField(required=False, allow_blank=True, default='')
    page = serializers.IntegerField(min_value=1, required=False, default=1)
    page_size = serializers.IntegerField(
        min_value=1,
        max_value=100,
        required=False,
        default=50,
    )
    sort = serializers.ChoiceField(
        choices=('name', 'barcode', 'sold', 'last', 'accuracy'),
        required=False,
        default='sold',
    )
    order = serializers.ChoiceField(
        choices=('asc', 'desc'),
        required=False,
        default='desc',
    )
    last_from = serializers.DateField(required=False, allow_null=True, default=None)
    last_to = serializers.DateField(required=False, allow_null=True, default=None)
    sold_from = serializers.DecimalField(
        max_digits=14,
        decimal_places=3,
        min_value=0,
        required=False,
        allow_null=True,
        default=None,
    )
    sold_to = serializers.DecimalField(
        max_digits=14,
        decimal_places=3,
        min_value=0,
        required=False,
        allow_null=True,
        default=None,
    )
    accuracy = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_accuracy(self, value: str) -> str:
        parts = [part.strip() for part in value.split(',') if part.strip()]
        unknown = [part for part in parts if part not in ACCURACY_LEVELS]
        if unknown:
            raise serializers.ValidationError('Выберите точность прогноза')

        return ','.join(dict.fromkeys(parts))


class ProductsSnapshotSerializer(serializers.Serializer):
    """Вкладка «Товары»: выгрузка чеков и одна страница списка."""

    status = serializers.CharField()
    synced_at = serializers.DateTimeField(allow_null=True)
    history_from = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_blank=True)
    items_total = serializers.IntegerField()
    page = serializers.IntegerField()
    page_size = serializers.IntegerField()
    items = StoreProductSerializer(many=True)


class DailySoldSerializer(serializers.Serializer):
    """Один день на графике: дата и сколько ушло с полки."""

    date = serializers.DateField()
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)


class ProductForecastSerializer(serializers.Serializer):
    """Прогноз спроса: модель, итог на горизонт и дневной ряд вперёд."""

    model = serializers.CharField()
    quantity = serializers.DecimalField(max_digits=14, decimal_places=3)
    per_day = serializers.DecimalField(max_digits=14, decimal_places=3)
    safety_stock = serializers.DecimalField(max_digits=14, decimal_places=3)
    holiday_factor = serializers.DecimalField(max_digits=8, decimal_places=3)
    error = serializers.DecimalField(max_digits=14, decimal_places=3)
    observations = serializers.IntegerField()
    series = DailySoldSerializer(many=True)


class StoreProductDetailSerializer(serializers.Serializer):
    """Карточка товара: кто это, сколько продали и куда движется спрос."""

    barcode = serializers.CharField()
    name = serializers.CharField()
    measure = serializers.CharField(allow_blank=True)
    supplier = serializers.CharField(allow_blank=True)
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)
    last_sold = serializers.DateTimeField(allow_null=True)
    horizon = serializers.IntegerField()
    history_days = serializers.IntegerField()
    history = DailySoldSerializer(many=True)
    forecast = ProductForecastSerializer(allow_null=True)


class AnalyticsQuerySerializer(serializers.Serializer):
    """Период сводки: даты `start`/`end` или длина окна `days` от сегодня."""

    start = serializers.DateField(required=False)
    end = serializers.DateField(required=False)
    days = serializers.IntegerField(
        min_value=1,
        max_value=MAX_DAYS,
        required=False,
        default=30,
    )

    def validate(self, attrs):
        start = attrs.get('start')
        end = attrs.get('end')
        today = timezone.localdate()

        if start is None and end is None:
            days = attrs.get('days') or 30
            attrs['start'] = today - timedelta(days=days - 1)
            attrs['end'] = today
            attrs['days'] = days
            return attrs

        if start is None or end is None:
            raise serializers.ValidationError('Укажите начало и конец периода.')

        if start > end:
            start, end = end, start

        if end > today:
            raise serializers.ValidationError('Конец периода ещё не наступил.')

        span = (end - start).days + 1
        if span > MAX_DAYS:
            raise serializers.ValidationError(f'Период не длиннее {MAX_DAYS} дней.')

        attrs['start'] = start
        attrs['end'] = end
        attrs['days'] = span
        return attrs


class AnalyticsDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)
    revenue = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)


class AnalyticsWeekdaySerializer(serializers.Serializer):
    weekday = serializers.IntegerField()
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)


class AnalyticsHourSerializer(serializers.Serializer):
    hour = serializers.IntegerField()
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)


class AnalyticsCategorySerializer(serializers.Serializer):
    name = serializers.CharField()
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)
    sku_count = serializers.IntegerField()


class SalesAnalyticsSerializer(serializers.Serializer):
    """Сводка продаж магазина: итоги периода, графики и категории."""

    status = serializers.CharField()
    synced_at = serializers.DateTimeField(allow_null=True)
    history_from = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_blank=True)
    has_sales = serializers.BooleanField()
    days = serializers.IntegerField()
    start = serializers.DateField()
    end = serializers.DateField()
    sold = serializers.DecimalField(max_digits=14, decimal_places=3)
    sku_count = serializers.IntegerField()
    active_days = serializers.IntegerField()
    promo_share = serializers.DecimalField(max_digits=8, decimal_places=3, allow_null=True)
    trend = serializers.DecimalField(max_digits=8, decimal_places=3, allow_null=True)
    revenue = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    profit = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    visitors = serializers.IntegerField(allow_null=True)
    average_check = serializers.DecimalField(max_digits=16, decimal_places=2, allow_null=True)
    history = AnalyticsDaySerializer(many=True)
    weekdays = AnalyticsWeekdaySerializer(many=True)
    hours = AnalyticsHourSerializer(many=True)
    categories = AnalyticsCategorySerializer(many=True)


class ProductDetailQuerySerializer(serializers.Serializer):
    """Параметры карточки: горизонт прогноза и длина истории на графике."""

    horizon = serializers.IntegerField(
        min_value=1,
        max_value=MAX_HORIZON,
        required=False,
        default=14,
    )
    history_days = serializers.IntegerField(
        min_value=7,
        max_value=MAX_DAYS,
        required=False,
        default=60,
    )
    model = serializers.ChoiceField(
        choices=['', *MODELS],
        required=False,
        allow_blank=True,
        default='',
    )
    forecast = serializers.BooleanField(required=False, default=True)


class PurchasePlanRequestSerializer(serializers.Serializer):
    """Что просят посчитать: как назвать, за какой период и на сколько дней вперёд."""

    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    days = serializers.IntegerField(min_value=7, max_value=MAX_DAYS, required=False)
    horizon = serializers.IntegerField(min_value=1, max_value=MAX_HORIZON, required=False)

    #: Вычитать ли остаток на полке из потребности.
    use_stock = serializers.BooleanField(required=False)


class ApproveSupplierSerializer(serializers.Serializer):
    """Какого поставщика одобрить — и из какого плана, если их несколько."""

    supplier = serializers.CharField(allow_blank=True, max_length=255)
    plan = serializers.IntegerField(required=False, min_value=1)


class ApprovedPurchaseItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ApprovedPurchaseItem
        fields = (
            'position',
            'barcode',
            'name',
            'measure',
            'sold',
            'stock',
            'per_day',
            'cover_days',
            'forecast_model',
            'forecast_quantity',
            'forecast_per_day',
            'safety_stock',
            'holiday_factor',
            'forecast_error',
            'is_perishable',
            'shelf_life_days',
            'purchase_horizon',
            'perishability_source',
            'suggested',
            'price',
            'cost',
        )


class ApprovedPurchaseSerializer(serializers.ModelSerializer):
    items = ApprovedPurchaseItemSerializer(many=True, read_only=True)

    class Meta:
        model = ApprovedPurchase
        fields = (
            'id',
            'store_id',
            'store_name',
            'supplier',
            'items_total',
            'total_cost',
            'approved_at',
            'items',
        )
