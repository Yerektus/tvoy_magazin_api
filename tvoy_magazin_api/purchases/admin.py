from django.contrib import admin

from .models import PurchasePlan, PurchasePlanItem


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
    list_display = ('created_at', 'user', 'store_name', 'status', 'items_total', 'total_cost')
    list_filter = ('status',)
    search_fields = ('user__email', 'store_name')
    inlines = [PurchasePlanItemInline]
