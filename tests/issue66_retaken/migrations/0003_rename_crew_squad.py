from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('issue66_retaken', '0002_auto_enforcement')]

    operations = [migrations.RenameModel('Crew', 'Squad')]
