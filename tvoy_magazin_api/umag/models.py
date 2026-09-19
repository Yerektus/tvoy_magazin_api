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
    heartbeat_at = models.DateTimeField('последняя активность', null=True, blank=True)
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
    """Свежий чек: нужен, чтобы пересчитать последние дни, если UMAG правил продажу.

    Двухлетняя история живёт в дневных агрегатах. Сами чеки держим только на
    окно перекрытия синхронизации — иначе миллионы строк ради прогноза.
    """

    organization = models.ForeignKey(
        'accounts.Organization',
        verbose_name='организация',
        on_delete=models.CASCADE,
        related_name='umag_sales',
    )
    store_id = models.PositiveIntegerField('магазин')
    external_id = models.CharField('ID в UMAG', max_length=64)
    occurred_at = models.DateTimeField('продажа', db_index=True)
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
        return f'{self.external_id} — {self.occurred_at:%d.%m.%Y %H:%M}'


class UmagSaleItem(models.Model):
    """Строка свежего чека. Название нужно, пока товар не попал в номенклатуру."""

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
    # Цена не храним: для прогноза достаточно знать, что строка ушла со скидкой.
    on_promo = models.BooleanField('со скидкой', default=False)

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
    """Возврат по чеку; спрос снимаем с дня исходной продажи, не с дня возврата."""

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
    # Чек могли уже выкинуть из окна перекрытия — день спроса тогда берём отсюда.
    sale_occurred_at = models.DateTimeField('продажа', null=True, blank=True)
    occurred_at = models.DateTimeField('возврат', db_index=True)
    # Поздний возврат на уже свёрнутый день вычитаем один раз и больше не трогаем.
    folded = models.BooleanField('учтён в спросе', default=False)
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
    quantity = models.DecimalField('количество', max_digits=14, decimal_places=3)

    class Meta:
        verbose_name = 'строка возврата UMAG'
        verbose_name_plural = 'строки возврата UMAG'
        ordering = ('position',)
        constraints = [
            models.UniqueConstraint(fields=('refund', 'position'), name='unique_refund_position'),
        ]

    def __str__(self):
        return f'{self.barcode} — {self.quantity}'


class UmagDailyDemand(models.Model):
    """Спрос за день: продажи минус возвраты, одна строка на штрихкод."""

    organization = models.ForeignKey(
        'accounts.Organization',
        verbose_name='организация',
        on_delete=models.CASCADE,
        related_name='umag_daily_demands',
    )
    store_id = models.PositiveIntegerField('магазин')
    barcode = models.CharField('штрихкод', max_length=64)
    day = models.DateField('день')
    quantity = models.DecimalField('количество', max_digits=14, decimal_places=3)
    promo_quantity = models.DecimalField(
        'со скидкой',
        max_digits=14,
        decimal_places=3,
        default=0,
    )

    class Meta:
        verbose_name = 'дневной спрос UMAG'
        verbose_name_plural = 'дневной спрос UMAG'
        constraints = [
            models.UniqueConstraint(
                fields=('organization', 'store_id', 'barcode', 'day'),
                name='unique_organization_store_barcode_day',
            ),
        ]
        indexes = [
            models.Index(fields=('organization', 'store_id', 'day')),
            models.Index(fields=('organization', 'store_id', 'barcode')),
        ]

    def __str__(self):
        return f'{self.barcode} {self.day}: {self.quantity}'


class UmagSoldProduct(models.Model):
    """Товар, который продавался: сумма, последняя продажа и точность для списка."""

    organization = models.ForeignKey(
        'accounts.Organization',
        verbose_name='организация',
        on_delete=models.CASCADE,
        related_name='umag_sold_products',
    )
    store_id = models.PositiveIntegerField('магазин')
    barcode = models.CharField('штрихкод', max_length=64)
    name = models.CharField('товар', max_length=255, blank=True)
    measure = models.CharField('единица', max_length=32, blank=True)
    sold = models.DecimalField('продано', max_digits=14, decimal_places=3, default=0)
    last_sold = models.DateTimeField('последняя продажа', null=True, blank=True)
    # Как у планировки: список не гоняет модели на каждый GET. Пустая дата —
    # ещё не считали или спрос обновился; дата не сегодня — ряд устарел.
    # Ошибку держим и после сброса даты: пока идёт «Обновить», страница
    # показывает последнюю точность, а не пустую колонку.
    forecast_error = models.DecimalField(
        'ошибка прогноза',
        max_digits=8,
        decimal_places=3,
        null=True,
        blank=True,
    )
    forecast_on = models.DateField('прогноз на дату', null=True, blank=True)

    class Meta:
        verbose_name = 'проданный товар UMAG'
        verbose_name_plural = 'проданные товары UMAG'
        constraints = [
            models.UniqueConstraint(
                fields=('organization', 'store_id', 'barcode'),
                name='unique_organization_store_sold_product',
            ),
        ]
        indexes = [
            models.Index(fields=('organization', 'store_id')),
        ]

    def __str__(self):
        return f'{self.name or self.barcode} — {self.sold}'
