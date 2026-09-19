from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('umag', '0009_sale_promo_quantity'),
    ]

    operations = [
        migrations.AddField(
            model_name='umagsoldproduct',
            name='forecast_error',
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                max_digits=8,
                null=True,
                verbose_name='ошибка прогноза',
            ),
        ),
        migrations.AddField(
            model_name='umagsoldproduct',
            name='forecast_on',
            field=models.DateField(
                blank=True,
                null=True,
                verbose_name='прогноз на дату',
            ),
        ),
    ]
