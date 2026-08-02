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

# A third alias, same test database as 'default' (TEST.MIRROR skips creating a separate
# one) but with the psycopg connection-pool option on -- tests/test_concurrency.py uses
# this to exercise guitars' tenant-scope/GUC-cache correctness under Django 5.1+'s
# `OPTIONS: {"pool": True}`, where each checkout is a distinct psycopg connection object
# rather than one long-lived connection. Defined statically (not built at test runtime)
# because Django validates `django_db(databases=[...])` against settings.DATABASES at
# class setup, before any test body runs.
DATABASES['pooled'] = {  # noqa: F405
    **DATABASES['default'],  # noqa: F405
    'OPTIONS': {**DATABASES['default'].get('OPTIONS', {}), 'pool': True},  # noqa: F405
    'TEST': {'MIRROR': 'default'},
}

# 'tests.legacy_migrations' is installed (so pytest-django migrates it like any other app,
# giving tests/test_legacy_migrations.py an already-migrated "legacy" database for free at
# session setup) but deliberately left out of LOCAL_APPS -- makeguitarmigrations/
# audittenancy ignore it by default, and that test scopes to it explicitly by app label.
INSTALLED_APPS = [*INSTALLED_APPS, 'tests.testapp', 'tests.legacy_migrations']  # noqa: F405

LOCAL_APPS = ['tests.testapp']
TRIGGER_FUNCTION_APP = 'tests.testapp'

# Tenancy. The field name is deliberately non-default -- see tests/testapp/models.py.
GUITARS_TENANT_MODEL = 'testapp.Label'
GUITARS_TENANT_FIELD = 'label'
