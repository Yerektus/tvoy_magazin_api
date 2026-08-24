from rest_framework import serializers

from .models import Message

#: Длиннее вопроса не бывает: это чат, а не форма для полотна текста.
MAX_QUESTION = 2000


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = ('id', 'role', 'text', 'created_at')


class AskSerializer(serializers.Serializer):
    """Вопрос человека."""

    text = serializers.CharField(max_length=MAX_QUESTION, trim_whitespace=True)
