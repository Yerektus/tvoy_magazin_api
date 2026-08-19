from django.db.models import Q, Sum
from rest_framework import generics, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from umag import matching
from umag.models import UmagAccount

from . import tasks
from .models import Invoice, InvoiceLine
from .serializers import (
    InvoiceCreateSerializer,
    InvoiceDetailSerializer,
    InvoiceLineSerializer,
    InvoiceListSerializer,
)


class InvoiceQuerysetMixin:
    """Накладные сотрудника — все, независимо от выбранного магазина.

    По магазину отбирается только список: открытую накладную чужого магазина
    нужно и дочитать, и поправить, даже если в шапке уже переключились.
    """

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Invoice.objects.filter(created_by=self.request.user)


class InvoiceListCreateView(InvoiceQuerysetMixin, generics.ListCreateAPIView):
    """GET /api/invoices/?tab=… — список; POST — загрузка фото накладной.

    Список — по магазину, выбранному в шапке: приёмки из него уедут в свой
    кабинет, и держать документы разных магазинов в одной куче незачем.

    Вкладки списка: `pending` — ещё не проверенные, `checked` — проверенные,
    `deleted` — удалённые (они лежат в базе, просто скрыты). Без параметра —
    все живые.
    """

    parser_classes = [MultiPartParser, FormParser]

    def get_serializer_class(self):
        return InvoiceCreateSerializer if self.request.method == 'POST' else InvoiceListSerializer

    def get_queryset(self):
        tab = self.request.query_params.get('tab')

        if tab == 'deleted':
            return _of_store(
                Invoice.all_objects.filter(
                    created_by=self.request.user,
                    deleted_at__isnull=False,
                ),
                self.request.user,
            )

        queryset = _of_store(super().get_queryset(), self.request.user)

        if tab == 'pending':
            return queryset.exclude(status=Invoice.Status.CHECKED)

        if tab == 'checked':
            return queryset.filter(status=Invoice.Status.CHECKED)

        return queryset

    def perform_create(self, serializer):
        invoice = serializer.save(created_by=self.request.user, **_store(self.request.user))
        # Ответ уходит сразу, распознавание идёт в фоне — фронт опрашивает статус.
        tasks.schedule(invoice)


class InvoiceCountsView(InvoiceQuerysetMixin, APIView):
    """GET /api/invoices/counts/ — сколько накладных в каждой вкладке списка.

    Считаем по тому же магазину, что и список: числа у вкладок должны сходиться
    с тем, что под ними лежит.
    """

    def get(self, request):
        alive = _of_store(self.get_queryset(), request.user)
        deleted = _of_store(
            Invoice.all_objects.filter(created_by=request.user, deleted_at__isnull=False),
            request.user,
        )

        return Response(
            {
                'all': alive.count(),
                'pending': alive.exclude(status=Invoice.Status.CHECKED).count(),
                'checked': alive.filter(status=Invoice.Status.CHECKED).count(),
                'deleted': deleted.count(),
            }
        )


class InvoiceDetailView(InvoiceQuerysetMixin, generics.RetrieveUpdateDestroyAPIView):
    """/api/invoices/<id>/ — накладная с позициями.

    GET отдаёт карточку, PATCH правит поставщика (модель читает его название и
    БИН с бумаги хуже всего — там печать, а не таблица), DELETE помечает
    накладную удалённой.
    """

    serializer_class = InvoiceDetailSerializer

    def get_queryset(self):
        # Удалённые открываются на чтение: их видно во вкладке списка, и
        # тыкать в строку, которая ведёт в никуда, странно.
        return Invoice.all_objects.filter(created_by=self.request.user)

    def update(self, request, *args, **kwargs):
        # Удалённую открывают только посмотреть — править там нечего.
        if self.get_object().deleted_at is not None:
            return Response(
                {'detail': 'Накладная удалена'},
                status=status.HTTP_409_CONFLICT,
            )

        return super().update(request, *args, **kwargs)

    def perform_destroy(self, instance):
        # Из базы ничего не выкидываем — накладная просто уходит из выдачи.
        instance.soft_delete()


class InvoiceLineCreateView(InvoiceQuerysetMixin, generics.CreateAPIView):
    """POST /api/invoices/<pk>/lines/ — дописать позицию, которую модель пропустила."""

    serializer_class = InvoiceLineSerializer

    def perform_create(self, serializer):
        invoice = generics.get_object_or_404(self.get_queryset(), pk=self.kwargs['pk'])
        # Пустая строка встаёт первой: человек видит её сразу, без прокрутки таблицы.
        _shift_down(invoice)

        serializer.save(
            invoice=invoice,
            position=1,
            # Название человек впишет в таблице, но пустым его модель не хранит.
            name=serializer.validated_data.get('name') or 'Новая позиция',
        )
        _refresh_total(invoice)


