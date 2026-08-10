"""Direct tests for guitars.local_apps -- the leaf module extracted from two duplicated
``is_local`` copies (M5, #12) in ``guitars.tenancy.discovery`` and
``guitars.management._generator``. Both re-export it (``from guitars.local_apps import
is_local``) so existing call sites and imports are unaffected; this file is about the
extracted function itself, not its consumers -- those stay covered by
test_tenancy_discovery.py and the management-command tests.
"""

from django.apps import apps as django_apps

from guitars.local_apps import is_local
from guitars.management._generator import is_local as generator_is_local
from guitars.tenancy.discovery import is_local as discovery_is_local


def test_testapp_is_local():
    """``tests.testapp`` is in ``settings.LOCAL_APPS`` (see tests/settings.py)."""
    assert is_local(django_apps.get_app_config('testapp'))


def test_guitars_itself_is_not_local():
    """The shipped package is never in a consuming project's ``LOCAL_APPS``."""
    assert not is_local(django_apps.get_app_config('guitars'))


def test_discovery_and_generator_both_re_export_the_same_function():
    """Not two functions that happen to agree -- one function, two import paths."""
    assert discovery_is_local is is_local
    assert generator_is_local is is_local
