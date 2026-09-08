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


class PurchasePlanRequestSerializer(serializers.Serializer):
    """Что просят посчитать: за какой период и на сколько дней вперёд."""

    days = serializers.IntegerField(min_value=7, max_value=MAX_DAYS, required=False)
    horizon = serializers.IntegerField(min_value=1, max_value=MAX_HORIZON, required=False)

    #: Вычитать ли остаток на полке из потребности.
    use_stock = serializers.BooleanField(required=False)


class ApproveSupplierSerializer(serializers.Serializer):
    """Какого поставщика одобрить из текущего плана."""

    supplier = serializers.CharField(allow_blank=True, max_length=255)


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
