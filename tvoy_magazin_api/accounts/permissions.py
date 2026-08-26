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


class UsesPurchases(BasePermission):
    """Пускает в закупки: ведущих организацию и тех, кому доступ выдали руками.

    Прятать раздел в приложении мало: адрес ручки известен, и запрос из
    консоли считал бы весь закуп магазина.
    """

    message = 'Доступ к закупкам не открыт'

    def has_permission(self, request, view):
        user = request.user

        return bool(user and user.is_authenticated and user.uses_purchases)


class UsesAssistant(BasePermission):
    """Пускает к помощнику — по тому же правилу, что и в закупки."""

    message = 'Доступ к помощнику не открыт'

    def has_permission(self, request, view):
        user = request.user

        return bool(user and user.is_authenticated and user.uses_assistant)
