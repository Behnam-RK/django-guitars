"""Empty on purpose, as ``0064_retirement_host`` was: the newest node, after the newest generated
enforcement migration, for the retirement-ordering tests to pretend a ``RetireEnforcement`` in."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0066_auto_enforcement'),
    ]

    operations = []
