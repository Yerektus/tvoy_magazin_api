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
