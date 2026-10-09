"""Empty on purpose, as ``0094_retirement_host_after_self_arms`` was: the newest node, after the
newest generated enforcement migration, for the retirement-ordering tests to pretend a
``RetireEnforcement`` in."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0097_auto_enforcement'),
    ]

    operations = []
