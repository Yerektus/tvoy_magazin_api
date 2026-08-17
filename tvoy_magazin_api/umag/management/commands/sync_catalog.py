"""Выгрузка номенклатуры из UMAG. Место команды — в ночном cron.

    python manage.py sync_catalog

Идёт по всем подключённым кабинетам и обновляет копию номенклатуры каждого
выбранного магазина. Шесть тысяч товаров занимают секунд пятнадцать.
"""

from django.core.management.base import BaseCommand

from umag import catalog
from umag.client import UmagClient, UmagError
from umag.models import UmagAccount


class Command(BaseCommand):
    help = 'Обновляет копию номенклатуры UMAG, по которой ищутся товары накладной'

    def add_arguments(self, parser):
        parser.add_argument(
            '--store',
            type=int,
            help='Обновить только этот магазин',
        )

    def handle(self, *args, **options):
        accounts = UmagAccount.objects.exclude(store_id=None).exclude(token='')

        if options['store']:
            accounts = accounts.filter(store_id=options['store'])

        # У сотрудников одного магазина номенклатура общая — второй раз не ходим.
        done = set()

        for account in accounts:
            if account.store_id in done:
                continue

            done.add(account.store_id)
            store = account.store_name or account.store_id

            try:
                count = catalog.refresh(UmagClient(account, account.store_id), account.store_id)
            except UmagError as error:
                self.stderr.write(f'{store}: {error}')
                continue

            self.stdout.write(f'{store}: {count} товаров')

        if not done:
            self.stdout.write('Подключённых магазинов нет — обновлять нечего')
