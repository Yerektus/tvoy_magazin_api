from django.conf import settings
from django.db import models


#: Длина названия переписки в истории. Дальше строка всё равно не влезает в
#: экран телефона и обрезается многоточием уже на нём.
TITLE = 80


class Conversation(models.Model):
    """Переписка сотрудника с аналитиком.

    Их у человека много. Разговор про поставщиков и разговор про то, что
    заканчивается на полке, — это разные разговоры: смешанные в один, они
    заставляют платить за чужой контекст в каждом вопросе, а найти вчерашний
    ответ в общей ленте нельзя. Продолжается всегда та переписка, которую
    открыли; остальные ждут в истории.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name='сотрудник',
        on_delete=models.CASCADE,
        related_name='assistant_chats',
    )

    #: Чем эта переписка была — по ней её находят в истории. Берём первый
    #: вопрос: как человек спросил, так он её и вспомнит.
    title = models.CharField('о чём', max_length=TITLE, blank=True)

    created_at = models.DateTimeField('начата', auto_now_add=True)

    #: Время последней реплики. По нему история и упорядочена: сверху та, в
    #: которой говорили только что.
    updated_at = models.DateTimeField('последняя реплика', auto_now=True)

    class Meta:
        verbose_name = 'переписка с аналитиком'
        verbose_name_plural = 'переписки с аналитиком'
        ordering = ('-updated_at', '-id')

    def __str__(self):
        return self.title or f'чат {self.user}'

    def name_after(self, question):
        """Называет переписку первым вопросом. Второй раз не переименовывает:
        человек ищет её по тому, с чего начал."""

        if self.title:
            return

        text = ' '.join(question.split()) or 'Фото'
        self.title = text if len(text) <= TITLE else f'{text[: TITLE - 1].rstrip()}…'
        self.save(update_fields=['title'])


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
