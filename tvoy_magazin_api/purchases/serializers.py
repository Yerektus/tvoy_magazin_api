from rest_framework import serializers

from .models import PurchasePlan, PurchasePlanItem

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
