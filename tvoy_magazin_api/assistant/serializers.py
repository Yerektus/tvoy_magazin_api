from rest_framework import serializers

from .models import Message

#: Длиннее вопроса не бывает: это чат, а не форма для полотна текста.
MAX_QUESTION = 2000


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ('id', 'role', 'text', 'image', 'created_at')


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

    def validate(self, data):
        if not (data.get('text') or '').strip() and not data.get('image'):
            raise serializers.ValidationError('Спросите словами или пришлите фото')

        return data
