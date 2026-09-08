from django.conf import settings
from django.db import models


class PurchasePlan(models.Model):
    """План закупа: что заканчивается и сколько этого дозаказать.

    Спрос прогнозируется по локальной копии чеков и возвратов UMAG, а товарный
    отчёт даёт актуальный остаток и цену. План остаётся в базе: полная история
    и кабинет читаются в фоне, а открывать страницу хочется сразу.
    """

    class Status(models.TextChoices):
        BUILDING = 'building', 'Считается'
        READY = 'ready', 'Готов'
        FAILED = 'failed', 'Ошибка'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь',
        on_delete=models.CASCADE,
        related_name='purchase_plans',
    )

    # Магазин, по которому считали: у сотрудника их несколько, и план у каждого свой.
    store_id = models.PositiveIntegerField('магазин', null=True, blank=True)
    store_name = models.CharField('название магазина', max_length=255, blank=True)

    days = models.PositiveSmallIntegerField('период анализа, дней', default=30)
    horizon = models.PositiveSmallIntegerField('закупаем на, дней', default=14)

    # Вычитать ли то, что уже лежит на полке. Обычно вычитаем: заказывать под
    # полный горизонт поверх остатка значит везти второй запас того же. Но перед
    # праздником или переездом склад в расчёт не берут — тогда снимают галочку.
    use_stock = models.BooleanField('учитывать остаток', default=True)

    status = models.CharField('статус', max_length=16, choices=Status.choices, default=Status.BUILDING)
    error = models.TextField('ошибка', blank=True)

    # Сколько товаров вообще просило заказа и на какую сумму — в самих строках
    # лежат только самые срочные, список на тысячу позиций никто не закупит.
    items_total = models.PositiveIntegerField('позиций требует заказа', default=0)
    total_cost = models.DecimalField('сумма закупа', max_digits=14, decimal_places=2, default=0)

    created_at = models.DateTimeField('создан', auto_now_add=True)
    built_at = models.DateTimeField('посчитан', null=True, blank=True)

    class Meta:
        verbose_name = 'план закупа'
        verbose_name_plural = 'планы закупа'
        ordering = ('-created_at',)

    def __str__(self):
        return f'План закупа от {self.created_at:%d.%m.%Y} — {self.store_name or self.store_id}'


class PurchasePlanItem(models.Model):
    """Строка плана: один товар, который пора дозаказать."""

    plan = models.ForeignKey(
        PurchasePlan,
        verbose_name='план',
        on_delete=models.CASCADE,
        related_name='items',
    )
    position = models.PositiveSmallIntegerField('№')

    barcode = models.CharField('штрихкод', max_length=64, blank=True)
    name = models.CharField('товар', max_length=255)
    measure = models.CharField('единица', max_length=32, blank=True)
    # У кого этот товар берут: закупаются поставщиками, а не построчно.
    supplier = models.CharField('поставщик', max_length=255, blank=True)

    sold = models.DecimalField('продано за период', max_digits=12, decimal_places=3)
    stock = models.DecimalField('остаток', max_digits=12, decimal_places=3)
    per_day = models.DecimalField('расход в день', max_digits=12, decimal_places=3)
    # Пусто — товар кончился совсем: делить остаток не на что.
    cover_days = models.DecimalField('хватит на, дней', max_digits=8, decimal_places=1, null=True, blank=True)
    forecast_model = models.CharField('модель прогноза', max_length=32, default='average')
    forecast_quantity = models.DecimalField(
        'прогноз на горизонт',
        max_digits=14,
        decimal_places=3,
        default=0,
    )
    forecast_per_day = models.DecimalField(
        'прогноз в день',
        max_digits=12,
        decimal_places=3,
        default=0,
    )
    safety_stock = models.DecimalField(
        'страховой запас',
        max_digits=12,
        decimal_places=3,
        default=0,
    )
    holiday_factor = models.DecimalField(
        'коэффициент праздников',
        max_digits=6,
        decimal_places=3,
        default=1,
    )
    forecast_error = models.DecimalField(
        'ошибка прогноза',
        max_digits=8,
        decimal_places=3,
        null=True,
        blank=True,
    )
    suggested = models.DecimalField('заказать', max_digits=12, decimal_places=3)

    # Средняя закупочная за период — из неё складывается сумма плана.
    price = models.DecimalField('закупочная', max_digits=12, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField('на сумму', max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = 'строка плана закупа'
        verbose_name_plural = 'строки плана закупа'
        ordering = ('position',)

    def __str__(self):
        return f'{self.name} — {self.suggested}'


class ApprovedPurchase(models.Model):
    """Одобренный закуп у одного поставщика.

    Из текущего плана берут группу поставщика целиком: заказ едет ему, а не
    поштучно. Строки копируются — пересчёт плана их уже не затрёт.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='пользователь',
        on_delete=models.CASCADE,
        related_name='approved_purchases',
    )

    store_id = models.PositiveIntegerField('магазин', null=True, blank=True)
    store_name = models.CharField('название магазина', max_length=255, blank=True)
    supplier = models.CharField('поставщик', max_length=255, blank=True)

    items_total = models.PositiveIntegerField('позиций', default=0)
    total_cost = models.DecimalField('сумма', max_digits=14, decimal_places=2, default=0)

    approved_at = models.DateTimeField('одобрен', auto_now_add=True)

    class Meta:
        verbose_name = 'одобренный закуп'
        verbose_name_plural = 'одобренные закупки'
        ordering = ('-approved_at',)

    def __str__(self):
        return f'{self.supplier or "без поставщика"} — {self.approved_at:%d.%m.%Y}'


class ApprovedPurchaseItem(models.Model):
    """Строка одобренного закупа: снимок позиции на момент одобрения."""

    purchase = models.ForeignKey(
        ApprovedPurchase,
        verbose_name='закуп',
        on_delete=models.CASCADE,
        related_name='items',
    )
    position = models.PositiveSmallIntegerField('№')

    barcode = models.CharField('штрихкод', max_length=64, blank=True)
    name = models.CharField('товар', max_length=255)
    measure = models.CharField('единица', max_length=32, blank=True)

    sold = models.DecimalField('продано за период', max_digits=12, decimal_places=3)
    stock = models.DecimalField('остаток', max_digits=12, decimal_places=3)
    per_day = models.DecimalField('расход в день', max_digits=12, decimal_places=3)
    cover_days = models.DecimalField('хватит на, дней', max_digits=8, decimal_places=1, null=True, blank=True)
    forecast_model = models.CharField('модель прогноза', max_length=32, default='average')
    forecast_quantity = models.DecimalField(
        'прогноз на горизонт',
        max_digits=14,
        decimal_places=3,
        default=0,
    )
    forecast_per_day = models.DecimalField(
        'прогноз в день',
        max_digits=12,
        decimal_places=3,
        default=0,
    )
    safety_stock = models.DecimalField(
        'страховой запас',
        max_digits=12,
        decimal_places=3,
        default=0,
    )
    holiday_factor = models.DecimalField(
        'коэффициент праздников',
        max_digits=6,
        decimal_places=3,
        default=1,
    )
    forecast_error = models.DecimalField(
        'ошибка прогноза',
        max_digits=8,
        decimal_places=3,
        null=True,
        blank=True,
    )
    suggested = models.DecimalField('заказать', max_digits=12, decimal_places=3)

    price = models.DecimalField('закупочная', max_digits=12, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField('на сумму', max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = 'строка одобренного закупа'
        verbose_name_plural = 'строки одобренного закупа'
        ordering = ('position',)

    def __str__(self):
        return f'{self.name} — {self.suggested}'
