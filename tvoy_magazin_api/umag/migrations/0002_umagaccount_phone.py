from django.db import migrations, models


class Migration(migrations.Migration):
    """В UMAG заходят по номеру телефона, а не по выдуманному логину."""

    dependencies = [('umag', '0001_initial')]

    operations = [
        migrations.RenameField(
            model_name='umagaccount',
            old_name='login',
            new_name='phone',
        ),
        migrations.AlterField(
            model_name='umagaccount',
            name='phone',
            field=models.CharField(max_length=32, verbose_name='телефон в UMAG'),
        ),
    ]
