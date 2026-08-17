"""JPEG-превью для форматов, которые браузер показать не умеет.

Айфон снимает в HEIC. Safari такой файл рисует, а Chrome и Firefox — нет, и в
просмотрщике вместо накладной пустота. Этот же JPEG уходит в модель: часть
моделей на HEIC отвечает «это не картинка».

Конвертируем прямо в процессе, через Pillow с плагином pillow-heif. Раньше
звали внешнюю утилиту — `sips`, `heif-convert` или `magick`, — и на маке это
работало, а в контейнере хостинга не оказалось ни одной: превью тихо не
появлялось, и всё ломалось разом. Колесо pillow-heif несёт libheif с собой,
поэтому от содержимого образа мы больше не зависим.
"""

import io
import logging
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Длинная сторона превью: мелкий текст накладной должен остаться читаемым.
# Оригинал с телефона вчетверо тяжелее, а платим мы и за токены изображения.
PREVIEW_SIZE = 2200
QUALITY = 85

NEEDS_PREVIEW = ('.heic', '.heif')

# Учит Pillow открывать HEIC — иначе он такой файл просто не узнает.
pillow_heif.register_heif_opener()


def needed_for(name: str) -> bool:
    return name.lower().endswith(NEEDS_PREVIEW)


def to_jpeg(source: Path) -> bytes | None:
    """Возвращает JPEG или None, если картинку открыть не удалось."""

    try:
        with Image.open(source) as image:
            # Телефон пишет поворот в EXIF, а не в самих пикселях: без этого
            # накладная открывается лёжа на боку.
            fixed = ImageOps.exif_transpose(image) or image
            fixed.thumbnail((PREVIEW_SIZE, PREVIEW_SIZE))

            buffer = io.BytesIO()
            fixed.convert('RGB').save(buffer, 'JPEG', quality=QUALITY, optimize=True)
    except (OSError, UnidentifiedImageError, ValueError) as error:
        # Битый файл — не повод ронять разбор: накладная откроется без снимка.
        logger.warning('Не удалось сделать превью для %s: %s', source.name, error)
        return None

    return buffer.getvalue()
