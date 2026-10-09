# Hand-written: an ``AlterModelTable`` moves a table with no model rename at all, which is the
# second shape ``graph.replay_plan`` has to read off Django's migration state.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0052_auto_enforcement'),
    ]

    operations = [
        migrations.AlterModelTable(name='callback', table='testapp_callbacks'),
    ]
