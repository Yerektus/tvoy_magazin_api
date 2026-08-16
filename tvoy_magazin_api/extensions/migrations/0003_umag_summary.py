from django.db import migrations

SLUG = 'umag'

# Строка под названием описывает сам сервис, а не нашу интеграцию с ним:
# что даёт подключение, написано ниже на странице.
SUMMARY = 'Программа учёта товаров и продаж для розничного магазина.'
WAS = 'Проверенные накладные уходят в UMAG черновиком приёмки.'


def set_summary(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).update(summary=SUMMARY)


def restore_summary(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).update(summary=WAS)


class Migration(migrations.Migration):
    dependencies = [('extensions', '0002_umag')]

    operations = [migrations.RunPython(set_summary, restore_summary)]
