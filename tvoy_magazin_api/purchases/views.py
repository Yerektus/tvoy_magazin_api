from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import ManagesOrganization, UsesPurchases
from extensions.models import Extension, ExtensionInstall
from umag.models import UmagAccount

from . import tasks
from .models import PurchasePlan
from .serializers import PurchasePlanRequestSerializer, PurchasePlanSerializer

# Код расширения в каталоге.
SLUG = 'planning'


class PlanningAccessView(APIView):
    """/api/purchases/access/ — подключение расширения «Планирование закупов».

    Своего входа у него нет: оно работает поверх подключённого UMAG, поэтому
    подключение — это отметка, что сотрудник им пользуется.

    Читать состояние может любой — иначе страница закупов не поймёт, показывать
    ей план или приглашение подключиться. А вот подключать и отключать
    расширения — дело владельца и администратора.
    """

    permission_classes = [IsAuthenticated, UsesPurchases]

    def get_permissions(self):
        if self.request.method in ('POST', 'DELETE'):
            return [IsAuthenticated(), UsesPurchases(), ManagesOrganization()]

        return super().get_permissions()

    def get(self, request):
        return Response(_state(request.user))

    def post(self, request):
        extension = Extension.objects.filter(slug=SLUG, is_active=True).first()

        if extension is None:
            return Response(
                {'detail': 'Расширения нет в каталоге'},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not _signed_in(request.user):
            return Response(
                {'detail': 'Сначала подключите UMAG'},
                status=status.HTTP_409_CONFLICT,
            )

        ExtensionInstall.objects.get_or_create(user=request.user, extension=extension)
        return Response(_state(request.user))

    def delete(self, request):
        ExtensionInstall.objects.filter(user=request.user, extension__slug=SLUG).delete()
        return Response(_state(request.user))


class PurchasePlanView(APIView):
    """/api/purchases/plan/ — последний план по выбранному магазину и пересчёт."""

    permission_classes = [IsAuthenticated, UsesPurchases]

    def get(self, request):
        account = _account(request.user)
        plan = (
            PurchasePlan.objects.filter(user=request.user, store_id=account.store_id).first()
            if account
            else None
        )

        if plan is None:
            # Плана ещё нет — страница предложит посчитать.
            return Response(status=status.HTTP_204_NO_CONTENT)

        return Response(PurchasePlanSerializer(plan).data)

    def post(self, request):
        if not _installed(request.user):
            return Response(
                {'detail': 'Подключите расширение «Планирование закупов»'},
                status=status.HTTP_409_CONFLICT,
            )

        account = _account(request.user)

        if account is None or not account.ready:
            return Response(
                {'detail': 'Подключите UMAG и выберите магазин'},
                status=status.HTTP_409_CONFLICT,
            )

        form = PurchasePlanRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        # Прошлый план по этому магазину больше не нужен: считаем заново.
        PurchasePlan.objects.filter(user=request.user, store_id=account.store_id).delete()

        plan = PurchasePlan.objects.create(
            user=request.user,
            store_id=account.store_id,
            store_name=account.store_name,
            # Чего не прислали — остаётся по умолчанию: 30 дней и горизонт в две недели.
            **{key: value for key, value in form.validated_data.items() if value},
        )

        tasks.schedule(plan)
        plan.refresh_from_db()

        return Response(PurchasePlanSerializer(plan).data, status=status.HTTP_201_CREATED)


def _account(user):
    return UmagAccount.objects.filter(user=user).first()


def _installed(user) -> bool:
    return ExtensionInstall.objects.filter(user=user, extension__slug=SLUG).exists()


def _signed_in(user) -> bool:
    """Вход в UMAG выполнен.

    Магазин для подключения выбирать не обязательно: он нужен, только когда
    дойдёт до расчёта плана, а переключают его в шапке когда угодно.
    """

    return _account(user) is not None


def _state(user) -> dict:
    """Состояние подключения — в том же виде, что у остальных расширений."""

    return {'connected': _installed(user), 'umag': _signed_in(user)}
