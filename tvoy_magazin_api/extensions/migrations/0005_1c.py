from django.db import migrations

SLUG = '1c'

DESCRIPTION = (
    '1С — учётная система, в которой ведут бухгалтерию, склад и торговлю. '
    'В рознице через неё проводят поступления товаров, списания, переоценку '
    'и сдают отчётность.\n\n'
    'Подключение готовится: расширение уже в каталоге, но войти в него пока нельзя. '
    'Когда интеграция будет готова, накладные можно будет отправлять в 1С так же, '
    'как сейчас в UMAG.'
)


def add_1c(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.update_or_create(
        slug=SLUG,
        defaults={
            'name': '1С',
            'summary': 'Учётная система для бухгалтерии, склада и торговли.',
            'description': DESCRIPTION,
            'logo': '/logos/1c.svg',
            'is_active': True,
            'position': 1,
        },
    )


def remove_1c(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [('extensions', '0004_umag_description')]

    operations = [migrations.RunPython(add_1c, remove_1c)]
