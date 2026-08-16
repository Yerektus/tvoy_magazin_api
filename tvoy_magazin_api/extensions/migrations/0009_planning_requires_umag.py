from django.db import migrations


def link(apps, schema_editor):
    Extension = apps.get_model('extensions', 'Extension')

    planning = Extension.objects.filter(slug='planning').first()
    umag = Extension.objects.filter(slug='umag').first()

    # Планированию неоткуда взять продажи и остатки без подключённого UMAG.
    if planning and umag:
        planning.requires.add(umag)


def unlink(apps, schema_editor):
    Extension = apps.get_model('extensions', 'Extension')
    planning = Extension.objects.filter(slug='planning').first()

    if planning:
        planning.requires.clear()


class Migration(migrations.Migration):
    dependencies = [('extensions', '0008_extension_requires')]

    operations = [migrations.RunPython(link, unlink)]
