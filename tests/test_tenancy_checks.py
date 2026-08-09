"""Startup validation of the tenancy settings.

``GUITARS_TENANT_ENFORCE`` is read at *write* time, so without a system check a typo would
surface as a ``ValueError`` from inside the first write that happened to trip a guard --
on a request, long after deploy, nowhere near the setting. ``GUITARS_TENANT_AUTOFILL`` is
worse: it is read through ``bool()``, so the string ``'False'`` would silently enable
autofill and start assigning tenants to rows.
"""

import pytest
from django.apps import apps as django_apps
from django.core.management.commands.migrate import Command as DjangoMigrate
from django.test import override_settings

from guitars.tenancy import TenantEnforcement
from guitars.tenancy.checks import (
    AUTOFILL_ID,
    ENFORCE_ID,
    MIGRATE_OVERRIDE_ID,
    POOLING_ID,
    check_migrate_bypasses_tenancy,
    check_pooling_leaks_tenant_gucs,
    check_tenancy_settings,
)


def _ids(errors):
    return {error.id for error in errors}


def test_defaults_pass_with_nothing_configured():
    """A project that never sets either setting must not fail its own checks."""
    assert check_tenancy_settings(None) == []


@pytest.mark.parametrize('mode', ['strict', 'audit'])
def test_every_valid_mode_string_passes(mode):
    with override_settings(GUITARS_TENANT_ENFORCE=mode):
        assert check_tenancy_settings(None) == []


@pytest.mark.parametrize('mode', list(TenantEnforcement))
def test_passing_the_enum_itself_passes(mode):
    """Relies on the StrEnum-parity ``__str__`` on Python 3.10.

    The check compares ``str(configured)`` against the allowed values. On a plain
    ``(str, Enum)`` without that override, ``str()`` yields 'TenantEnforcement.STRICT' and
    a project handing over the enum would fail its own check.
    """
    with override_settings(GUITARS_TENANT_ENFORCE=mode):
        assert check_tenancy_settings(None) == []


def test_a_misspelled_mode_is_an_error():
    with override_settings(GUITARS_TENANT_ENFORCE='strcit'):
        errors = check_tenancy_settings(None)

    assert _ids(errors) == {ENFORCE_ID}
    assert 'strcit' in errors[0].msg
    assert "['strict', 'audit']" in errors[0].hint


def test_an_unknown_mode_of_the_right_shape_is_still_an_error():
    with override_settings(GUITARS_TENANT_ENFORCE='warn'):
        assert _ids(check_tenancy_settings(None)) == {ENFORCE_ID}


@pytest.mark.parametrize('value', [True, False])
def test_a_bool_autofill_passes(value):
    with override_settings(GUITARS_TENANT_AUTOFILL=value):
        assert check_tenancy_settings(None) == []


@pytest.mark.parametrize(
    'value',
    [
        pytest.param('False', id='the-dangerous-one'),
        pytest.param('True', id='string-true'),
        pytest.param(0, id='int-zero'),
        pytest.param(1, id='int-one'),
        pytest.param(None, id='none'),
    ],
)
def test_a_non_bool_autofill_is_an_error(value):
    """``'False'`` is the case that motivates the check: non-empty, so ``bool()`` is True."""
    with override_settings(GUITARS_TENANT_AUTOFILL=value):
        errors = check_tenancy_settings(None)

    assert _ids(errors) == {AUTOFILL_ID}
    assert "'False' would read as True" in errors[0].hint


def test_both_settings_can_fail_at_once():
    """Report every problem in one run, rather than one per ``manage.py check`` cycle."""
    with override_settings(GUITARS_TENANT_ENFORCE='nope', GUITARS_TENANT_AUTOFILL='False'):
        assert _ids(check_tenancy_settings(None)) == {ENFORCE_ID, AUTOFILL_ID}


def test_check_ids_are_namespaced_so_they_cannot_collide():
    for check_id in (ENFORCE_ID, AUTOFILL_ID, MIGRATE_OVERRIDE_ID):
        assert check_id.startswith('guitars.tenancy.')


@pytest.mark.parametrize(
    'check', [check_tenancy_settings, check_migrate_bypasses_tenancy], ids=lambda c: c.__name__
)
def test_the_checks_are_registered_with_django(check):
    """Registered, not merely defined -- otherwise ``manage.py check`` never runs them."""
    from django.core.checks import registry

    from guitars.tenancy.testing import register_checks

    register_checks()

    assert check in registry.registry.registered_checks


# ─────────────────── W001: the migrate override must win ────────────────── #
#
# Nothing but INSTALLED_APPS order decides which `migrate` runs, and losing it is silent:
# a RunPython backfill is filtered by every tenant_scope policy, updates zero rows, and is
# marked applied anyway.


