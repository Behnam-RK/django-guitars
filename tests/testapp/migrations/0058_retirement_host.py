"""Empty on purpose: a node *after* the newest generated enforcement migration, so the
retirement-ordering tests have somewhere to pretend a ``RetireEnforcement`` lives. A file that
both creates and retires one key is refused as its own evidence, so the two cannot share one."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0057_auto_enforcement'),
    ]

    operations = []
