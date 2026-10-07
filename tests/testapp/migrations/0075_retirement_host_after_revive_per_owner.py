"""Empty on purpose, as ``0073_retirement_host_after_rig`` was: the newest node, after the
newest generated enforcement migration, for the retirement-ordering tests to pretend a
``RetireEnforcement`` in."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0074_auto_enforcement'),
    ]

    operations = []
