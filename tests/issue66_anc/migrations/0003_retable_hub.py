from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('issue66_shop', '0002_auto_enforcement'), ('issue66_anc', '0002_auto_enforcement')]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[migrations.AlterModelTable('hub', 'issue66_shop_hub')],
            state_operations=[migrations.AlterModelTable('hub', 'issue66_shop_hub')],
        )
    ]
