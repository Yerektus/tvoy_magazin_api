from django.conf import settings
from django.db import models


class PurchasePlan(models.Model):
    """План закупа: что заканчивается и сколько этого дозаказать.

    Считается по товарному отчёту UMAG — он одним запросом отдаёт и продажи за
    период, и остаток на сейчас. План остаётся в базе: пересчёт ходит в кабинет
    и занимает секунды, а открывать страницу хочется сразу.
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