class InvoiceLineView(generics.RetrieveUpdateDestroyAPIView):
    """/api/invoices/<pk>/lines/<line_id>/ — правка и удаление позиции.

    Модель то придумывает лишнюю строку, то склеивает две в одну, поэтому
    человеку нужны обе операции: поправить значение и выкинуть строку целиком.
    """

    permission_classes = [IsAuthenticated]
    serializer_class = InvoiceLineSerializer
    lookup_url_kwarg = 'line_id'

    def get_queryset(self):
        return InvoiceLine.objects.filter(
            invoice__pk=self.kwargs['pk'],
            invoice__created_by=self.request.user,
            invoice__deleted_at__isnull=True,
        )

    def perform_update(self, serializer):
        line = serializer.save()

        # Название или штрихкод поправили — прежнее сопоставление с товаром
        # UMAG к строке больше не относится, при следующей проверке ищем заново.
        if {'name', 'barcode'} & set(serializer.validated_data):
            matching.forget(line)

        _refresh_total(line.invoice)

    def perform_destroy(self, instance):
        invoice = instance.invoice
        instance.delete()
        _renumber(invoice)
        _refresh_total(invoice)


def _store(user) -> dict:
    """Магазин, выбранный на момент загрузки: туда накладная и уедет.

    UMAG не подключён или магазин ещё не выбран — оставляем пусто, при отправке
    возьмётся тот, что будет выбран тогда.
    """

    account = _account(user)

    if account is None or not account.store_id:
        return {}

    return {'umag_store_id': account.store_id, 'umag_store_name': account.store_name}


def _of_store(queryset, user):
    """Оставляет накладные выбранного магазина.

    UMAG не подключён — фильтровать не по чему, отдаём всё. Накладные без
    магазина видны в любом: их завели до подключения, и спрятать их значит
    потерять — открыть их будет неоткуда.
    """

    account = _account(user)

    if account is None or not account.store_id:
        return queryset

    return queryset.filter(Q(umag_store_id=account.store_id) | Q(umag_store_id__isnull=True))


def _account(user):
    return UmagAccount.objects.filter(user=user).first()


def _refresh_total(invoice):
    """Итог накладной — всегда сумма строк, даже после правок руками."""

    invoice.total = invoice.lines.aggregate(Sum('total'))['total__sum']
    invoice.save(update_fields=('total',))


def _shift_down(invoice):
    """Освобождает первый номер под дописанную строку.

    Номера растут, поэтому идём с конца: иначе строка встанет на место соседней,
    которая ещё не сдвинулась, и сработает ограничение на уникальность позиции.
    """

    for line in invoice.lines.order_by('-position'):
        invoice.lines.filter(pk=line.pk).update(position=line.position + 1)


def _renumber(invoice):
    """Убирает дыры в нумерации после удаления позиции.

    Номера только уменьшаются, поэтому занятых мест по пути не встретится и
    ограничение на уникальность позиции не сработает.
    """

    for position, line in enumerate(list(invoice.lines.all()), start=1):
        if line.position != position:
            invoice.lines.filter(pk=line.pk).update(position=position)


class InvoiceCheckView(InvoiceQuerysetMixin, APIView):
    """POST /api/invoices/<id>/check/ — человек сверил данные с бумагой."""

    def post(self, request, pk):
        invoice = generics.get_object_or_404(self.get_queryset(), pk=pk)

        if invoice.status not in (Invoice.Status.DONE, Invoice.Status.CHECKED):
            return Response(
                {'detail': 'Проверять можно только разобранную накладную'},
                status=status.HTTP_409_CONFLICT,
            )

        invoice.mark_checked(request.user)
        return Response(InvoiceDetailSerializer(invoice, context={'request': request}).data)


class InvoiceRetryView(InvoiceQuerysetMixin, APIView):
    """POST /api/invoices/<id>/retry/ — перезапустить разбор."""

    def post(self, request, pk):
        invoice = generics.get_object_or_404(self.get_queryset(), pk=pk)

        if invoice.status == Invoice.Status.PROCESSING:
            return Response(
                {'detail': 'Накладная уже распознаётся'},
                status=status.HTTP_409_CONFLICT,
            )

        # Данные будут другими — прежняя отметка о проверке к ним не относится.
        # Вместе с ней снимаем и годность к обучению: строки снова станут
        # догадкой модели, а учить на них — закреплять её же ошибки.
        invoice.status = Invoice.Status.PENDING
        invoice.error = ''
        invoice.checked_at = None
        invoice.checked_by = None
        invoice.for_training = False
        invoice.save(update_fields=('status', 'error', 'checked_at', 'checked_by', 'for_training'))
        tasks.schedule(invoice)

        return Response(InvoiceDetailSerializer(invoice).data, status=status.HTTP_202_ACCEPTED)
