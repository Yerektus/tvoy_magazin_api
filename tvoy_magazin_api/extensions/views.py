from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from accounts.permissions import ManagesOrganization

from .models import Extension
from .serializers import ExtensionSerializer

# Расширения ведут организацию, а не работают с накладными: менеджеру их не
# показываем и подключать не даём.
MANAGEMENT_ONLY = [IsAuthenticated, ManagesOrganization]


class ExtensionListView(generics.ListAPIView):
    """GET /api/extensions/ — каталог: всё, что можно подключить."""

    permission_classes = MANAGEMENT_ONLY
    serializer_class = ExtensionSerializer
    # Расширений считанные штуки — каталог отдаём одним списком.
    pagination_class = None

    def get_queryset(self):
        return Extension.objects.filter(is_active=True).prefetch_related('features', 'requires', 'required_by')


class ExtensionDetailView(generics.RetrieveAPIView):
    """GET /api/extensions/<slug>/ — страница одного расширения."""

    permission_classes = MANAGEMENT_ONLY
    serializer_class = ExtensionSerializer
    lookup_field = 'slug'

    def get_queryset(self):
        return Extension.objects.filter(is_active=True).prefetch_related('features', 'requires', 'required_by')
