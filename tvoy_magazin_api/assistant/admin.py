from django.contrib import admin
from django.db.models import Count, Sum

from .models import Conversation, Message


class MessageInline(admin.StackedInline):
    model = Message
    extra = 0
    can_delete = False
    readonly_fields = (
        'role',
        'text',
        'image',
        'file',
        'file_name',
        'cost',
        'created_at',
    )
    fields = (
        'role',
        'text',
        'image',
        'file',
        'file_name',
        'cost',
        'created_at',
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = (
        '__str__',
        'user',
        'messages_count',
        'spent',
        'created_at',
        'updated_at',
    )
    list_filter = ('user__organization',)
    search_fields = ('title', 'user__email', 'user__name', 'messages__text')
    date_hierarchy = 'updated_at'
    readonly_fields = ('user', 'created_at', 'updated_at')
    inlines = [MessageInline]

    def has_add_permission(self, request):
        # Переписку заводит сам чат, вместе с первым вопросом.
        return False

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related('user')
            .annotate(
                messages_count=Count('messages'),
                spent=Sum('messages__cost'),
            )
        )

    @admin.display(description='реплик', ordering='messages_count')
    def messages_count(self, chat):
        return chat.messages_count

    @admin.display(description='потрачено, $', ordering='spent')
    def spent(self, chat):
        return chat.spent


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    """Отдельный список — чтобы найти вопрос или ответ без открытия чата."""

    list_display = ('preview', 'role', 'conversation', 'cost', 'created_at')
    list_filter = ('role',)
    search_fields = (
        'text',
        'conversation__title',
        'conversation__user__email',
        'conversation__user__name',
    )
    date_hierarchy = 'created_at'
    readonly_fields = (
        'conversation',
        'role',
        'text',
        'image',
        'file',
        'file_name',
        'cost',
        'created_at',
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).select_related(
            'conversation',
            'conversation__user',
        )

    @admin.display(description='текст')
    def preview(self, message):
        text = ' '.join(message.text.split())

        if not text:
            return 'Фото' if message.image else '—'

        return text if len(text) <= 80 else f'{text[:79].rstrip()}…'
