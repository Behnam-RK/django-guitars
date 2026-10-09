"""Empty on purpose, as ``0084_retirement_host_after_stamp`` was: the newest node, after the
newest generated enforcement migration, for the retirement-ordering tests to pretend a
``RetireEnforcement`` in."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0085_auto_enforcement'),
    ]

    operations = []
