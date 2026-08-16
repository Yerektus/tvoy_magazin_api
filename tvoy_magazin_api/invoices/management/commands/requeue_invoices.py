"""Возвращает в очередь накладные, которые зависли в ожидании.

Фоновые потоки живут внутри процесса сервера: если его перезапустить во время
разбора, накладные останутся в статусе «в очереди» или «распознаётся» навсегда.

    uv run python tvoy_magazin_api/manage.py requeue_invoices
"""

from django.core.management.base import BaseCommand

from invoices import tasks
from invoices.models import Invoice


class Command(BaseCommand):
    help = 'Перезапускает разбор накладных, застрявших в статусах pending и processing'

    def add_arguments(self, parser):
        parser.add_argument(
            '--failed',
            action='store_true',
            help='заодно перезапустить те, что завершились ошибкой',
        )

    def handle(self, *args, **options):
        statuses = [Invoice.Status.PENDING, Invoice.Status.PROCESSING]
        if options['failed']:
            statuses.append(Invoice.Status.FAILED)

        stuck = Invoice.objects.filter(status__in=statuses)

        if not stuck:
            self.stdout.write('Застрявших накладных нет')
            return

        for invoice in stuck:
            invoice.status = Invoice.Status.PENDING
            invoice.error = ''
            invoice.save(update_fields=('status', 'error'))
            tasks.schedule(invoice)
            self.stdout.write(f'В очередь: #{invoice.pk}')

        self.stdout.write(self.style.SUCCESS(f'Возвращено в очередь: {len(stuck)}'))
