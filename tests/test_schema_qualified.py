"""End-to-end coverage for M4's schema-qualified ``db_table`` support, against a real,
non-``public`` PostgreSQL schema.

``tests.schema_qualified`` is not in ``INSTALLED_APPS`` in ``tests/settings.py`` -- it is
added only for the duration of a test, via ``override_settings``, the same pattern
``tests/test_makemigrations_override.py`` uses for its own throwaway app. That matters
here for a sharper reason than tidiness: Django's own flush (run by every
``@pytest.mark.django_db(transaction=True)`` test's teardown) truncates tables by
cross-referencing ``model._meta.db_table`` against ``connection.introspection.
table_names()`` -- and that comparison never matches a quoted, schema-qualified
``db_table`` like ``'"analytics"."events"'``, so the table is silently excluded from the
truncate list. A table excluded that way, but still physically present with a foreign key
into ``testapp_label``, makes flush() fail for *every* ``transaction=True`` test in the
whole suite the moment ``testapp_label`` is truncated first -- not just tests that touch
this app. Confirmed by hand while building this file: adding the app (and its migrations)
to ``INSTALLED_APPS`` permanently broke three unrelated, pre-existing tests in
``test_base.py`` with ``psycopg.errors.FeatureNotSupported: cannot truncate a table
referenced in a foreign key constraint``.

The fix is to never let the table persist past one test's transaction. Every test below
uses the *default*, non-``transaction`` ``db`` fixture (savepoint-rollback teardown, no
flush involved at all) and creates the schema, table, trigger, rule and policy by hand
inside that same transaction -- schema editor and raw SQL alike -- so a rollback erases
all of it as cleanly as it erases a row insert. Nothing here ever reaches ``migrate`` or a
checked-in migration file.
"""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
from django.db import connection
from django.test import override_settings

from guitars import sql
from guitars.sql import _identifiers
from guitars.tenancy import tenancy_bypassed, tenant
from tests.testapp.models import Label


pytestmark = pytest.mark.django_db


@pytest.fixture
def event_model():
    """``tests.schema_qualified.models.Event`` (``db_table = '"analytics"."events"'``),
    installed only for the duration of one test.

    Imported lazily, inside the ``override_settings`` block: at module-collection time
    ``tests.schema_qualified`` is not yet an installed app, and a model class needs its
    app registered *before* it can be defined -- see the module docstring.
    """
    with override_settings(
        INSTALLED_APPS=[*django_settings.INSTALLED_APPS, 'tests.schema_qualified']
    ):
        from tests.schema_qualified.models import Event

        yield Event


@pytest.fixture
def analytics_events_table(event_model):
    """Physically create ``"analytics"."events"`` (schema, table, and every enforcement
    statement the generator would write for it) inside the current test's transaction.

    Uses ``connection.schema_editor()`` for the table itself -- not hand-written column
    DDL -- so the columns/FK/index genuinely match what ``Event`` declares, the same
    guarantee a real migration would give. The enforcement SQL is generated the same way
    ``operations.py`` does: through ``_identifiers._quote_table`` and the public ``sql``
    functions, not reimplemented here.

    ``SET LOCAL search_path`` is not decoration. ``set_updated_at()`` (the shared, singleton
    trigger function every *own-table* -- non-MTI -- ``_updated_at`` trigger calls) updates
    ``TG_TABLE_NAME`` rather than a schema-qualified name, because ``TG_TABLE_NAME`` is
    always the bare relation name -- Postgres has no equivalent that returns it
    pre-qualified. That dynamic ``UPDATE`` is therefore resolved through the *firing
    session's* ``search_path`` like any other unqualified reference, exactly as it always
    was before schema-qualified ``db_table`` existed; this milestone makes that pre-existing
    assumption newly *visible*, not newly true. A project putting a table outside the
    session's default ``search_path`` (``"$user", public``) must include that schema in it --
    the same operational requirement any schema-per-tenant Postgres deployment already has
    for unqualified references. ``LOCAL`` scopes the change to this transaction, so it
    reverts along with everything else in it on rollback, the same as ``rules.hard_deletion``
    elsewhere in this kit. See ``docs/mti.md``.
    """
    table = event_model._meta.db_table
    assert table == '"analytics"."events"'
    qualified = _identifiers._quote_table(table)

    with connection.cursor() as cursor:
        cursor.execute('CREATE SCHEMA analytics')
        cursor.execute('SET LOCAL search_path TO analytics, public')

    with connection.schema_editor() as schema_editor:
        schema_editor.create_model(event_model)

    with connection.cursor() as cursor:
        cursor.execute(
            sql.CREATE_UPDATED_AT_TRIGGER.format(
                table=qualified, primary_key=_identifiers._escape_literal('id')
            )
        )
        cursor.execute(
            sql.CREATE_SOFT_DELETE_RULE.format(
                table=qualified, primary_key=_identifiers._escape_ident('id')
            )
        )
        for statement in sql.create_table_rls(table=table, columns={'label': 'label_id'}):
            cursor.execute(statement)

    return event_model


