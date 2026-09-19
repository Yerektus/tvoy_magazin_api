import re

from rest_framework import serializers

from .agent import split_suggestions
from .models import Conversation, Message

#: Длиннее вопроса не бывает: это чат, а не форма для полотна текста.
MAX_QUESTION = 2000

#: Путь страницы кабинета — короткий, без хоста и без мусора.
MAX_PAGE_PATH = 200
MAX_PAGE_TITLE = 80


class PageSerializer(serializers.Serializer):
    """Откуда спросили: название в шапке и путь в кабинете."""

    title = serializers.CharField(
        max_length=MAX_PAGE_TITLE,
        trim_whitespace=True,
        allow_blank=True,
        required=False,
        default='',
    )
    path = serializers.CharField(
        max_length=MAX_PAGE_PATH,
        trim_whitespace=True,
        allow_blank=True,
        required=False,
        default='',
    )

    def validate_path(self, value):
        value = (value or '').split('?', 1)[0].split('#', 1)[0].strip()

        if not value:
            return ''

        # Только внутренние пути кабинета: чужой URL в подсказку модели
        # не пускаем, даже если клиент его прислал.
        if not re.fullmatch(r'/[a-z0-9_/-]*', value):
            return ''

        return value


class MessageSerializer(serializers.ModelSerializer):
    """Реплика для приложения: у ответа аналитика вопросы вынесены в кнопки."""

    suggestions = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ('id', 'role', 'text', 'image', 'created_at', 'suggestions')

    def get_suggestions(self, message):
        if message.role != Message.Role.ASSISTANT:
            return []

        return split_suggestions(message.text)[1]

    def to_representation(self, instance):
        data = super().to_representation(instance)

        if instance.role == Message.Role.ASSISTANT:
            data['text'] = split_suggestions(instance.text)[0]

        return data


class ConversationSerializer(serializers.ModelSerializer):
    """Переписка в истории: чем была и когда в ней говорили в последний раз.

    Без реплик: история — это список, и тянуть в него все разговоры целиком
    значит грузить полмегабайта ради двух строк на экране.
    """

    class Meta:
        model = Conversation
        fields = ('id', 'title', 'created_at', 'updated_at')


class AskSerializer(serializers.Serializer):
    """Вопрос человека: текст, при желании фото и просьба подумать."""

    # Пустой текст допустим, когда прислали фото: «что это?» видно и так.
    text = serializers.CharField(
        max_length=MAX_QUESTION,
        trim_whitespace=True,
        allow_blank=True,
        required=False,
        default='',
    )
    image = serializers.ImageField(required=False, allow_null=True)

    #: Думать дольше. Стоит дороже и ждать заметно дольше, поэтому включает
    #: это человек сам, а не мы за него.
    think = serializers.BooleanField(required=False, default=False)

    #: В какую переписку. Пусто — в ту, где говорили последней.
    chat = serializers.IntegerField(required=False, allow_null=True)

    #: Начать новую вместо продолжения. Пустых переписок так не заводится: она
    #: появляется вместе с первым вопросом, а не по нажатию «начать заново».
    fresh = serializers.BooleanField(required=False, default=False)

    #: Страница кабинета, с которой спросили. В переписку не пишется — только
    #: подсказка аналитику, какие данные смотреть в первую очередь.
    page = PageSerializer(required=False, allow_null=True)

    def validate(self, data):
        if not (data.get('text') or '').strip() and not data.get('image'):
            raise serializers.ValidationError('Спросите словами или пришлите фото')

        page = data.get('page')

        # Пустую страницу обнуляем здесь: вложенный сериализатор не может
        # вернуть None из validate — DRF это запрещает.
        if page is not None and not (page.get('title') or page.get('path')):
            data['page'] = None

        return data
