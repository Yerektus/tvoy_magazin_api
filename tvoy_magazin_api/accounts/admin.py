from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Organization, User


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'members_count', 'created_at')
    search_fields = ('name',)

    @admin.display(description='сотрудников')
    def members_count(self, organization):
        return organization.members.count()


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ('email',)
    list_display = ('email', 'name', 'organization', 'role', 'is_active')
    list_filter = ('organization', 'role', 'is_active', 'is_staff')
    search_fields = ('email', 'name')

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Профиль', {'fields': ('name',)}),
        # Пока людей заводят отсюда: своего экрана для приглашений ещё нет.
        ('Организация', {'fields': ('organization', 'role')}),
        ('Права', {'fields': ('is_active', 'is_staff', 'is_superuser', 'groups', 'user_permissions')}),
        ('Даты', {'fields': ('last_login', 'date_joined')}),
    )

    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': ('email', 'name', 'organization', 'role', 'password1', 'password2'),
            },
        ),
    )
