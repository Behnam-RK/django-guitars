"""Test settings: the dev harness (core.settings) plus the concrete test app.

The kit ships only abstract models, so the suite defines real models in
``tests.testapp`` and points the enforcement-migrations command at it via
``LOCAL_APPS`` / ``TRIGGER_FUNCTION_APP`` (so generated migrations land under
``tests/``, never inside the shipped ``guitars`` package).
"""

from core.settings import *  # noqa: F401, F403


# A second alias, same server, different database -- so `connections[self.db]` in
# `hard_delete()` (guitars/models/soft_deletion.py) has a real second connection to
# thread through instead of silently resolving to `connections['default']` regardless
# of what `self.db` names. Same connection params as 'default'; pytest-django creates
# and migrates 'test_guitars_secondary' the same way it does the default test database.
DATABASES['secondary'] = {**DATABASES['default'], 'NAME': 'guitars_secondary'}  # noqa: F405

INSTALLED_APPS = [*INSTALLED_APPS, 'tests.testapp']  # noqa: F405

LOCAL_APPS = ['tests.testapp']
TRIGGER_FUNCTION_APP = 'tests.testapp'

# Tenancy. The field name is deliberately non-default -- see tests/testapp/models.py.
GUITARS_TENANT_MODEL = 'testapp.Label'
GUITARS_TENANT_FIELD = 'label'
