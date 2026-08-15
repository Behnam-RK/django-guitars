"""M4's schema-qualified ``db_table`` support, against a real non-``public`` schema.
``tests.schema_qualified`` is added to ``INSTALLED_APPS`` only per-test: permanently
installed, its quoted ``db_table`` breaks Django's flush-teardown for every other test."""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
from django.db import connection
from django.test import override_settings

from guitars import sql
from guitars.sql import _identifiers
from guitars.sql import triggers as _triggers
from guitars.tenancy import tenancy_bypassed, tenant
from tests.testapp.models import Label


pytestmark = pytest.mark.django_db


@pytest.fixture
def event_model():
    """``tests.schema_qualified.models.Event``, installed only for one test's duration.
    Imported lazily inside the ``override_settings`` block: at collection time the app
    isn't installed yet, and a model class needs its app registered before it's defined."""
    with override_settings(
        INSTALLED_APPS=[*django_settings.INSTALLED_APPS, 'tests.schema_qualified']
    ):
        from tests.schema_qualified.models import Event

        yield Event


@pytest.fixture
def analytics_events_table(event_model):
    """Create ``"analytics"."events"`` and its enforcement SQL in the current transaction.
    ``SET LOCAL search_path`` matters: ``set_updated_at()`` updates the bare
    ``TG_TABLE_NAME``, so ``search_path`` must include the table's schema -- see ``docs/mti.md``."""
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
    """RLS, the updated-at trigger, and the soft-delete rule against a table whose
    ``db_table`` names a non-``public`` schema -- the M4 scenario nothing else exercises."""

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
        """Proves the trigger fired (a non-null timestamp lands), not that time passed --
        ``NOW()`` is transaction-start time, so a strictly-later value can never be observed
        within one transaction; see the cross-transaction test below for that proof."""
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
            # Model.delete() resets the instance's pk to None on success -- captured before.
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
    """Needs a real wall-clock gap: ``NOW()`` never changes within one transaction, so this
    needs two committed transactions. Manual ``finally`` teardown, run before pytest-django's
    flush, avoids the module docstring's flush breakage."""
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


def test_mti_parent_trigger_fires_the_schema_qualified_branch(analytics_events_table):
    """The one runtime path nothing else fires: ``set_parent_updated_at()``'s 4-arg,
    non-empty-``parent_schema`` branch. Not a real MTI subclass: that would permanently
    register a parent-link on ``Event._meta``, breaking other tests' cascade deletes."""
    table = analytics_events_table._meta.db_table
    # The fixture also enables RLS on this table; bypassed since a raw cursor INSERT is
    # not exempted by tenant() the way an ORM call is, but label_id is still NOT NULL.
    with tenancy_bypassed():
        label = Label.objects.create(name='Analytics Co')
    with tenancy_bypassed(), connection.cursor() as cursor:
        cursor.execute(
            'INSERT INTO "analytics"."events" (name, _updated_at, label_id) '
            "VALUES ('probe', '2000-01-01T00:00:00Z', %s) RETURNING id, _updated_at",
            [label.pk],
        )
        event_id, before = cursor.fetchone()

        cursor.execute('CREATE TABLE mti_child_probe (id integer PRIMARY KEY, venue text)')
        cursor.execute(
            'INSERT INTO mti_child_probe (id, venue) VALUES (%s, %s)', [event_id, 'hall']
        )

        parent_schema, parent_table = _identifiers._split_qualified('table', table)
        cursor.execute(
            _triggers._CREATE_PARENT_UPDATED_AT_TRIGGER.format(
                child_table=_identifiers._quote_table('mti_child_probe'),
                parent_schema=_identifiers._escape_literal(parent_schema or ''),
                parent_table=_identifiers._escape_literal(parent_table),
                parent_pk=_identifiers._escape_literal('id'),
                child_pk=_identifiers._escape_literal('id'),
            )
        )

        # Child-only write: only mti_child_probe is touched, never "analytics"."events" --
        # exactly the write set_parent_updated_at() exists to propagate.
        cursor.execute(
            'UPDATE mti_child_probe SET venue = %s WHERE id = %s', ['loud hall', event_id]
        )

        cursor.execute('SELECT _updated_at FROM "analytics"."events" WHERE id = %s', [event_id])
        (after,) = cursor.fetchone()

    assert after is not None
    assert after != before
