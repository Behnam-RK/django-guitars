# Hand-written, as Django writes it when the author answers its rename prompt. The point of
# this one is that it moves **no table**: 0053 pinned the model's ``db_table``, so the rename
# changes the model name alone -- the shape a table-diff would read as a rename and get wrong.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0054_auto_enforcement'),
    ]

    operations = [
        migrations.RenameModel(old_name='Callback', new_name='Refrain'),
    ]
