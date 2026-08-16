from django.conf import settings
from django.db import models


class Extension(models.Model):
    """Расширение в каталоге: витрина сервиса, а не его настройки.

    Как подключаться и куда ходить по сети, знает код на фронте — он находит
    свою реализацию по `slug`. Здесь лежит только то, что читает человек.
    """

    slug = models.SlugField(
        'код',
        unique=True,
        help_text='По нему фронт находит свою реализацию: umag',
    )
    name = models.CharField('название', max_length=64)
    summary = models.CharField('коротко', max_length=255, help_text='Строка под названием')
    description = models.TextField('описание', help_text='Абзацы разделяются пустой строкой')
    logo = models.CharField(
        'логотип',
        max_length=255,
        blank=True,
        help_text='Путь во фронте (/logos/umag.svg) или полный адрес картинки',
    )

    # Надстройки работают не сами по себе: планированию закупов нужен UMAG,
    # без него ему неоткуда взять продажи и остатки.
    requires = models.ManyToManyField(
        'self',
        verbose_name='обязательные расширения',
        symmetrical=False,
        blank=True,
        related_name='required_by',
    )

    is_active = models.BooleanField('показывать в каталоге', default=True)
    position = models.PositiveSmallIntegerField('порядок', default=0)

    created_at = models.DateTimeField('создано', auto_now_add=True)
    updated_at = models.DateTimeField('изменено', auto_now=True)

    class Meta:
        verbose_name = 'расширение'
        verbose_name_plural = 'расширения'
        ordering = ('position', 'name')

    def __str__(self):
        return self.name


class ExtensionInstall(models.Model):
    """Расширение, подключённое сотрудником.

    Так подключаются расширения без собственного входа: у UMAG состояние — это
    его токен, а у надстроек над ним вроде планирования закупов подключение и
    есть вот эта запись.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь',
        on_delete=models.CASCADE,
        related_name='extension_installs',
    )
    extension = models.ForeignKey(
        'Extension',
        verbose_name='расширение',
        on_delete=models.CASCADE,
        related_name='installs',
    )
    created_at = models.DateTimeField('подключено', auto_now_add=True)

    class Meta:
        verbose_name = 'подключённое расширение'
        verbose_name_plural = 'подключённые расширения'
        constraints = [
            models.UniqueConstraint(fields=('user', 'extension'), name='unique_extension_install'),
        ]

    def __str__(self):
        return f'{self.extension} — {self.user}'


class ExtensionFeature(models.Model):
    """Строка списка «что даёт подключение»."""

    extension = models.ForeignKey(
        Extension,
        verbose_name='расширение',
        on_delete=models.CASCADE,
        related_name='features',
    )
    text = models.CharField('что даёт', max_length=255)
    position = models.PositiveSmallIntegerField('порядок', default=0)

    class Meta:
        verbose_name = 'возможность расширения'
        verbose_name_plural = 'возможности расширения'
        ordering = ('position', 'id')

    def __str__(self):
        return self.text
