from django.apps import AppConfig


class MakemigrationsOverrideConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tests.makemigrations_override'
