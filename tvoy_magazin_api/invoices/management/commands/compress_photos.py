"""Дожимает снимки накладных, загруженных до того, как появилось сжатие.

    python manage.py compress_photos

Свежие накладные сжимаются при разборе. Старые остались с сырым файлом с
телефона: он втрое тяжелее и в HEIC, которого браузер не показывает.
Команда проходит по ним ещё раз.

Снимки, уже приведённые в порядок, пропускаются — гонять команду повторно
безопасно, качество от этого не теряется.
"""

from django.core.management.base import BaseCommand

from invoices import preview, tasks
from invoices.models import Invoice


class Command(BaseCommand):
    help = 'Сжимает снимки накладных, загруженные без обработки'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только показать, кого обработает',
        )

    def handle(self, *args, **options):
        # Удалённые тоже: их открывают на чтение, и снимок там нужен.
        pending = [
            invoice
            for invoice in Invoice.all_objects.exclude(image='').order_by('pk')
            if preview.needed_for(invoice.image.name)
        ]

        if not pending:
            self.stdout.write('Обрабатывать нечего')
            return

        self.stdout.write(f'Кандидатов: {len(pending)}')

        if options['dry_run']:
            return

        # Тот же код, что и при разборе: расходиться этим двум путям незачем.
        done = sum(tasks.prepare_photo(invoice) for invoice in pending)

        self.stdout.write(f'Сжато: {done}, уже готовы: {len(pending) - done}')
