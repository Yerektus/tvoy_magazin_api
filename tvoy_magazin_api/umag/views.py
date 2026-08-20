from django.conf import settings
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from extensions import access
from invoices.models import Invoice

from . import client as umag_client
from . import supply
from .client import UmagAuthError, UmagError
from .models import UmagAccount
from .serializers import UmagAccountSerializer, UmagConnectSerializer, UmagStoreSerializer

# Код UMAG в каталоге расширений: на нём стоят надстройки вроде планирования.
SLUG = 'umag'


class UmagAccountView(APIView):
    """/api/umag/account/ — подключение сотрудника к своему кабинету UMAG."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = _account(request)
        return Response(_state(account))

    def post(self, request):
        """Телефон и пароль в обмен на токен сессии."""

        form = UmagConnectSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        phone = form.validated_data['phone']

        try:
            token, users = umag_client.sign_in(
                phone,
                form.validated_data['password'],
                form.validated_data.get('user_id'),
            )

            # На номере несколько сотрудников — сначала пусть выберут, кто они.
            if users:
                return Response({**_state(None), 'phone': phone, 'users': users})

            stores = umag_client.stores(token)
        except UmagAuthError as error:
            return Response({'detail': str(error)}, status=status.HTTP_409_CONFLICT)
        except UmagError as error:
            return Response({'detail': str(error)}, status=status.HTTP_502_BAD_GATEWAY)

        account, _ = UmagAccount.objects.update_or_create(
            user=request.user,
            defaults={'phone': phone, 'token': token},
        )

        # Магазин один — выбирать нечего, ставим сразу.
        if len(stores) == 1:
            _set_store(account, stores[0])

        return Response(_state(account, stores), status=status.HTTP_200_OK)

    def patch(self, request):
        """Выбор магазина: приёмка создаётся в одном конкретном."""

        account = _account(request)
        if account is None:
            return Response({'detail': 'Сначала войдите в UMAG'}, status=status.HTTP_409_CONFLICT)

        form = UmagStoreSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        try:
            stores = umag_client.stores(account.token)
        except UmagAuthError as error:
            return Response({'detail': str(error)}, status=status.HTTP_409_CONFLICT)
        except UmagError as error:
            return Response({'detail': str(error)}, status=status.HTTP_502_BAD_GATEWAY)

        chosen = next((s for s in stores if s.get('id') == form.validated_data['store_id']), None)
        if chosen is None:
            return Response({'detail': 'Такого магазина нет'}, status=status.HTTP_400_BAD_REQUEST)

        _set_store(account, chosen)
        return Response(_state(account, stores))

    def delete(self, request):
        UmagAccount.objects.filter(user=request.user).delete()
        # Надстройки над UMAG без него не работают — снимаем и их.
        access.revoke(request.user, SLUG)

        return Response(status=status.HTTP_204_NO_CONTENT)


class UmagStoresView(APIView):
    """GET /api/umag/stores/ — магазины компании, чтобы было из чего выбрать."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = _account(request)
        if account is None:
            return Response({'detail': 'Сначала войдите в UMAG'}, status=status.HTTP_409_CONFLICT)

        try:
            stores = umag_client.stores(account.token)
        except UmagAuthError as error:
            return Response({'detail': str(error)}, status=status.HTTP_409_CONFLICT)
        except UmagError as error:
            return Response({'detail': str(error)}, status=status.HTTP_502_BAD_GATEWAY)

        return Response(_stores(stores))


class UmagSupplyView(APIView):
    """/api/umag/invoices/<pk>/ — проверка готовности и отправка черновика."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        invoice, account, refusal = _prepare(request, pk)
        if refusal:
            return refusal

        return _run(lambda: supply.preflight(invoice, account))

    def post(self, request, pk):
        invoice, account, refusal = _prepare(request, pk)
        if refusal:
            return refusal

        if invoice.status != Invoice.Status.CHECKED:
            return Response(
                {'detail': 'Сначала отметьте накладную проверенной'},
                status=status.HTTP_409_CONFLICT,
            )

        if invoice.umag_supply_id:
            return Response(
                {'detail': 'Накладная уже отправлена в UMAG', 'supply_id': invoice.umag_supply_id},
                status=status.HTTP_409_CONFLICT,
            )

        agent_id = request.data.get('agent_id') or None

        def send():
            supply_id = supply.push(invoice, account, agent_id)
            return {'supply_id': supply_id, 'url': settings.UMAG_SUPPLY_URL.format(id=supply_id)}

        return _run(send, ok=status.HTTP_201_CREATED)


def _prepare(request, pk):
    """Общее начало: чья накладная и есть ли доступ в UMAG."""

    invoice = generics.get_object_or_404(
        Invoice.objects.filter(organization=request.user.organization_id),
        pk=pk,
    )
    account = _account(request)

    if account is None or not account.ready:
        return (
            invoice,
            account,
            Response(
                {'detail': 'Подключите UMAG в настройках'},
                status=status.HTTP_409_CONFLICT,
            ),
        )

    return invoice, account, None


def _run(action, ok=status.HTTP_200_OK):
    """Ошибки UMAG — не наши пятисотки, поэтому переводим их в ответы."""

    try:
        return Response(action(), status=ok)
    except supply.NotReady as error:
        return Response({'detail': str(error)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
    except UmagAuthError as error:
        return Response({'detail': str(error)}, status=status.HTTP_409_CONFLICT)
    except UmagError as error:
        return Response({'detail': str(error)}, status=status.HTTP_502_BAD_GATEWAY)


def _account(request):
    return UmagAccount.objects.filter(user=request.user).first()


def _set_store(account, store: dict) -> None:
    account.store_id = store.get('id')
    account.store_name = (store.get('name') or '').strip()
    account.save(update_fields=('store_id', 'store_name', 'refreshed_at'))


def _state(account, stores: list | None = None) -> dict:
    if account is None:
        state = {'connected': False, 'phone': '', 'store_id': None, 'store_name': ''}
    else:
        state = UmagAccountSerializer(account).data

    if stores is not None:
        state['stores'] = _stores(stores)

    return state


def _stores(stores: list) -> list[dict]:
    return [
        {'id': store.get('id'), 'name': (store.get('name') or '').strip()}
        for store in stores
        if not store.get('deleted')
    ]
