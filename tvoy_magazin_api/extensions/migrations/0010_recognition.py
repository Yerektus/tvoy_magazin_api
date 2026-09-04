from django.db import migrations

SLUG = 'recognition'

DESCRIPTION = (
    'Распознавание документов читает фото накладной и собирает из него карточку: '
    'поставщика, номер, дату, итог и каждую позицию со штрихкодом, количеством '
    'и ценой. Снимок можно сделать в приложении или выбрать из галереи — разбор '
    'идёт на сервере, и через минуту в списке уже строки, а не картинка.\n\n'
    'Модель ошибается на печатях и кривых кадрах, поэтому каждую накладную '
    'человек сверяет с бумагой: правит строку, дописывает пропущенную, выкидывает '
    'лишнюю. Когда всё сошлось — отмечает проверенной, и тогда её можно отправить '
    'в приёмку UMAG, если кабинет подключён.\n\n'
    'Расширение не зависит от UMAG: распознавать можно и без учётки, а в кабинет '
    'накладная уедет позже, когда вход появится.'
)

FEATURES = (
    'Читает поставщика, номер, дату и итог с фото',
    'Собирает позиции: название, штрихкод, количество, цена',
    'Несколько листов одной накладной разбираются вместе',
    'Строки можно поправить, дописать и выкинуть до проверки',
    'Работает без UMAG — в приёмку накладная уедет отдельно',
)


def add_recognition(apps, schema_editor):
    Extension = apps.get_model('extensions', 'Extension')
    ExtensionFeature = apps.get_model('extensions', 'ExtensionFeature')

    extension, _ = Extension.objects.update_or_create(
        slug=SLUG,
        defaults={
            'name': 'Распознавание документов',
            'summary': 'Накладная с фото становится списком позиций.',
            'description': DESCRIPTION,
            'logo': '/logos/recognition.svg',
            'is_active': True,
            'position': 0,
        },
    )

    for position, text in enumerate(FEATURES):
        ExtensionFeature.objects.update_or_create(
            extension=extension,
            text=text,
            defaults={'position': position},
        )

    # Остальные чуть ниже: распознавание — то, с чего начинается приёмка.
    Extension.objects.filter(slug='umag').update(position=1)
    Extension.objects.filter(slug='planning').update(position=2)
    Extension.objects.filter(slug='1c').update(position=3)


def remove_recognition(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).delete()
    apps.get_model('extensions', 'Extension').objects.filter(slug='umag').update(position=0)
    apps.get_model('extensions', 'Extension').objects.filter(slug='planning').update(position=1)
    apps.get_model('extensions', 'Extension').objects.filter(slug='1c').update(position=2)


class Migration(migrations.Migration):
    dependencies = [('extensions', '0009_planning_requires_umag')]

    operations = [migrations.RunPython(add_recognition, remove_recognition)]
