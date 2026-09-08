from django.contrib import admin

from .models import ApprovedPurchase, ApprovedPurchaseItem, PurchasePlan, PurchasePlanItem


class PurchasePlanItemInline(admin.TabularInline):
    model = PurchasePlanItem
    extra = 0
    # План считает машина — руками его не правят, смотреть достаточно.
    can_delete = False
    readonly_fields = ('position', 'name', 'barcode', 'sold', 'stock', 'per_day', 'cover_days', 'suggested', 'price', 'cost')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(PurchasePlan)
class PurchasePlanAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at', 'user', 'store_name', 'status', 'items_total', 'total_cost')
    list_filter = ('status',)
    search_fields = ('user__email', 'store_name')
    inlines = [PurchasePlanItemInline]


class ApprovedPurchaseItemInline(admin.TabularInline):
    model = ApprovedPurchaseItem
    extra = 0
    can_delete = False
    readonly_fields = (
        'position',
        'name',
        'barcode',
        'sold',
        'stock',
        'per_day',
        'cover_days',
        'suggested',
        'price',
        'cost',
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ApprovedPurchase)
class ApprovedPurchaseAdmin(admin.ModelAdmin):
    list_display = ('approved_at', 'user', 'store_name', 'supplier', 'items_total', 'total_cost')
    search_fields = ('user__email', 'store_name', 'supplier')
    inlines = [ApprovedPurchaseItemInline]
