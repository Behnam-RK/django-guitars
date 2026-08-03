from django.apps import AppConfig


class LegacyMigrationsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tests.legacy_migrations'
