from django.apps import AppConfig


class GuitarsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'guitars'
    verbose_name = 'Guitars'

    def ready(self) -> None:
        """Activate tenancy enforcement.

        One of two entry points -- ``tenanted_manager()`` calls the same idempotent
        ``install()`` at model-definition time, which is what covers projects using
        guitars as a pure library with no ``INSTALLED_APPS`` entry. Neither alone is
        sufficient: this hook does not run in that configuration, and the manager hook
        does not run for a project that installs the app but declares no tenanted model
        yet (where installing early is still correct, so ``manage.py check`` validates the
        settings).

        Deferred import: ``ready()`` runs after Django's own setup, and importing the
        tenancy package touches ``django.db``.
        """
        from guitars import tenancy  # noqa: PLC0415 - must run after Django setup

        tenancy.install()
