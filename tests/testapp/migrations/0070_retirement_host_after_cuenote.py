"""Empty on purpose, as ``0067_retirement_host_after_ledger`` was: the newest node, after the
newest generated enforcement migration, for the retirement-ordering tests to pretend a
``RetireEnforcement`` in."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0069_auto_enforcement'),
    ]

    operations = []
