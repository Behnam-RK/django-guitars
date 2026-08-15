from django.apps import AppConfig


class GuitarsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'guitars'
    verbose_name = 'Guitars'

    def ready(self) -> None:
        """Activate tenancy enforcement. One of two entry points -- ``tenanted_manager()``
        calls the same idempotent ``install()`` at model-definition time, covering a pure
        library with no ``INSTALLED_APPS`` entry. Deferred import: touches ``django.db``."""
        from guitars import tenancy  # noqa: PLC0415 - must run after Django setup

        tenancy.install()
