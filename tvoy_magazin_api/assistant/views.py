from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import UsesAssistant
from invoices.openrouter import OpenRouterError

from . import agent
from .models import Conversation, Message
from .serializers import AskSerializer, ConversationSerializer, MessageSerializer

#: Сколько прошлых реплик уходит в модель. Дальше разговор всё равно про другое,
#: а контекст стоит денег — пусть и небольших.
HISTORY = 20


class ChatMixin:
    """Общее для ручек переписки: чужие разговоры не видны никому.

    И не всякому: менеджеру помощник закрыт, пока доступ не выдали руками.
    """

    permission_classes = [IsAuthenticated, UsesAssistant]

    def chats(self):
        return Conversation.objects.filter(user=self.request.user)

    def body(self, chat, messages):
        """Ответ приложению: сама переписка и её реплики.

        Реплики — с запросом в контексте: без него ссылка на фото приходит без
        домена, и телефон её не откроет.
        """

        return {
            'chat': ConversationSerializer(chat).data if chat else None,
            'messages': MessageSerializer(
                messages,
                many=True,
                context={'request': self.request},
            ).data,
        }


class ChatView(ChatMixin, APIView):
    """/api/assistant/chat/ — открытая переписка.

    GET отдаёт ту, в которой говорили последней, POST задаёт вопрос.

    Аналитик видит только данные организации того, кто спрашивает: отбор стоит
    в самих ручках (`tools`), а не приходит из ответа модели.
    """

    def get(self, request):
        chat = self.chats().first()

        return Response(self.body(chat, chat.messages.all() if chat else []))

    def post(self, request):
        form = AskSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        photo = form.validated_data.get('image')
        chat = self._chat(form.validated_data)
        question = Message.objects.create(
            conversation=chat,
            role=Message.Role.USER,
            text=form.validated_data['text'],
            image=photo or '',
        )
        chat.name_after(question.text)

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

            # Пустую переписку тоже: вопрос был первым, и в истории осталась бы
            # строка без единой реплики.
            if not chat.messages.exists():
                chat.delete()
                chat = None

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

        # Переписка всплывает в истории наверх: `auto_now` считает время не по
        # репликам, а по сохранению самой переписки.
        chat.save(update_fields=['updated_at'])

        return Response(
            self.body(chat, [question, answer]),
            status=status.HTTP_201_CREATED,
        )

    def _chat(self, asked):
        """Куда писать вопрос: в названную, в новую или в последнюю.

        Новую заводим здесь, а не по кнопке «начать заново»: нажал и передумал
        спрашивать — в истории не должно остаться пустой строки.
        """

        if asked.get('chat'):
            return get_object_or_404(self.chats(), pk=asked['chat'])

        if asked['fresh']:
            return Conversation.objects.create(user=self.request.user)

        return self.chats().first() or Conversation.objects.create(user=self.request.user)


class ChatListView(ChatMixin, APIView):
    """/api/assistant/chats/ — история переписок, свежие сверху."""

    def get(self, request):
        return Response({'chats': ConversationSerializer(self.chats(), many=True).data})


class ChatDetailView(ChatMixin, APIView):
    """/api/assistant/chats/<id>/ — прошлая переписка: открыть или удалить."""

    def get(self, request, pk):
        chat = get_object_or_404(self.chats(), pk=pk)

        return Response(self.body(chat, chat.messages.all()))

    def delete(self, request, pk):
        get_object_or_404(self.chats(), pk=pk).delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
