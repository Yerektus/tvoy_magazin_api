from django.contrib import admin

from .models import SupplierLink, UmagAccount, UmagProduct


@admin.register(UmagAccount)
class UmagAccountAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone', 'store_name', 'connected_at', 'refreshed_at')
    search_fields = ('user__email', 'phone', 'store_name')
    # Токен видеть незачем: он равен доступу в чужой кабинет.
    exclude = ('token',)


@admin.register(UmagProduct)
class UmagProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'barcode', 'measure', 'store_id', 'updated_at')
    search_fields = ('name', 'barcode')
    list_filter = ('store_id',)


@admin.register(SupplierLink)
class SupplierLinkAdmin(admin.ModelAdmin):
    list_display = ('name', 'agent_name', 'agent_id', 'store_id', 'created_at')
    search_fields = ('name', 'agent_name')
    list_filter = ('store_id',)
