"""Права по роли в организации."""

from rest_framework.permissions import BasePermission


class ManagesOrganization(BasePermission):
    message = 'Доступно только владельцу и администратору организации'

    def has_permission(self, request, view):
        user = request.user

        return bool(user and user.is_authenticated and user.manages_organization)


class UsesPurchases(BasePermission):
    message = 'Доступ к закупкам не открыт'

    def has_permission(self, request, view):
        user = request.user

        return bool(user and user.is_authenticated and user.uses_purchases)


class UsesAssistant(BasePermission):
    message = 'Доступ к помощнику не открыт'

    def has_permission(self, request, view):
        user = request.user

        return bool(user and user.is_authenticated and user.uses_assistant)
