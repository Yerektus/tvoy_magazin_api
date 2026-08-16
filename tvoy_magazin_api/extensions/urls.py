from django.urls import path

from .views import ExtensionDetailView, ExtensionListView

urlpatterns = [
    path('', ExtensionListView.as_view(), name='extension-list'),
    path('<slug:slug>/', ExtensionDetailView.as_view(), name='extension-detail'),
]
