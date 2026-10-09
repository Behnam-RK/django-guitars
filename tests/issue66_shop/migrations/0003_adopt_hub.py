import django.db.models.functions.datetime
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('issue66_shop', '0002_auto_enforcement'), ('issue66_anc', '0003_retable_hub')]

    operations = [migrations.SeparateDatabaseAndState(state_operations=[
        migrations.CreateModel(
            name='Hub',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('_deleted_at', models.DateTimeField(editable=False, null=True, verbose_name='Deleted at')),
                ('_created_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False, verbose_name='Created at')),
                ('_updated_at', models.DateTimeField(db_default=django.db.models.functions.datetime.Now(), editable=False, verbose_name='Updated at')),
                ('name', models.CharField(default='h', max_length=20)),
            ],
            options={'db_table': 'issue66_shop_hub',
                'abstract': False,
                'default_manager_name': 'objects',
                'indexes': [models.Index(condition=models.Q(('_deleted_at__isnull', True)), fields=['_deleted_at'], name='hub_deleted_at')],
            },
        )
    ])]
