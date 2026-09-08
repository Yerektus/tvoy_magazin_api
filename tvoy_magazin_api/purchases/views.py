from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import ManagesOrganization, UsesPurchases
from extensions.models import Extension, ExtensionInstall
from umag.models import UmagAccount, UmagSalesSync

from . import products, tasks
from .models import ApprovedPurchase, ApprovedPurchaseItem, PurchasePlan
from .serializers import (
    ApproveSupplierSerializer,
    ApprovedPurchaseSerializer,
    ProductsSnapshotSerializer,
    PurchasePlanListSerializer,
    PurchasePlanRequestSerializer,
    PurchasePlanSerializer,
)

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


class StoreProductsView(APIView):
    """GET /api/purchases/products/ — товары из продаж выбранного магазина.

    POST запускает выгрузку чеков из UMAG. Пока она идёт, страница опрашивает
    тот же GET: статус `syncing`, а список уже может быть непустым.
    """

    permission_classes = [IsAuthenticated, UsesPurchases]

    def get(self, request):
        return Response(ProductsSnapshotSerializer(products.snapshot(_account(request.user))).data)

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

        state, created = UmagSalesSync.objects.get_or_create(
            organization=account.user.organization,
            store_id=account.store_id,
        )

        if created or state.status != UmagSalesSync.Status.SYNCING:
            if not created:
                state.status = UmagSalesSync.Status.SYNCING
                state.error = ''
                state.save(update_fields=('status', 'error'))

            tasks.schedule_sales_sync(account)

        return Response(
            ProductsSnapshotSerializer(products.snapshot(account)).data,
            status=status.HTTP_202_ACCEPTED,
        )


class PurchasePlanListView(APIView):
    """GET /api/purchases/plans/ — планировки выбранного магазина, без позиций."""

    permission_classes = [IsAuthenticated, UsesPurchases]

    def get(self, request):
        account = _account(request.user)

        if account is None or not account.store_id:
            return Response([])

        plans = PurchasePlan.objects.filter(user=request.user, store_id=account.store_id)
        return Response(PurchasePlanListSerializer(plans, many=True).data)


