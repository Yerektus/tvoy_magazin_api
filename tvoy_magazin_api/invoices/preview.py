"""JPEG-превью для форматов, которые браузер показать не умеет.

Айфон снимает в HEIC: модель такой файл читает, а Chrome и Firefox — нет, и
в просмотрщике вместо накладной пустота. Полноценный декодер тянуть незачем —
конвертируем системной утилитой, какая найдётся.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Длинная сторона превью: мелкий текст накладной должен остаться читаемым.
PREVIEW_SIZE = 2200
TIMEOUT = 30

NEEDS_PREVIEW = ('.heic', '.heif')


def _sips(source: Path, target: Path) -> list[str]:
    return ['sips', '-s', 'format', 'jpeg', '-Z', str(PREVIEW_SIZE), str(source), '--out', str(target)]


def _heif_convert(source: Path, target: Path) -> list[str]:
    return ['heif-convert', '-q', '85', str(source), str(target)]


def _magick(source: Path, target: Path) -> list[str]:
    return ['magick', str(source), '-resize', f'{PREVIEW_SIZE}x{PREVIEW_SIZE}>', str(target)]


# sips есть на macOS, остальные два — то, что обычно стоит на Linux.
CONVERTERS = (('sips', _sips), ('heif-convert', _heif_convert), ('magick', _magick))


def needed_for(name: str) -> bool:
    return name.lower().endswith(NEEDS_PREVIEW)


def to_jpeg(source: Path) -> bytes | None:
    """Возвращает JPEG или None, если конвертировать нечем."""

    command = next(((name, build) for name, build in CONVERTERS if shutil.which(name)), None)
    if command is None:
        logger.info('Нет утилиты для конвертации %s — превью не будет', source.name)
        return None

    name, build = command

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / 'preview.jpg'

        try:
            result = subprocess.run(
                build(source, target),
                capture_output=True,
                timeout=TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning('%s не смог обработать %s: %s', name, source.name, error)
            return None

        if result.returncode != 0 or not target.exists():
            logger.warning('%s вернул %s на %s', name, result.returncode, source.name)
            return None

        return target.read_bytes()
