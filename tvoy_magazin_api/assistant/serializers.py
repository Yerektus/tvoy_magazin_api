from rest_framework import serializers

from .models import Conversation, Message

#: Длиннее вопроса не бывает: это чат, а не форма для полотна текста.
MAX_QUESTION = 2000


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ('id', 'role', 'text', 'image', 'created_at')


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

    def validate(self, data):
        if not (data.get('text') or '').strip() and not data.get('image'):
            raise serializers.ValidationError('Спросите словами или пришлите фото')

        return data
