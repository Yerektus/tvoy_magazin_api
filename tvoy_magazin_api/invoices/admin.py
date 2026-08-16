from django.contrib import admin

from .models import Invoice, InvoiceLine


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'supplier', 'issued_at', 'total', 'status', 'created_at', 'deleted_at')
    list_filter = ('status',)
    search_fields = ('number', 'supplier', 'supplier_bin')
    inlines = [InvoiceLineInline]

    def get_queryset(self, request):
        # В админке видны и удалённые — ради них мягкое удаление и заводилось.
        return Invoice.all_objects.all()
