"""Startup validation of the tenancy settings.

``GUITARS_TENANT_ENFORCE`` is read at *write* time, so without a system check a typo would
surface as a ``ValueError`` from inside the first write that happened to trip a guard --
on a request, long after deploy, nowhere near the setting. ``GUITARS_TENANT_AUTOFILL`` is
worse: it is read through ``bool()``, so the string ``'False'`` would silently enable
autofill and start assigning tenants to rows.
"""

import pytest
from django.test import override_settings

from guitars.tenancy import TenantEnforcement
from guitars.tenancy.checks import AUTOFILL_ID, ENFORCE_ID, check_tenancy_settings


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
    for check_id in (ENFORCE_ID, AUTOFILL_ID):
        assert check_id.startswith('guitars.tenancy.')


def test_the_check_is_registered_with_django():
    """Registered, not merely defined -- otherwise ``manage.py check`` never runs it."""
    from django.core.checks import registry

    from guitars.tenancy import register_checks

    register_checks()

    assert check_tenancy_settings in registry.registry.registered_checks
