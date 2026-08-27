import logging

from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from extensions import access
from invoices.models import Invoice

from . import client as umag_client
from . import supply
from .client import UmagAuthError, UmagClient, UmagError
from .models import UmagAccount
from .serializers import UmagAccountSerializer, UmagConnectSerializer, UmagStoreSerializer

logger = logging.getLogger(__name__)

# Код UMAG в каталоге расширений: на нём стоят надстройки вроде планирования.
SLUG = 'umag'


class UmagAccountView(APIView):
    """/api/umag/account/ — подключение сотрудника к своему кабинету UMAG."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Отдаём то, что лежит в базе, — включая сохранённый список магазинов.
        # Он нужен фронту для ссылки на приёмку: в адресе кабинета стоит
        # порядковый номер магазина, а не его id.
        return Response(_state(_fill_stores(_account(request))))

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


class UmagCategoriesView(APIView):
    """GET /api/umag/categories/ — полки кабинета.

    Нужны там, где мы заводим товар за человека: в карточке позиции он выбирает,
    куда положить новый товар, иначе тот уедет в «Незаданные» и потеряется среди
    сотен таких же.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        account = _account(request)

        if account is None or not account.ready:
            return Response({'categories': []})

        try:
            body = UmagClient(account).get(supply.CATEGORIES)
        except UmagError as error:
            logger.warning('UMAG не отдал категории: %s', error)
            return Response({'categories': []})

        rows = body if isinstance(body, list) else (body or {}).get('categories') or []

        return Response(
            {
                'categories': [
                    {'id': row.get('id'), 'name': (row.get('name') or '').strip()}
                    for row in rows
                    if isinstance(row, dict) and row.get('id')
                ]
            }
        )


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

        # Порядок такой: сначала человек отмечает накладную проверенной, и
        # только потом её можно отправить. Иначе в кабинет уехало бы то, что
        # никто не сверял с бумагой.
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
            # Ссылку на черновик собирает фронт: в адресе кабинета стоит не
            # номер магазина, а его порядковый номер в списке, и знает этот
            # список именно фронт.
            return {'supply_id': supply.push(invoice, account, agent_id)}

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


def _fill_stores(account):
    """Дописывает список магазинов учётке, заведённой до появления этого поля.

    Ходит в кабинет один раз на учётку, а не на каждое чтение: именно поход на
    каждое чтение когда-то и упирался в таймаут шлюза. Кабинет недоступен —
    оставляем пусто и попробуем в следующий раз, страницу из-за этого не роняем.
    """

    if account is None or account.stores or not account.token:
        return account

    try:
        stores = umag_client.stores(account.token)
    except UmagError as error:
        logger.warning('Не удалось дочитать магазины UMAG: %s', error)
        return account

    account.stores = _stores(stores)
    account.save(update_fields=('stores', 'refreshed_at'))

    return account


def _state(account, stores: list | None = None) -> dict:
    """Состояние подключения для фронта.

    `stores` передают там, где кабинет только что отдал свежий список, — вход и
    смена магазина. Тогда он заодно сохраняется, чтобы следующее чтение
    обошлось без похода в UMAG.
    """

    if account is None:
        return {'connected': False, 'phone': '', 'store_id': None, 'store_name': ''}

    if stores is not None:
        account.stores = _stores(stores)
        account.save(update_fields=('stores', 'refreshed_at'))

    state = UmagAccountSerializer(account).data
    state['stores'] = account.stores

    return state


def _stores(stores: list) -> list[dict]:
    return [
        {'id': store.get('id'), 'name': (store.get('name') or '').strip()}
        for store in stores
        if not store.get('deleted')
    ]
