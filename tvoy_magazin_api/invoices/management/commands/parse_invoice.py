"""Разбор накладной из файла — быстрый способ проверить ключ и промпт.

    uv run python tvoy_magazin_api/manage.py parse_invoice ~/Downloads/nakladnaya.jpeg
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from invoices.openrouter import OpenRouterError, parse_invoice
from invoices.tasks import content_type_for


class Command(BaseCommand):
    help = 'Отправляет фото накладной в OpenRouter и печатает разобранный JSON'

    def add_arguments(self, parser):
        parser.add_argument('path', help='путь к фотографии накладной')
        parser.add_argument('--model', help='переопределить модель OpenRouter')

    def handle(self, *args, **options):
        path = Path(options['path']).expanduser()
        if not path.is_file():
            raise CommandError(f'Файл не найден: {path}')

        if options['model']:
            settings.OPENROUTER_VISION_MODEL = options['model']

        # mimetypes не знает HEIC, поэтому тип определяем тем же кодом, что и сервер.
        content_type = content_type_for(path.name)
        self.stdout.write(f'Модель: {settings.OPENROUTER_VISION_MODEL}')

        try:
            parsed = parse_invoice(path.read_bytes(), content_type)
        except OpenRouterError as error:
            raise CommandError(str(error)) from error

        price = f' · ${parsed.cost:.4f}' if parsed.cost is not None else ''
        self.stdout.write(json.dumps(parsed.data, ensure_ascii=False, indent=2))
        self.stdout.write(
            self.style.SUCCESS(
                f'Позиций: {len(parsed.data.get("lines") or [])} · {parsed.model}{price}'
            )
        )
