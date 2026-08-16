"""Фоновый пересчёт плана закупа.

Отчёт по товарам приходит страницами по тысяче строк — это несколько секунд.
Держать на них открытый запрос незачем: страница опрашивает статус плана, как
и при разборе накладной.
"""

import logging
import threading

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from . import planner
from .models import PurchasePlan

logger = logging.getLogger(__name__)


def schedule(plan: PurchasePlan) -> None:
    """Ставит пересчёт в фон после того, как план закоммитится."""

    if settings.INVOICE_PARSE_INLINE:
        # Для тестов и отладки: считаем прямо в запросе, без потока.
        run(plan.pk)
        return

    transaction.on_commit(
        lambda: threading.Thread(target=_run_in_thread, args=(plan.pk,), daemon=True).start()
    )


def _run_in_thread(plan_id: int) -> None:
    close_old_connections()
    try:
        run(plan_id)
    finally:
        close_old_connections()


def run(plan_id: int) -> None:
    try:
        plan = PurchasePlan.objects.get(pk=plan_id)
    except PurchasePlan.DoesNotExist:
        return

    try:
        planner.build(plan)
    except planner.PlanError as error:
        _fail(plan, str(error))
        return
    except Exception as error:  # noqa: BLE001 — иначе поток умрёт молча
        logger.exception('Не удалось посчитать план закупа %s', plan_id)
        _fail(plan, f'Внутренняя ошибка: {error}')
        return

    plan.status = PurchasePlan.Status.READY
    plan.error = ''
    plan.built_at = timezone.now()
    plan.save(update_fields=('status', 'error', 'built_at', 'items_total', 'total_cost'))


def _fail(plan: PurchasePlan, message: str) -> None:
    PurchasePlan.objects.filter(pk=plan.pk).update(
        status=PurchasePlan.Status.FAILED,
        error=message[:1000],
        built_at=timezone.now(),
    )
