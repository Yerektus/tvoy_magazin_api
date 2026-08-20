"""Раздача ролей из консоли.

    python manage.py set_role shop@tvoymagazin.kz owner
    python manage.py set_role --list

Роли раздаёт человек, а не код: миграция заводит организации, но владельца в
ней не назначает. Экрана для этого пока нет, а в админку Django нужен
суперпользователь, — вот способ обойтись без обоих.
"""

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User


class Command(BaseCommand):
    help = 'Назначает пользователю роль в его организации'

    def add_arguments(self, parser):
        parser.add_argument('email', nargs='?', help='почта пользователя')
        parser.add_argument(
            'role',
            nargs='?',
            choices=[value for value, _ in User.Role.choices],
            help='owner, admin или manager',
        )
        parser.add_argument(
            '--list',
            action='store_true',
            help='показать, кто в какой организации и с какой ролью',
        )

    def handle(self, *args, **options):
        if options['list']:
            self.show_everyone()
            return

        if not options['email'] or not options['role']:
            raise CommandError('Нужны почта и роль: set_role shop@tvoymagazin.kz owner')

        try:
            user = User.objects.get(email=options['email'])
        except User.DoesNotExist as error:
            raise CommandError(f'Нет такого пользователя: {options["email"]}') from error

        if user.organization_id is None:
            raise CommandError(
                f'{user.email} не привязан к организации — роль ему давать не в чем',
            )

        was = user.get_role_display()
        user.role = options['role']
        user.save(update_fields=('role',))

        self.stdout.write(
            self.style.SUCCESS(
                f'{user.email} в организации «{user.organization}»: '
                f'{was} → {user.get_role_display()}'
            )
        )

    def show_everyone(self):
        users = User.objects.select_related('organization').order_by('organization__name', 'email')

        if not users:
            self.stdout.write('Пользователей нет')
            return

        for user in users:
            organization = user.organization or 'без организации'
            self.stdout.write(f'{user.email:40} {user.get_role_display():15} {organization}')
