import django.db.models.deletion
import guitars.models.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('issue66_shop', '0005_auto_enforcement'), ('issue66_anc', '0006_adopt_hub')]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    'spoke', 'hub', models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='spokes', to='issue66_anc.hub',
                    ),
                ),
                migrations.AlterField(
                    'keeper', 'hub', guitars.models.fields.OwningForeignKey(
                        null=True, on_delete=django.db.models.deletion.DO_NOTHING,
                        related_name='keepers', to='issue66_anc.hub',
                    ),
                ),
            ]
        )
    ]
