"""Test settings: the dev harness (core.settings) plus the concrete test app.

The kit ships only abstract models, so the suite defines real models in
``tests.testapp`` and points the advanced-migrations command at it via
``LOCAL_APPS`` / ``TRIGGER_FUNCTION_APP`` (so generated migrations land under
``tests/``, never inside the shipped ``guitars`` package).
"""

from core.settings import *  # noqa: F401, F403


INSTALLED_APPS = [*INSTALLED_APPS, 'tests.testapp']  # noqa: F405

LOCAL_APPS = ['tests.testapp']
TRIGGER_FUNCTION_APP = 'tests.testapp'
