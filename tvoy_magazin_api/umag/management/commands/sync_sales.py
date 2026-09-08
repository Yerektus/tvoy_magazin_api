"""Полная или инкрементальная выгрузка чеков UMAG для прогноза."""

from django.core.management.base import BaseCommand, CommandError

from umag import sales
from umag.client import UmagError
from umag.models import UmagAccount


class Command(BaseCommand):
    help = 'Синхронизирует чеки и возвраты UMAG для прогноза закупа'

    def add_arguments(self, parser):
        parser.add_argument('--store', type=int, help='Синхронизировать только этот магазин')
        parser.add_argument(
            '--full',
            action='store_true',
            help='Повторно прочитать всю доступную историю',
        )

    def handle(self, *args, **options):
        accounts = (
            UmagAccount.objects.select_related('user__organization')
            .exclude(store_id=None)
            .exclude(token='')
        )

        if options['store']:
            accounts = accounts.filter(store_id=options['store'])

        # У сотрудников одной организации и магазина продажи общие. Берём
        # первый рабочий токен и не загружаем одни чеки несколько раз.
        done = set()
        failed = 0

        for account in accounts:
            key = (account.user.organization_id, account.store_id)

            if key in done:
                continue

            done.add(key)
            label = account.store_name or account.store_id

            try:
                result = sales.sync(account, full=options['full'])
            except (UmagError, RuntimeError, ValueError) as error:
                failed += 1
                self.stderr.write(f'{label}: {error}')
                continue

            start = result.history_from.date() if result.history_from else 'нет продаж'
            self.stdout.write(
                self.style.SUCCESS(
                    f'{label}: {result.sales} чеков, {result.refunds} возвратов; история с {start}'
                )
            )

        if not done:
            self.stdout.write('Подключённых магазинов нет — синхронизировать нечего')

        if failed:
            raise CommandError(f'Не удалось синхронизировать магазинов: {failed}')