class TestMigrateOverrideCheck:
    def test_the_harness_configuration_passes(self):
        """`guitars` is first in INSTALLED_APPS here, so its override is the one that runs."""
        assert check_migrate_bypasses_tenancy(None) == []

    def test_a_shadowed_migrate_is_reported(self, monkeypatch):
        """Django's own command standing in for any app that ships a `migrate` of its own."""
        monkeypatch.setattr(
            'django.core.management.load_command_class', lambda *_: DjangoMigrate()
        )

        warnings = check_migrate_bypasses_tenancy(None)

        assert _ids(warnings) == {MIGRATE_OVERRIDE_ID}
        assert 'django.core.management.commands.migrate' in warnings[0].msg
        assert 'INSTALLED_APPS' in warnings[0].hint

    def test_a_subclass_of_the_override_still_passes(self, monkeypatch):
        """Resolved by type, not by app name -- wrapping the override is a supported thing."""
        from guitars.management.commands.migrate import Command as GuitarsMigrate

        class ProjectMigrate(GuitarsMigrate):
            pass

        monkeypatch.setattr(
            'django.core.management.load_command_class', lambda *_: ProjectMigrate()
        )

        assert check_migrate_bypasses_tenancy(None) == []

    def test_silent_when_the_database_layer_is_switched_off(self, monkeypatch):
        """No policies means no filtered backfill, so there is nothing to warn about."""
        monkeypatch.setattr(
            'django.core.management.load_command_class', lambda *_: DjangoMigrate()
        )

        with override_settings(GUITARS_TENANT_POLICIES=False):
            assert check_migrate_bypasses_tenancy(None) == []

    def test_silent_when_the_scoped_apps_have_no_tenanted_model(self, monkeypatch):
        """`manage.py check guitars` must not answer a question about `testapp`."""
        monkeypatch.setattr(
            'django.core.management.load_command_class', lambda *_: DjangoMigrate()
        )

        guitars_app = django_apps.get_app_config('guitars')

        assert check_migrate_bypasses_tenancy([guitars_app]) == []


class TestPoolingCheck:
    """The 'pooled' and 'secondary' aliases tests/settings.py adds specifically to prove
    Django's own psycopg pool and CONN_MAX_AGE are safe (test_concurrency.py) are exactly
    the configurations this check must stay silent about -- see
    check_pooling_leaks_tenant_gucs's own docstring for why those two signals were
    deliberately left out.

    ``DATABASES`` is monkeypatched directly rather than through ``override_settings``:
    Django's own ``complex_setting_changed`` receiver warns "Overriding setting DATABASES
    can lead to unexpected behavior" for exactly this setting, and this suite's
    ``filterwarnings = ["error"]`` turns that into a failure having nothing to do with
    what these tests check. A plain attribute patch changes what the check function reads
    without going through that signal at all.
    """

    def test_the_harness_configuration_passes(self):
        """Confirms `manage.py check` is clean today, `pooled`/`secondary` aliases and all."""
        assert check_pooling_leaks_tenant_gucs(None) == []

    def test_disable_server_side_cursors_is_reported(self, monkeypatch):
        from django.conf import settings as django_settings

        databases = {
            **django_settings.DATABASES,
            'default': {
                **django_settings.DATABASES['default'],
                'DISABLE_SERVER_SIDE_CURSORS': True,
            },
        }
        monkeypatch.setattr(django_settings, 'DATABASES', databases)

        warnings = check_pooling_leaks_tenant_gucs(None)

        assert _ids(warnings) == {POOLING_ID}
        assert 'default' in warnings[0].msg
        assert 'DISABLE_SERVER_SIDE_CURSORS' in warnings[0].msg
        assert 'DISCARD ALL' in warnings[0].hint

    def test_options_pool_alone_is_not_reported(self, monkeypatch):
        """Django's own psycopg connection pool -- proven safe by
        test_tenant_scope_is_correct_under_djangos_psycopg_pool -- must not trigger this
        on its own; it is what the 'pooled' harness alias already does."""
        from django.conf import settings as django_settings

        databases = {
            **django_settings.DATABASES,
            'default': {**django_settings.DATABASES['default'], 'OPTIONS': {'pool': True}},
        }
        monkeypatch.setattr(django_settings, 'DATABASES', databases)

        assert check_pooling_leaks_tenant_gucs(None) == []

    def test_conn_max_age_alone_is_not_reported(self, monkeypatch):
        """Django's own persistent-connection mechanism -- proven safe by the
        fingerprint/marker tests in test_concurrency.py -- must not trigger this alone."""
        from django.conf import settings as django_settings

        databases = {
            **django_settings.DATABASES,
            'default': {**django_settings.DATABASES['default'], 'CONN_MAX_AGE': 60},
        }
        monkeypatch.setattr(django_settings, 'DATABASES', databases)

        assert check_pooling_leaks_tenant_gucs(None) == []

    def test_silent_when_the_database_layer_is_switched_off(self, monkeypatch):
        from django.conf import settings as django_settings

        databases = {
            **django_settings.DATABASES,
            'default': {
                **django_settings.DATABASES['default'],
                'DISABLE_SERVER_SIDE_CURSORS': True,
            },
        }
        monkeypatch.setattr(django_settings, 'DATABASES', databases)

        with override_settings(GUITARS_TENANT_POLICIES=False):
            assert check_pooling_leaks_tenant_gucs(None) == []

    def test_silent_when_the_scoped_apps_have_no_tenanted_model(self, monkeypatch):
        from django.conf import settings as django_settings

        databases = {
            **django_settings.DATABASES,
            'default': {
                **django_settings.DATABASES['default'],
                'DISABLE_SERVER_SIDE_CURSORS': True,
            },
        }
        monkeypatch.setattr(django_settings, 'DATABASES', databases)
        guitars_app = django_apps.get_app_config('guitars')

        assert check_pooling_leaks_tenant_gucs([guitars_app]) == []

    def test_multiple_flagged_aliases_are_all_named(self, monkeypatch):
        from django.conf import settings as django_settings

        databases = {
            alias: {**config, 'DISABLE_SERVER_SIDE_CURSORS': True}
            for alias, config in django_settings.DATABASES.items()
        }
        monkeypatch.setattr(django_settings, 'DATABASES', databases)

        warnings = check_pooling_leaks_tenant_gucs(None)

        assert _ids(warnings) == {POOLING_ID}
        assert 'have' in warnings[0].msg  # plural wording when more than one alias flagged
        for alias in databases:
            assert alias in warnings[0].msg
