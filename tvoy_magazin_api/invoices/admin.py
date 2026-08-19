from django.contrib import admin

from .models import Invoice, InvoiceLine


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        '__str__', 'supplier', 'issued_at', 'total', 'status', 'for_training', 'created_at', 'deleted_at',
    )
    # `for_training` в фильтре — чтобы выборку для дообучения можно было
    # собрать глазами и снять флаг с негодного снимка.
    list_filter = ('status', 'for_training')
    search_fields = ('number', 'supplier', 'supplier_bin')
    inlines = [InvoiceLineInline]

    def get_queryset(self, request):
        # В админке видны и удалённые — ради них мягкое удаление и заводилось.
        return Invoice.all_objects.all()