class PurchasePlanDetailView(APIView):
    """GET/POST/DELETE /api/purchases/plans/<id>/ — одна планировка.

    Пересчёт не создаёт новую запись: имя и место в списке остаются, меняются
    условия и строки. Удаление убирает и готовый план, не только считающийся.
    """

    permission_classes = [IsAuthenticated, UsesPurchases]

    def get(self, request, pk):
        plan = _owned_plan(request, pk)

        if plan is None:
            return Response({'detail': 'План не найден'}, status=status.HTTP_404_NOT_FOUND)

        return Response(PurchasePlanSerializer(plan).data)

    def post(self, request, pk):
        plan = _owned_plan(request, pk)

        if plan is None:
            return Response({'detail': 'План не найден'}, status=status.HTTP_404_NOT_FOUND)

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

        if plan.status == PurchasePlan.Status.BUILDING:
            return Response(
                {'detail': 'План ещё считается'},
                status=status.HTTP_409_CONFLICT,
            )

        form = PurchasePlanRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        for field, value in form.validated_data.items():
            setattr(plan, field, value)

        plan.status = PurchasePlan.Status.BUILDING
        plan.error = ''
        plan.items_total = 0
        plan.total_cost = 0
        plan.built_at = None
        plan.save()
        plan.items.all().delete()

        tasks.schedule(plan)
        plan.refresh_from_db()

        return Response(PurchasePlanSerializer(plan).data)

    def delete(self, request, pk):
        plan = _owned_plan(request, pk)

        if plan is None:
            return Response({'detail': 'План не найден'}, status=status.HTTP_404_NOT_FOUND)

        plan.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PurchasePlanView(APIView):
    """/api/purchases/plan/ — последний план по выбранному магазину и новая планировка.

    Список живёт отдельно: телефон по-прежнему читает последний план, а
    кабинет открывает конкретную запись. Прошлые планировки больше не стираем.
    """

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

        plan = PurchasePlan.objects.create(
            user=request.user,
            store_id=account.store_id,
            store_name=account.store_name,
            # Чего не прислали — остаётся по умолчанию: 30 дней, горизонт в две
            # недели и учёт остатка. Пустые значения не отсеиваем: «не учитывать
            # остаток» — это `False`, и по прежнему условию оно молча терялось.
            **form.validated_data,
        )

        tasks.schedule(plan)
        plan.refresh_from_db()

        return Response(PurchasePlanSerializer(plan).data, status=status.HTTP_201_CREATED)

    def delete(self, request):
        """Отменяет расчёт: недосчитанный план удаляем, готовый не трогаем."""

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

        building = PurchasePlan.objects.filter(
            user=request.user,
            store_id=account.store_id,
            status=PurchasePlan.Status.BUILDING,
        )
        plan_id = request.query_params.get('id')

        if plan_id:
            building = building.filter(pk=plan_id)

        building.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class ApproveSupplierView(APIView):
    """POST /api/purchases/plan/approve/ — одобрить закуп у одного поставщика.

    Строки копируются в отдельную запись: пересчёт плана их уже не затрёт.
    Из текущего плана группа уходит — повторно одобрить того же нечего.
    """

    permission_classes = [IsAuthenticated, UsesPurchases]

    def post(self, request):
        if not _installed(request.user):
            return Response(
                {'detail': 'Подключите расширение «Планирование закупов»'},
                status=status.HTTP_409_CONFLICT,
            )

        account = _account(request.user)

        if account is None or not account.store_id:
            return Response(
                {'detail': 'Выберите магазин'},
                status=status.HTTP_409_CONFLICT,
            )

        form = ApproveSupplierSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        supplier = form.validated_data['supplier']
        plan_id = form.validated_data.get('plan')

        plans = PurchasePlan.objects.filter(
            user=request.user,
            store_id=account.store_id,
            status=PurchasePlan.Status.READY,
        )
        plan = plans.filter(pk=plan_id).first() if plan_id else plans.first()

        if plan is None:
            return Response(
                {'detail': 'Нет готового плана для одобрения'},
                status=status.HTTP_409_CONFLICT,
            )

        lines = list(plan.items.filter(supplier=supplier).order_by('position'))

        if not lines:
            return Response(
                {'detail': 'В плане нет позиций этого поставщика'},
                status=status.HTTP_404_NOT_FOUND,
            )

        with transaction.atomic():
            total = sum((line.cost or Decimal('0') for line in lines), Decimal('0'))

            purchase = ApprovedPurchase.objects.create(
                user=request.user,
                store_id=plan.store_id,
                store_name=plan.store_name,
                supplier=supplier,
                items_total=len(lines),
                total_cost=total,
            )

            ApprovedPurchaseItem.objects.bulk_create(
                [
                    ApprovedPurchaseItem(
                        purchase=purchase,
                        position=number,
                        barcode=line.barcode,
                        name=line.name,
                        measure=line.measure,
                        sold=line.sold,
                        stock=line.stock,
                        per_day=line.per_day,
                        cover_days=line.cover_days,
                        forecast_model=line.forecast_model,
                        forecast_quantity=line.forecast_quantity,
                        forecast_per_day=line.forecast_per_day,
                        safety_stock=line.safety_stock,
                        holiday_factor=line.holiday_factor,
                        forecast_error=line.forecast_error,
                        is_perishable=line.is_perishable,
                        shelf_life_days=line.shelf_life_days,
                        purchase_horizon=line.purchase_horizon,
                        perishability_source=line.perishability_source,
                        suggested=line.suggested,
                        price=line.price,
                        cost=line.cost,
                    )
                    for number, line in enumerate(lines, start=1)
                ]
            )

            plan.items.filter(pk__in=[line.pk for line in lines]).delete()
            _refresh_plan(plan)

        return Response(
            ApprovedPurchaseSerializer(purchase).data,
            status=status.HTTP_201_CREATED,
        )


class ApprovedPurchaseListView(APIView):
    """GET /api/purchases/approved/ — одобренные закупки выбранного магазина."""

    permission_classes = [IsAuthenticated, UsesPurchases]

    def get(self, request):
        account = _account(request.user)

        if account is None or not account.store_id:
            return Response([])

        purchases = (
            ApprovedPurchase.objects.filter(user=request.user, store_id=account.store_id)
            .prefetch_related('items')
            .order_by('-approved_at')
        )

        return Response(ApprovedPurchaseSerializer(purchases, many=True).data)


def _owned_plan(request, pk) -> PurchasePlan | None:
    """План этого сотрудника и выбранного магазина. Чужой — как будто нет."""

    account = _account(request.user)

    if account is None or not account.store_id:
        return None

    return PurchasePlan.objects.filter(
        pk=pk,
        user=request.user,
        store_id=account.store_id,
    ).first()


def _refresh_plan(plan: PurchasePlan) -> None:
    """Пересчитывает итоги плана после того, как из него ушла группа."""

    remaining = list(plan.items.order_by('position'))

    for position, line in enumerate(remaining, start=1):
        if line.position != position:
            plan.items.filter(pk=line.pk).update(position=position)

    total = plan.items.aggregate(Sum('cost'))['cost__sum'] or Decimal('0')
    plan.items_total = len(remaining)
    plan.total_cost = total
    plan.save(update_fields=('items_total', 'total_cost'))


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
