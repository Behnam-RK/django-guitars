"""Schema only, no enforcement -- see tests/test_mti_incremental.py.

Adds ``Descendant``, an MTI child of ``Ancestor``, one commit after
``0002_auto_enforcement.py`` -- which is already fully current (real, current-generator
output, not hand-written). Nothing here creates the child's own trigger/rule; that is the
point.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('mti_incremental', '0002_auto_enforcement'),
    ]

    operations = [
        migrations.CreateModel(
            name='Descendant',
            fields=[
                (
                    'ancestor_ptr',
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to='mti_incremental.ancestor',
                    ),
                ),
                ('detail', models.CharField(max_length=100)),
            ],
            bases=('mti_incremental.ancestor',),
        ),
    ]
