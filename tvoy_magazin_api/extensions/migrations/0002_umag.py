from django.db import migrations

SLUG = 'umag'

SUMMARY = 'Программа учёта товаров и продаж для розничного магазина.'

DESCRIPTION = (
    'UMAG — программа автоматизации розничной торговли: учёт товаров и продаж, касса, '
    'ценники, штрихкодирование, контроль кассиров и аналитика по магазину. '
    'Подходит любому формату розницы — продуктовому, аптеке, одежде, косметике.\n\n'
    'Работает по подписке и в облаке: продажи, остатки и отчёты видно с моноблока '
    'в магазине, компьютера в офисе или телефона. UMAG работает по всему Казахстану, '
    'а также в Кыргызстане, Узбекистане и Таджикистане.\n\n'
    'Вход у каждого сотрудника свой — тот же телефон и пароль, что и в кабинете UMAG. '
    'Пароль не сохраняется: он нужен один раз, чтобы получить токен сессии.'
)

FEATURES = (
    'Накладная уходит в приёмку одной кнопкой',
    'Позиции сопоставляются с товарами по штрихкоду',
    'Поставщик подставляется из контрагентов кабинета',
    'Перед отправкой видно, что мешает: строки без штрихкода, цены или количества',
    'Приёмка создаётся черновиком — проверить и провести можно в UMAG',
)


def add_umag(apps, schema_editor):
    Extension = apps.get_model('extensions', 'Extension')
    ExtensionFeature = apps.get_model('extensions', 'ExtensionFeature')

    extension, _ = Extension.objects.update_or_create(
        slug=SLUG,
        defaults={
            'name': 'UMAG',
            'summary': SUMMARY,
            'description': DESCRIPTION,
            'logo': '/logos/umag.svg',
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


def remove_umag(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [('extensions', '0001_initial')]

    operations = [migrations.RunPython(add_umag, remove_umag)]
