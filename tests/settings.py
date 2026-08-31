"""Test settings: the dev harness (core.settings) plus the concrete test app. The kit
ships only abstract models, so the suite defines real ones in ``tests.testapp`` and
points the enforcement-migrations command at it via ``LOCAL_APPS``/``TRIGGER_FUNCTION_APP``."""

from core.settings import *  # noqa: F401, F403


# A second alias, same server, different database -- so `connections[self.db]` in
# `hard_delete()` has a real second connection to thread through instead of silently
# resolving to `connections['default']` regardless of what `self.db` names.
DATABASES['secondary'] = {**DATABASES['default'], 'NAME': 'guitars_secondary'}  # noqa: F405

# A third alias mirroring 'default' (TEST.MIRROR) but with the psycopg connection-pool
# option on -- test_concurrency.py exercises the GUC cache under Django 5.1+'s pooled
# checkouts. Defined statically since Django validates `databases=[...]` at class setup.
DATABASES['pooled'] = {  # noqa: F405
    **DATABASES['default'],  # noqa: F405
    'OPTIONS': {**DATABASES['default'].get('OPTIONS', {}), 'pool': True},  # noqa: F405
    'TEST': {'MIRROR': 'default'},
}

# 'legacy_migrations'/'mti_incremental'/'crossapp_*' are installed but out of LOCAL_APPS -- each
# test scopes explicitly; the trio co-owns one dependent across apps, the crossapp_tenant pair an
# MTI chain tenanted one app up. 'makemigrations_override'/'schema_qualified' self-install instead.
INSTALLED_APPS = [  # noqa: F405
    *INSTALLED_APPS,  # noqa: F405
    # Harness-only, and deliberately not in core/settings.py or the shipped wheel: it exists
    # so 'testapp' can declare a GenericRelation, the one referring shape carrying no column.
    'django.contrib.contenttypes',
    'tests.testapp',
    'tests.legacy_migrations',
    'tests.mti_incremental',
    'tests.crossapp_dependent',
    'tests.crossapp_owner',
    'tests.crossapp_third',
    'tests.crossapp_tenant_ancestor',
    'tests.crossapp_tenant_child',
]

LOCAL_APPS = ['tests.testapp']
TRIGGER_FUNCTION_APP = 'tests.testapp'

# Tenancy. The field name is deliberately non-default -- see tests/testapp/models.py.
GUITARS_TENANT_MODEL = 'testapp.Label'
GUITARS_TENANT_FIELD = 'label'
