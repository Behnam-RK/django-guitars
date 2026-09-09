# Hand-written: an ``AlterModelTable`` moves a table with no model rename at all, which is the
# second shape ``graph.renamed_tables`` has to resolve through Django's migration state.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0052_auto_enforcement'),
    ]

    operations = [
        migrations.AlterModelTable(name='callback', table='testapp_callbacks'),
    ]
