"""Подключение расширений: что от чего зависит.

Надстройка живёт только пока подключено то, на чём она стоит: планирование
закупов без UMAG считать нечем. Поэтому отключение основы утягивает за собой
и надстройки — иначе расширение числится подключённым, а работать не может.
"""

from .models import Extension, ExtensionInstall


def revoke(user, slug: str) -> list[str]:
    """Отключает расширения, которым нужен `slug`. Возвращает их коды.

    Цепочка может быть длиннее одного шага: если поверх надстройки появится
    ещё одна, снимутся обе.
    """

    revoked: list[str] = []
    pending = [slug]

    while pending:
        current = pending.pop()

        dependent = list(
            ExtensionInstall.objects.filter(
                user=user,
                extension__in=Extension.objects.filter(requires__slug=current),
            ).values_list('extension__slug', flat=True)
        )

        if not dependent:
            continue

        ExtensionInstall.objects.filter(user=user, extension__slug__in=dependent).delete()

        revoked.extend(dependent)
        pending.extend(dependent)

    return revoked
