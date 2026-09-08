# Hand-written, as Django writes it when the author answers its rename prompt: the autodetector
# cannot tell a rename from a create-and-delete without being asked. The corpus needs a real
# ``RenameModel`` for the rename-aware coverage the generator grew in 2.9.0.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0050_auto_enforcement'),
    ]

    operations = [
        migrations.RenameModel(old_name='Encore', new_name='Callback'),
    ]
