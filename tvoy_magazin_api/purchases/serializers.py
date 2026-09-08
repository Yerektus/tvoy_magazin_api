from rest_framework import serializers

from .models import ApprovedPurchase, ApprovedPurchaseItem, PurchasePlan, PurchasePlanItem

# Дольше двух месяцев считать бессмысленно: ассортимент за это время меняется.
MAX_DAYS = 90
MAX_HORIZON = 60


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


class ProductsSnapshotSerializer(serializers.Serializer):
    """Вкладка «Товары»: выгрузка чеков и собранный по ним список."""

    status = serializers.CharField()
    synced_at = serializers.DateTimeField(allow_null=True)
    history_from = serializers.DateTimeField(allow_null=True)
    error = serializers.CharField(allow_blank=True)
    items_total = serializers.IntegerField()
    items = StoreProductSerializer(many=True)


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
