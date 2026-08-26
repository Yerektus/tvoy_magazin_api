from django.conf import settings
from django.db import models
from django.utils import timezone


class AliveInvoiceManager(models.Manager):
    """Обычная выборка — только неудалённые накладные."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class Invoice(models.Model):
    """Загруженное фото накладной и результат его разбора."""

    class Status(models.TextChoices):
        PENDING = 'pending', 'В очереди'
        PROCESSING = 'processing', 'Распознаётся'
        DONE = 'done', 'Готово'
        CHECKED = 'checked', 'Проверено'
        FAILED = 'failed', 'Ошибка'

    # Снимок накладной: сжат, в JPEG и повёрнут так, чтобы текст стоял ровно.
    # Он один на накладную — его показывает браузер, его читает модель, он же
    # идёт в обучающую выборку (см. `for_training`).
    image = models.FileField('фото накладной', upload_to='invoices/%Y/%m')
    # Остался от времён, когда рядом держали второй файл. Заполнен только у
    # старых накладных, у которых в `image` лежит сырой HEIC; новые пишут
    # единственный снимок и это поле не трогают.
    preview = models.FileField('превью', upload_to='invoices/%Y/%m/preview', blank=True)

    # Маленькая копия первого листа — её показывает список накладных. Полный
    # снимок там весит два мегабайта, а по карточке его всё равно не читают.
    thumbnail = models.FileField('миниатюра', upload_to='invoices/%Y/%m/thumb', blank=True)
    status = models.CharField('статус', max_length=16, choices=Status.choices, default=Status.PENDING)
    error = models.TextField('ошибка разбора', blank=True)

    # Шапка накладной — то, что удалось прочитать с фото.
    supplier = models.CharField('поставщик', max_length=255, blank=True)
    supplier_bin = models.CharField('БИН поставщика', max_length=32, blank=True)
    # БИН не прочитался с бумаги, и его взяли из прошлой накладной того же
    # поставщика: человеку видно, что цифры не из документа.
    supplier_bin_auto = models.BooleanField('БИН подставлен', default=False)
    number = models.CharField('номер документа', max_length=64, blank=True)
    issued_at = models.DateField('дата документа', null=True, blank=True)
    total = models.DecimalField('итого по накладной', max_digits=12, decimal_places=2, null=True, blank=True)

    model = models.CharField('модель распознавания', max_length=128, blank=True)
    raw_response = models.JSONField('ответ модели', null=True, blank=True)
    # Сколько OpenRouter списал за разбор этого фото. Повторное распознавание
    # добавляется к прежней сумме: в поле — всё, что документ уже стоил.
    cost = models.DecimalField(
        'потрачено на разбор, $',
        max_digits=10,
        decimal_places=6,
        null=True,
        blank=True,
    )

    # Накладная принадлежит организации, а не тому, кто её сфотографировал:
    # принимает товар сменщик, а сверяет и отправляет в приёмку хозяин.
    organization = models.ForeignKey(
        'accounts.Organization',
        verbose_name='организация',
        on_delete=models.CASCADE,
        related_name='invoices',
    )
    # Кто загрузил — остаётся, но правами больше не заведует: это след в
    # истории и адрес, по которому берут учётку UMAG при отправке приёмки.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='загрузил',
        on_delete=models.CASCADE,
        related_name='invoices',
    )
    created_at = models.DateTimeField('загружено', auto_now_add=True)
    processed_at = models.DateTimeField('разобрано', null=True, blank=True)

    # Человек сверил распознанное с бумагой.
    checked_at = models.DateTimeField('проверено', null=True, blank=True)
    checked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='проверил',
        on_delete=models.SET_NULL,
        related_name='checked_invoices',
        null=True,
        blank=True,
    )
    # Номер черновика приёмки в UMAG. Стоит — значит накладная уже уехала
    # туда, и второй раз её отправлять нельзя.
    umag_supply_id = models.PositiveBigIntegerField('приёмка в UMAG', null=True, blank=True)
    umag_pushed_at = models.DateTimeField('отправлено в UMAG', null=True, blank=True)

    # Магазин, в который уйдёт приёмка. Записывается при загрузке: магазин
    # переключают в шапке когда угодно, а накладная должна уехать туда, где её
    # завели. Пусто — UMAG на тот момент не был подключён, и при отправке
    # возьмётся магазин, выбранный сейчас.
    umag_store_id = models.PositiveIntegerField('магазин в UMAG', null=True, blank=True)
    umag_store_name = models.CharField('название магазина', max_length=255, blank=True)

    # Фото плюс выверенные руками строки — готовая пара для дообучения модели.
    # Ставится в момент проверки: до неё в строках догадка модели, и учить на
    # них значит закреплять её же ошибки. Флаг отдельно от `checked_at` затем,
    # чтобы негодный снимок можно было исключить, не снимая проверку.
    for_training = models.BooleanField('годится для обучения', default=False)

    # Накладные не выкидываем: удалённая просто перестаёт показываться.
    deleted_at = models.DateTimeField('удалено', null=True, blank=True)

    objects = AliveInvoiceManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = 'накладная'
        verbose_name_plural = 'накладные'
        ordering = ('-created_at',)

    def __str__(self):
        return self.number or f'Накладная #{self.pk}'

    def mark_checked(self, user):
        """Отмечает, что человек сверил распознанные данные с бумагой.

        С этой минуты накладная годится и в обучение: снимок настоящий, а
        числа в строках выверены глазами по бумаге — эталоннее не будет.
        """

        self.status = self.Status.CHECKED
        self.checked_at = timezone.now()
        self.checked_by = user
        self.for_training = True
        self.save(update_fields=('status', 'checked_at', 'checked_by', 'for_training'))

    def soft_delete(self):
        """Помечает накладную удалённой, оставляя данные и фото в базе."""

        self.deleted_at = timezone.now()
        self.save(update_fields=('deleted_at',))


class InvoicePage(models.Model):
    """Второй и следующие листы накладной.

    Первый лист лежит в самой накладной (`Invoice.image`): он один у
    подавляющего большинства документов, и на нём шапка — поставщик, номер,
    дата. Сюда попадают только продолжения, когда позиции не поместились на
    одну страницу.

    Модели все листы уходят разом, одним документом: строки на втором листе
    продолжают нумерацию первого, а шапки у него нет вовсе.
    """

    invoice = models.ForeignKey(
        Invoice,
        verbose_name='накладная',
        on_delete=models.CASCADE,
        related_name='pages',
    )
    image = models.FileField('лист накладной', upload_to='invoices/%Y/%m')

    # Со второго: первый лист — это `Invoice.image`.
    position = models.PositiveSmallIntegerField('№ листа', default=2)

    class Meta:
        verbose_name = 'лист накладной'
        verbose_name_plural = 'листы накладной'
        ordering = ('position',)

    def __str__(self):
        return f'лист {self.position}'


class InvoiceLine(models.Model):
    """Позиция накладной — только то, что реально есть в бумаге."""

    invoice = models.ForeignKey(Invoice, verbose_name='накладная', on_delete=models.CASCADE, related_name='lines')
    position = models.PositiveSmallIntegerField('№ строки')

    name = models.CharField('название', max_length=255)
    barcode = models.CharField('штрихкод', max_length=64, blank=True)
    quantity = models.DecimalField('количество', max_digits=12, decimal_places=3, null=True, blank=True)
    unit = models.CharField('единица измерения', max_length=32, blank=True)
    price = models.DecimalField('цена по накладной', max_digits=12, decimal_places=2, null=True, blank=True)
    total = models.DecimalField('сумма', max_digits=12, decimal_places=2, null=True, blank=True)

    # Товар кабинета, которым эта строка является. По штрихкоду находится точно,
    # по названию его выбирает модель — тогда штрихкод в строку вписывает
    # человек, а до этого выбор живёт здесь и второй раз не оплачивается.
    umag_product_id = models.PositiveBigIntegerField('товар в UMAG', null=True, blank=True)
    umag_product_name = models.CharField('название в UMAG', max_length=255, blank=True)
    umag_barcode = models.CharField('штрихкод в UMAG', max_length=64, blank=True)
    umag_confidence = models.FloatField('уверенность сопоставления', null=True, blank=True)

    class Meta:
        verbose_name = 'позиция накладной'
        verbose_name_plural = 'позиции накладной'
        ordering = ('position',)
        constraints = [
            models.UniqueConstraint(fields=('invoice', 'position'), name='unique_line_position'),
        ]

    def __str__(self):
        return self.name
