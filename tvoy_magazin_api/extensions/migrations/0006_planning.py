from django.db import migrations

SLUG = 'planning'

DESCRIPTION = (
    'Планирование закупов смотрит, что и как быстро расходится с полки, и '
    'подсказывает, чего не хватит до следующего завоза. Продажи и остатки '
    'берутся из товарного отчёта UMAG — того же, что в кабинете.\n\n'
    'Расход считается за выбранный период: сколько штук или килограммов уходило '
    'в день. Дальше остаток делится на этот расход — получается, на сколько дней '
    'товара хватит. Что не доживает до горизонта закупа, попадает в план вместе '
    'с количеством и суммой по закупочной цене.\n\n'
    'Расширение работает поверх подключённого UMAG: считает по тому магазину, '
    'который выбран в шапке.'
)

FEATURES = (
    'Показывает, на сколько дней хватит остатка',
    'Считает расход по продажам за выбранный период',
    'Предлагает количество к заказу до конца горизонта',
    'Сначала то, что кончится раньше всех',
    'Считает сумму закупа по закупочным ценам',
)


def add_planning(apps, schema_editor):
    Extension = apps.get_model('extensions', 'Extension')
    ExtensionFeature = apps.get_model('extensions', 'ExtensionFeature')

    extension, _ = Extension.objects.update_or_create(
        slug=SLUG,
        defaults={
            'name': 'Планирование закупов',
            'summary': 'Что заканчивается на полке и сколько этого дозаказать.',
            'description': DESCRIPTION,
            'logo': '/logos/planning.svg',
            'is_active': True,
            'position': 1,
        },
    )

    for position, text in enumerate(FEATURES):
        ExtensionFeature.objects.update_or_create(
            extension=extension,
            text=text,
            defaults={'position': position},
        )

    # 1С уходит вниз списка: подключить его пока нельзя.
    Extension.objects.filter(slug='1c').update(position=2)


def remove_planning(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).delete()
    apps.get_model('extensions', 'Extension').objects.filter(slug='1c').update(position=1)


class Migration(migrations.Migration):
    dependencies = [('extensions', '0005_1c')]

    operations = [migrations.RunPython(add_planning, remove_planning)]
