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

        return Response({'messages': self._shown(messages)})

    def post(self, request):
        form = AskSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        photo = form.validated_data.get('image')
        chat, _ = Conversation.objects.get_or_create(user=request.user)
        question = Message.objects.create(
            conversation=chat,
            role=Message.Role.USER,
            text=form.validated_data['text'],
            image=photo or '',
        )

        # Последние реплики берём с конца через обратный порядок: срез с
        # отрицательным индексом queryset не умеет.
        recent = reversed(list(chat.messages.order_by('-created_at', '-id')[:HISTORY]))
        history = [{'role': message.role, 'content': message.text} for message in recent]

        # Фото уходит только со своим вопросом. Слать его снова в каждом
        # следующем — платить за одну и ту же картинку весь разговор.
        if photo:
            photo.seek(0)
            history[-1]['content'] = agent.with_image(
                question.text,
                photo.read(),
                getattr(photo, 'content_type', None) or 'image/jpeg',
            )

        try:
            text, cost = agent.reply(
                request.user,
                history,
                think=form.validated_data['think'],
            )
        except OpenRouterError as error:
            # Вопрос из переписки убираем. Пока он в ней оставался, разговор
            # копил немые реплики: вопрос есть, ответа нет и не будет, а
            # выглядит это как молчание аналитика. Текст не теряется — его
            # возвращает в поле само приложение.
            question.delete()

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
            {'messages': self._shown([question, answer])},
            status=status.HTTP_201_CREATED,
        )

    def _shown(self, messages):
        """Реплики для приложения. С запросом в контексте: без него ссылка на
        фото приходит без домена, и телефон её не откроет."""

        return MessageSerializer(
            messages,
            many=True,
            context={'request': self.request},
        ).data

    def delete(self, request):
        Conversation.objects.filter(user=request.user).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
