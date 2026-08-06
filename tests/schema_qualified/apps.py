from django.apps import AppConfig


class SchemaQualifiedConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tests.schema_qualified'
