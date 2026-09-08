from django.conf import settings
from django.db import models


class UmagAccount(models.Model):
    """Доступ в UMAG — свой у каждого сотрудника, как и в самом кабинете.

    Пароль не храним: он нужен один раз, чтобы обменять его на токен сессии.
    Токен живёт до первого отказа и обновляется через refresh-token.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь',
        on_delete=models.CASCADE,
        related_name='umag_account',
    )
    phone = models.CharField('телефон в UMAG', max_length=32)
    token = models.CharField('токен сессии', max_length=255)

    # Магазинов у компании обычно несколько, а приёмка создаётся в одном.
    store_id = models.PositiveIntegerField('магазин', null=True, blank=True)
    store_name = models.CharField('название магазина', max_length=255, blank=True)

    # Список магазинов, каким его отдал кабинет при входе. Лежит здесь, а не
    # спрашивается заново: по нему фронт считает порядковый номер магазина для
    # ссылки на приёмку, и ради этого ходить в UMAG на каждое чтение состояния
    # нельзя — кабинет отвечает не быстро, и страница вставала бы на его время.
    stores = models.JSONField('магазины', default=list, blank=True)

    connected_at = models.DateTimeField('подключено', auto_now_add=True)
    refreshed_at = models.DateTimeField('токен обновлён', auto_now=True)

    class Meta:
        verbose_name = 'доступ в UMAG'
        verbose_name_plural = 'доступы в UMAG'

    def __str__(self):
        return f'{self.phone} → {self.store_name or self.store_id or "магазин не выбран"}'

    @property
    def ready(self) -> bool:
        """Можно ли отправлять: без выбранного магазина приёмку класть некуда."""

        return bool(self.token and self.store_id)


class UmagProduct(models.Model):
    """Своя копия номенклатуры магазина — по ней ищем товар для строки накладной.

    В API UMAG нет метода «отдай всё»: есть только поиск по подстроке, а он
    молчит, когда в накладной написано чуть иначе, чем на карточке («Ассорти
    ЧИЗ» против «Сыр Ассорти»). Поэтому раз в сутки выгружаем товарный отчёт —
    он отдаёт название, штрихкод и единицу по всему магазину, — и ищем у себя,
    нечётко и без сети.

    Номера товара в отчёте нет, но он и не нужен: как только штрихкод попал в
    строку, карточка находится по нему, вместе с id, ценой и остатком.
    """

    store_id = models.PositiveIntegerField('магазин')
    barcode = models.CharField('штрихкод', max_length=64)
    name = models.CharField('товар', max_length=255)
    measure = models.CharField('единица', max_length=32, blank=True)
    category = models.CharField('категория', max_length=255, blank=True)
    subcategory = models.CharField('подкатегория', max_length=255, blank=True)
    # Ручной срок важнее автоматического правила. Пусто — определяем по
    # категории и названию; 0 — явно считать товар нескоропортящимся.
    shelf_life_days = models.PositiveSmallIntegerField(
        'срок годности, дней',
        null=True,
        blank=True,
        help_text='Пусто — определить автоматически; 0 — не ограничивать закуп',
    )
    # Название, приведённое к виду для сравнения: считать его на каждый поиск
    # по шести тысячам строк — впустую.
    search_name = models.CharField('название для поиска', max_length=255, blank=True)
    updated_at = models.DateTimeField('обновлено', auto_now=True)

    class Meta:
        verbose_name = 'товар в UMAG'
        verbose_name_plural = 'номенклатура UMAG'
        constraints = [
            models.UniqueConstraint(fields=('store_id', 'barcode'), name='unique_store_product'),
        ]
        indexes = [models.Index(fields=('store_id',))]

    def __str__(self):
        return f'{self.name} ({self.barcode})'


class SupplierLink(models.Model):
    """Какой контрагент UMAG стоит за поставщиком из накладной.

    Само не определяется: БИН у контрагентов в кабинете не заполнен ни у кого,
    а один и тот же поставщик заведён по нескольку раз с разным написанием.
    Человек выбирает один раз на поставщика, дальше берём отсюда.
    """

    store_id = models.PositiveIntegerField('магазин')
    name = models.CharField('поставщик из накладной', max_length=255)
    agent_id = models.PositiveIntegerField('контрагент в UMAG')
    agent_name = models.CharField('название контрагента', max_length=255, blank=True)
    created_at = models.DateTimeField('создано', auto_now_add=True)

    class Meta:
        verbose_name = 'связка поставщика'
        verbose_name_plural = 'связки поставщиков'
        constraints = [
            models.UniqueConstraint(fields=('store_id', 'name'), name='unique_supplier_link'),
        ]

    def __str__(self):
        return f'{self.name} → {self.agent_name or self.agent_id}'


class UmagSalesSync(models.Model):
    """До какой точки локальная копия продаж догнала кабинет UMAG."""

    class Status(models.TextChoices):
        SYNCING = 'syncing', 'Загружается'
        READY = 'ready', 'Готово'
        FAILED = 'failed', 'Ошибка'

    organization = models.ForeignKey(
        'accounts.Organization',
        verbose_name='организация',
        on_delete=models.CASCADE,
        related_name='umag_sales_syncs',
    )
    store_id = models.PositiveIntegerField('магазин')
    status = models.CharField(
        'статус',
        max_length=16,
        choices=Status.choices,
        default=Status.SYNCING,
    )
    history_from = models.DateTimeField('история начинается', null=True, blank=True)
    synced_until = models.DateTimeField('синхронизировано по', null=True, blank=True)
    synced_at = models.DateTimeField('синхронизировано', null=True, blank=True)
    error = models.TextField('ошибка', blank=True)

    class Meta:
        verbose_name = 'синхронизация продаж UMAG'
        verbose_name_plural = 'синхронизации продаж UMAG'
        constraints = [
            models.UniqueConstraint(
                fields=('organization', 'store_id'),
                name='unique_organization_store_sales_sync',
            ),
        ]

    def __str__(self):
        return f'{self.organization} → {self.store_id}: {self.get_status_display()}'


class UmagSale(models.Model):
    """Чек продажи без персональных данных покупателя."""

    organization = models.ForeignKey(
        'accounts.Organization',
        verbose_name='организация',
        on_delete=models.CASCADE,
        related_name='umag_sales',
    )
    store_id = models.PositiveIntegerField('магазин')
    external_id = models.CharField('ID в UMAG', max_length=64)
    occurred_at = models.DateTimeField('продажа', db_index=True)
    receipt_no = models.CharField('номер чека', max_length=64, blank=True)
    pos_id = models.CharField('касса', max_length=64, blank=True)
    amount = models.DecimalField('сумма', max_digits=16, decimal_places=2, default=0)
    comment = models.TextField('комментарий', blank=True)
    is_ofd = models.BooleanField('фискализирован', null=True, blank=True)
    updated_at = models.DateTimeField('обновлено', auto_now=True)

    class Meta:
        verbose_name = 'продажа UMAG'
        verbose_name_plural = 'продажи UMAG'
        constraints = [
            models.UniqueConstraint(
                fields=('organization', 'store_id', 'external_id'),
                name='unique_organization_store_sale',
            ),
        ]
        indexes = [
            models.Index(fields=('organization', 'store_id', 'occurred_at')),
        ]

    def __str__(self):
        return f'{self.receipt_no or self.external_id} — {self.occurred_at:%d.%m.%Y %H:%M}'


class UmagSaleItem(models.Model):
    """Товарная строка чека — исходный спрос для прогноза."""

    sale = models.ForeignKey(
        UmagSale,
        verbose_name='продажа',
        on_delete=models.CASCADE,
        related_name='items',
    )
    position = models.PositiveSmallIntegerField('№')
    barcode = models.CharField('штрихкод', max_length=64, blank=True, db_index=True)
    name = models.CharField('товар', max_length=255, blank=True)
    measure = models.CharField('единица', max_length=32, blank=True)
    quantity = models.DecimalField('количество', max_digits=14, decimal_places=3)
    price = models.DecimalField('цена', max_digits=14, decimal_places=2, null=True, blank=True)
    price_before = models.DecimalField(
        'цена до скидки',
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
    )
    total = models.DecimalField('сумма', max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = 'строка продажи UMAG'
        verbose_name_plural = 'строки продаж UMAG'
        ordering = ('position',)
        constraints = [
            models.UniqueConstraint(fields=('sale', 'position'), name='unique_sale_position'),
        ]

    def __str__(self):
        return f'{self.name or self.barcode} — {self.quantity}'


class UmagRefund(models.Model):
    """Возврат по чеку; связь с продажей позволяет вернуть спрос в исходный день."""

    organization = models.ForeignKey(
        'accounts.Organization',
        verbose_name='организация',
        on_delete=models.CASCADE,
        related_name='umag_refunds',
    )
    store_id = models.PositiveIntegerField('магазин')
    external_id = models.CharField('ID в UMAG', max_length=64)
    sale_external_id = models.CharField('ID продажи в UMAG', max_length=64, blank=True)
    sale = models.ForeignKey(
        UmagSale,
        verbose_name='продажа',
        on_delete=models.SET_NULL,
        related_name='refunds',
        null=True,
        blank=True,
    )
    occurred_at = models.DateTimeField('возврат', db_index=True)
    amount = models.DecimalField('сумма', max_digits=16, decimal_places=2, default=0)
    paid_amount = models.DecimalField('выплачено', max_digits=16, decimal_places=2, default=0)
    note = models.TextField('комментарий', blank=True)
    updated_at = models.DateTimeField('обновлено', auto_now=True)

    class Meta:
        verbose_name = 'возврат UMAG'
        verbose_name_plural = 'возвраты UMAG'
        constraints = [
            models.UniqueConstraint(
                fields=('organization', 'store_id', 'external_id'),
                name='unique_organization_store_refund',
            ),
        ]
        indexes = [
            models.Index(fields=('organization', 'store_id', 'occurred_at')),
        ]

    def __str__(self):
        return f'Возврат {self.external_id} — {self.occurred_at:%d.%m.%Y %H:%M}'


class UmagRefundItem(models.Model):
    """Возвращённый товар, который вычитается из исторического спроса."""

    refund = models.ForeignKey(
        UmagRefund,
        verbose_name='возврат',
        on_delete=models.CASCADE,
        related_name='items',
    )
    position = models.PositiveSmallIntegerField('№')
    barcode = models.CharField('штрихкод', max_length=64, blank=True, db_index=True)
    name = models.CharField('товар', max_length=255, blank=True)
    measure = models.CharField('единица', max_length=32, blank=True)
    quantity = models.DecimalField('количество', max_digits=14, decimal_places=3)
    price = models.DecimalField('цена', max_digits=14, decimal_places=2, null=True, blank=True)
    total = models.DecimalField('сумма', max_digits=16, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = 'строка возврата UMAG'
        verbose_name_plural = 'строки возврата UMAG'
        ordering = ('position',)
        constraints = [
            models.UniqueConstraint(fields=('refund', 'position'), name='unique_refund_position'),
        ]

    def __str__(self):
        return f'{self.name or self.barcode} — {self.quantity}'
