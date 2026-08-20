"""Заготовки для тестов: пользователь без организации в кабинет не попадает.

Тесты остальных приложений заводят людей через `make_user` — иначе в каждом
пришлось бы помнить про организацию, а забытая всплыла бы не проверкой прав,
а падением на NOT NULL.
"""

from django.contrib.auth import get_user_model

from .models import Organization

User = get_user_model()


def make_organization(name='Магазин на углу') -> Organization:
    return Organization.objects.create(name=name)


def make_user(
    email='shop@tvoymagazin.kz',
    password='tainy-parol-123',
    organization=None,
    # Владелец по умолчанию: почти всем тестам нужен человек, которому всё
    # можно, а урезанные права проверяются отдельно и ролью явной.
    role=User.Role.OWNER,
    **extra,
) -> User:
    return User.objects.create_user(
        email=email,
        password=password,
        organization=organization or make_organization(),
        role=role,
        **extra,
    )
