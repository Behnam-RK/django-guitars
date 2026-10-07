"""Empty on purpose, as ``0075_retirement_host_after_revive_per_owner`` was: the newest node,
after the newest generated enforcement migration, for the retirement-ordering tests to pretend a
``RetireEnforcement`` in."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0077_auto_enforcement'),
    ]

    operations = []