class TestSchemaQualifiedTable:
    """RLS, the updated-at trigger and the soft-delete rule, all against a table whose
    ``db_table`` names a schema other than ``public`` -- the M4 scenario nothing else in
    the suite exercises.
    """

    def test_a_negative_control_confirms_the_table_lives_outside_public(
        self, analytics_events_table
    ):
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT n.nspname FROM pg_class c '
                'JOIN pg_namespace n ON n.oid = c.relnamespace '
                "WHERE c.relname = 'events' AND c.relkind = 'r'"
            )
            schemas = {row[0] for row in cursor.fetchall()}
        assert schemas == {'analytics'}

    def test_updated_at_trigger_fires_and_stamps_a_real_timestamp(self, analytics_events_table):
        """Proves the trigger *fired* (a non-null, DB-computed timestamp lands on the row)
        rather than that time passed -- ``_updated_at = NOW()`` is transaction-*start* time
        in PostgreSQL, not wall-clock time, so within this fixture's single savepoint-backed
        transaction a strictly-later value can never be observed even when the trigger runs
        correctly. See ``test_updated_at_trigger_genuinely_advances_across_committed_statements``
        below for the real, cross-transaction advancement proof that needs.
        """
        with tenancy_bypassed():
            label = Label.objects.create(name='Analytics Co')
        with tenant(label=label):
            event = analytics_events_table.objects.create(name='launch')
            event.name = 'renamed'
            event.save(update_fields=['name'])
            event.refresh_from_db()
        assert event._updated_at is not None

    def test_delete_soft_deletes_rather_than_removing_the_row(self, analytics_events_table):
        with tenancy_bypassed():
            label = Label.objects.create(name='Analytics Co')
        with tenant(label=label):
            event = analytics_events_table.objects.create(name='launch')
            pk = event.pk
            event.delete()
            # Django's own Model.delete() sets the in-memory instance's pk to None once the
            # statement succeeds -- captured above, before the call, for that reason.
            assert event.pk is None
            assert not analytics_events_table.objects.filter(pk=pk).exists()
            archived = analytics_events_table._all_objects.get(pk=pk)
        assert archived.is_deleted

    def test_tenant_rls_policy_hides_another_tenants_row(self, analytics_events_table):
        with tenancy_bypassed():
            label_a = Label.objects.create(name='Tenant A')
            label_b = Label.objects.create(name='Tenant B')
        with tenant(label=label_a):
            analytics_events_table.objects.create(name='a-only')
        with tenant(label=label_b):
            assert list(analytics_events_table.objects.values_list('name', flat=True)) == []
        with tenant(label=label_a):
            assert list(analytics_events_table.objects.values_list('name', flat=True)) == [
                'a-only'
            ]


@pytest.mark.django_db(transaction=True)
def test_updated_at_trigger_genuinely_advances_across_committed_statements():
    """The one test in this file that needs a real, wall-clock time gap -- ``NOW()`` inside
    a single transaction never changes, so proving the trigger *advances* ``_updated_at``
    (not just sets it) needs two genuinely separate, committed transactions, which only
    ``transaction=True`` provides.

    Manual setup and teardown, not the ``analytics_events_table``/``event_model`` fixtures:
    with ``transaction=True`` nothing is rolled back automatically, so the schema and table
    must be dropped by hand -- and dropped *before* pytest-django's own flush-based
    teardown runs, or this reproduces the whole-suite flush breakage described in the
    module docstring. Doing it inside the test body's own ``finally``, rather than in a
    fixture, is what guarantees that ordering: fixture teardown only runs after the test
    function returns.
    """
    with override_settings(
        INSTALLED_APPS=[*django_settings.INSTALLED_APPS, 'tests.schema_qualified']
    ):
        from tests.schema_qualified.models import Event

        table = Event._meta.db_table
        qualified = _identifiers._quote_table(table)
        try:
            with connection.cursor() as cursor:
                cursor.execute('CREATE SCHEMA analytics')
                # Not LOCAL here: transaction=True means each statement below is its own,
                # separately-committed transaction, so a transaction-scoped SET would not
                # survive past the statement that created the schema. Reset in `finally`.
                cursor.execute('SET search_path TO analytics, public')
            with connection.schema_editor() as schema_editor:
                schema_editor.create_model(Event)
            with connection.cursor() as cursor:
                cursor.execute(
                    sql.CREATE_UPDATED_AT_TRIGGER.format(
                        table=qualified, primary_key=_identifiers._escape_literal('id')
                    )
                )

            with tenancy_bypassed():
                label = Label.objects.create(name='Analytics Co')
            with tenant(label=label):
                event = Event.objects.create(name='launch')
                before = event._updated_at
                Event.objects.filter(pk=event.pk).update(name='renamed')
                event.refresh_from_db()
            assert event._updated_at > before
        finally:
            with connection.cursor() as cursor:
                cursor.execute('RESET search_path')
                cursor.execute('DROP SCHEMA IF EXISTS analytics CASCADE')
