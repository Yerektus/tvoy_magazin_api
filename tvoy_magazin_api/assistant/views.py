from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from invoices.openrouter import OpenRouterError

from . import agent
from .models import Conversation, Message
from .serializers import AskSerializer, MessageSerializer

#: Сколько прошлых реплик уходит в модель. Дальше разговор всё равно про другое,
#: а контекст стоит денег — пусть и небольших.
HISTORY = 20


class ChatView(APIView):
    """/api/assistant/chat/ — переписка с аналитиком.

    GET отдаёт историю, POST задаёт вопрос и возвращает ответ, DELETE начинает
    разговор заново.

    Аналитик видит только данные организации того, кто спрашивает: отбор стоит
    в самих ручках (`tools`), а не приходит из ответа модели.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        chat = Conversation.objects.filter(user=request.user).first()
        messages = chat.messages.all() if chat else []

        return Response({'messages': MessageSerializer(messages, many=True).data})

    def post(self, request):
        form = AskSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        chat, _ = Conversation.objects.get_or_create(user=request.user)
        question = Message.objects.create(
            conversation=chat,
            role=Message.Role.USER,
            text=form.validated_data['text'],
        )

        # Последние реплики берём с конца через обратный порядок: срез с
        # отрицательным индексом queryset не умеет.
        recent = reversed(list(chat.messages.order_by('-created_at', '-id')[:HISTORY]))
        history = [{'role': message.role, 'content': message.text} for message in recent]

        try:
            text, cost = agent.reply(request.user, history)
        except OpenRouterError as error:
            # Вопрос оставляем в переписке: человек его задал, и терять его
            # из-за чужой недоступности незачем — спросит ещё раз тем же.
            return Response(
                {'detail': str(error)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        answer = Message.objects.create(
            conversation=chat,
            role=Message.Role.ASSISTANT,
            text=text,
            cost=cost,
        )

        return Response(
            {'messages': MessageSerializer([question, answer], many=True).data},
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request):
        Conversation.objects.filter(user=request.user).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
