from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('issue66_shop', '0006_retable_hub'), ('issue66_shop', '0006_repoint_hub')]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.DeleteModel('hub')]
        )
    ]
