from django.contrib import admin

from .models import (
    SupplierLink,
    UmagAccount,
    UmagDailyDemand,
    UmagProduct,
    UmagRefund,
    UmagRefundItem,
    UmagSale,
    UmagSaleItem,
    UmagSalesSync,
    UmagSoldProduct,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Копия из UMAG — смотреть можно, править нельзя."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(UmagAccount)
class UmagAccountAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'store_name', 'connected_at', 'refreshed_at')
    search_fields = ('user__email', 'phone', 'store_name')
    # Токен видеть незачем: он равен доступу в чужой кабинет.
    exclude = ('token',)


@admin.register(UmagProduct)
class UmagProductAdmin(admin.ModelAdmin):
    list_display = (
        'name',
        'barcode',
        'category',
        'measure',
        'shelf_life_days',
        'store_id',
        'updated_at',
    )
    search_fields = ('name', 'barcode', 'category', 'subcategory')
    list_filter = ('store_id', 'category')


@admin.register(SupplierLink)
class SupplierLinkAdmin(admin.ModelAdmin):
    list_display = ('name', 'agent_name', 'agent_id', 'store_id', 'created_at')
    search_fields = ('name', 'agent_name')
    list_filter = ('store_id',)


@admin.register(UmagSalesSync)
class UmagSalesSyncAdmin(ReadOnlyAdmin):
    list_display = (
        'organization',
        'store_id',
        'status',
        'history_from',
        'synced_until',
        'synced_at',
        'heartbeat_at',
    )
    list_filter = ('status', 'store_id')
    search_fields = ('organization__name', 'error')
    readonly_fields = (
        'organization',
        'store_id',
        'status',
        'history_from',
        'synced_until',
        'synced_at',
        'heartbeat_at',
        'error',
    )


class UmagSaleItemInline(admin.TabularInline):
    model = UmagSaleItem
    extra = 0
    can_delete = False
    readonly_fields = ('position', 'barcode', 'name', 'measure', 'quantity', 'on_promo')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(UmagSale)
class UmagSaleAdmin(ReadOnlyAdmin):
    list_display = ('external_id', 'organization', 'store_id', 'occurred_at', 'updated_at')
    list_filter = ('store_id',)
    search_fields = ('external_id', 'organization__name')
    date_hierarchy = 'occurred_at'
    readonly_fields = ('organization', 'store_id', 'external_id', 'occurred_at', 'updated_at')
    inlines = [UmagSaleItemInline]


@admin.register(UmagSaleItem)
class UmagSaleItemAdmin(ReadOnlyAdmin):
    list_display = ('sale', 'position', 'barcode', 'name', 'quantity', 'on_promo')
    list_filter = ('on_promo',)
    search_fields = ('barcode', 'name', 'sale__external_id')
    readonly_fields = ('sale', 'position', 'barcode', 'name', 'measure', 'quantity', 'on_promo')


class UmagRefundItemInline(admin.TabularInline):
    model = UmagRefundItem
    extra = 0
    can_delete = False
    readonly_fields = ('position', 'barcode', 'quantity')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(UmagRefund)
class UmagRefundAdmin(ReadOnlyAdmin):
    list_display = (
        'external_id',
        'organization',
        'store_id',
        'sale',
        'occurred_at',
        'folded',
        'updated_at',
    )
    list_filter = ('store_id', 'folded')
    search_fields = ('external_id', 'sale_external_id', 'organization__name')
    date_hierarchy = 'occurred_at'
    readonly_fields = (
        'organization',
        'store_id',
        'external_id',
        'sale_external_id',
        'sale',
        'sale_occurred_at',
        'occurred_at',
        'folded',
        'updated_at',
    )
    inlines = [UmagRefundItemInline]


@admin.register(UmagRefundItem)
class UmagRefundItemAdmin(ReadOnlyAdmin):
    list_display = ('refund', 'position', 'barcode', 'quantity')
    search_fields = ('barcode', 'refund__external_id')
    readonly_fields = ('refund', 'position', 'barcode', 'quantity')


@admin.register(UmagDailyDemand)
class UmagDailyDemandAdmin(ReadOnlyAdmin):
    list_display = ('day', 'barcode', 'organization', 'store_id', 'quantity', 'promo_quantity')
    list_filter = ('store_id',)
    search_fields = ('barcode', 'organization__name')
    date_hierarchy = 'day'
    readonly_fields = ('organization', 'store_id', 'barcode', 'day', 'quantity', 'promo_quantity')


@admin.register(UmagSoldProduct)
class UmagSoldProductAdmin(ReadOnlyAdmin):
    list_display = ('name', 'barcode', 'organization', 'store_id', 'sold', 'measure', 'last_sold')
    list_filter = ('store_id',)
    search_fields = ('barcode', 'name', 'organization__name')
    readonly_fields = ('organization', 'store_id', 'barcode', 'name', 'measure', 'sold', 'last_sold')
