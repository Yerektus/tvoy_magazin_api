from django.db import migrations

SLUG = 'umag'

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

WAS = (
    'UMAG — кабинет магазина, в котором ведут товары, приёмки и продажи. '
    'Дашборд отправляет туда проверенные накладные, чтобы их не набивали руками.\n\n'
    'Вход у каждого сотрудника свой — тот же телефон и пароль, что и в кабинете. '
    'Пароль не сохраняется: он нужен один раз, чтобы получить токен сессии.'
)


def set_description(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).update(
        description=DESCRIPTION
    )


def restore_description(apps, schema_editor):
    apps.get_model('extensions', 'Extension').objects.filter(slug=SLUG).update(description=WAS)


class Migration(migrations.Migration):
    dependencies = [('extensions', '0003_umag_summary')]

    operations = [migrations.RunPython(set_description, restore_description)]
