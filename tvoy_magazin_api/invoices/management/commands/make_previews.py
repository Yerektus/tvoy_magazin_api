"""Досоздаёт JPEG-превью накладным, у которых его нет.

    python manage.py make_previews

Превью делается при разборе, поэтому накладные, загруженные там, где не было
утилиты конвертации, остались без него — и в браузере вместо снимка с айфона
пустота. Команда проходит по ним ещё раз, когда утилита появилась.
"""

from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from invoices import preview
from invoices.models import Invoice


class Command(BaseCommand):
    help = 'Создаёт превью для накладных, у которых его нет'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только показать, у кого превью не хватает',
        )

    def handle(self, *args, **options):
        # Удалённые тоже: их открывают на чтение, и снимок там нужен.
        pending = [
            invoice
            for invoice in Invoice.all_objects.exclude(image='').order_by('pk')
            if not invoice.preview and preview.needed_for(invoice.image.name)
        ]

        if not pending:
            self.stdout.write('Все накладные с превью — делать нечего')
            return

        self.stdout.write(f'Без превью: {len(pending)}')

        if options['dry_run']:
            return

        done = 0

        for invoice in pending:
            try:
                source = Path(invoice.image.path)
            except (NotImplementedError, ValueError):
                # Хранилище без локальных путей — конвертировать нечего.
                continue

            if not source.exists():
                self.stderr.write(f'{invoice.pk}: файла нет — {invoice.image.name}')
                continue

            jpeg = preview.to_jpeg(source)

            if jpeg is None:
                self.stderr.write(f'{invoice.pk}: не удалось сконвертировать {source.name}')
                continue

            invoice.preview.save(f'{source.stem}.jpg', ContentFile(jpeg), save=True)
            done += 1

        self.stdout.write(f'Готово: {done} из {len(pending)}')
