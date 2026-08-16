from rest_framework import generics

from .models import Extension
from .serializers import ExtensionSerializer


class ExtensionListView(generics.ListAPIView):
    """GET /api/extensions/ — каталог: всё, что можно подключить."""

    serializer_class = ExtensionSerializer
    # Расширений считанные штуки — каталог отдаём одним списком.
    pagination_class = None

    def get_queryset(self):
        return Extension.objects.filter(is_active=True).prefetch_related('features', 'requires', 'required_by')


class ExtensionDetailView(generics.RetrieveAPIView):
    """GET /api/extensions/<slug>/ — страница одного расширения."""

    serializer_class = ExtensionSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Extension.objects.filter(is_active=True).prefetch_related('features', 'requires', 'required_by')
