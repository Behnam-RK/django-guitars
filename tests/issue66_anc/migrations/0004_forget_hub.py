from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('issue66_anc', '0003_retable_hub'), ('issue66_shop', '0004_repoint_hub')]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel('hub')]
        )
    ]
