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
    check_migrate_bypasses_tenancy,
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

    from guitars.tenancy import register_checks

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
