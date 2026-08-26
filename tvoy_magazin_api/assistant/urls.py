from django.urls import path

from .views import ChatDetailView, ChatListView, ChatView

urlpatterns = [
    path('chat/', ChatView.as_view(), name='assistant-chat'),
    path('chats/', ChatListView.as_view(), name='assistant-chats'),
    path('chats/<int:pk>/', ChatDetailView.as_view(), name='assistant-chat-detail'),
]
