"""Права по роли в организации."""

from rest_framework.permissions import BasePermission


class ManagesOrganization(BasePermission):
    """Пускает только владельца и администратора.

    Менеджер работает с накладными наравне со всеми — здесь отсекается то, чем
    ведут саму организацию: расширения, а дальше и люди с настройками.
    """

    message = 'Доступно только владельцу и администратору организации'

    def has_permission(self, request, view):
        user = request.user

        return bool(user and user.is_authenticated and user.manages_organization)
