from django.conf import settings
from django.db import models


class Conversation(models.Model):
    """Переписка сотрудника с аналитиком.

    Одна на человека: разговор про свой магазин — это не набор веток, а
    продолжающийся диалог. Захочет начать заново — чистит эту же.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name='сотрудник',
        on_delete=models.CASCADE,
        related_name='assistant_chat',
    )
    created_at = models.DateTimeField('начата', auto_now_add=True)

    class Meta:
        verbose_name = 'переписка с аналитиком'
        verbose_name_plural = 'переписки с аналитиком'

    def __str__(self):
        return f'чат {self.user}'


class Message(models.Model):
    """Реплика в переписке.

    Хранятся только реплики человека и ответы вслух. Вызовы инструментов и их
    результаты не сохраняем: это черновик рассуждения, он занимает место и
    ничего не значит завтра, когда цифры уже другие.
    """

    class Role(models.TextChoices):
        USER = 'user', 'Человек'
        ASSISTANT = 'assistant', 'Аналитик'

    conversation = models.ForeignKey(
        Conversation,
        verbose_name='переписка',
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField('кто', max_length=16, choices=Role.choices)
    text = models.TextField('текст', blank=True)

    # Фото к вопросу: накладная на столе, ценник, полка. Модели оно уходит
    # один раз — в том запросе, где его прикрепили; дальше в переписке лежит
    # ради человека, чтобы он видел, о чём спрашивал.
    image = models.FileField('фото', upload_to='assistant/%Y/%m', blank=True)

    # Сколько стоил ответ. У реплик человека пусто.
    cost = models.DecimalField(
        'потрачено, $',
        max_digits=10,
        decimal_places=6,
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField('время', auto_now_add=True)

    class Meta:
        verbose_name = 'реплика'
        verbose_name_plural = 'реплики'
        ordering = ('created_at', 'id')

    def __str__(self):
        return f'{self.get_role_display()}: {self.text[:50]}'
