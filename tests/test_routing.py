"""The router gate: whether this kit's PostgreSQL enforcement applies to a model. Every test
here is registry-and-settings only -- the gate must never open a connection, which
`test_no_router_consults_no_connection` asserts by making every connection access raise."""

from io import StringIO

import pytest
from django.core.management import call_command
from django.test import override_settings

from guitars import routing
from tests.crossapp_owner.models import Owner
from tests.testapp.models import Band, Release


class _ToNonPg:
    """Sends `testapp` to the sqlite alias and nothing else."""

    def allow_migrate(self, db, app_label, **hints):
        if app_label == 'testapp':
            return db == 'nonpg'
        return None


class _ToNonPgModel:
    """Sends one model of `testapp` to the sqlite alias, reading the `model` hint that only
    `allow_migrate_model` supplies."""

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if hints.get('model') is Band:
            return db == 'nonpg'
        return None


class _ToBoth:
    """Sends `testapp` to both the PostgreSQL default and the sqlite alias."""

    def allow_migrate(self, db, app_label, **hints):
        if app_label == 'testapp':
            return db in {'default', 'nonpg'}
        return None


class _Nowhere:
    def allow_migrate(self, db, app_label, **hints):
        return False


class _ReplicaOnly:
    """A read-replica router: it routes queries and says nothing about migrations, which is
    the commonest router there is and must not read as moving a table anywhere."""

    def db_for_read(self, model, **hints):
        return 'secondary'

    def db_for_write(self, model, **hints):
        return 'secondary'


class _Exploding:
    def __getitem__(self, alias):
        raise AssertionError('the gate touched a connection')

    def __iter__(self):
        raise AssertionError('the gate iterated the connections')


def test_no_router_consults_no_connection(monkeypatch):
    """The overwhelmingly common case, and the reason the gate short-circuits on
    `DATABASE_ROUTERS`: `makeguitarmigrations` opens nothing today and must keep that."""
    monkeypatch.setattr(routing, 'connections', _Exploding())
    with override_settings(DATABASE_ROUTERS=[]):
        assert routing.migrates_to_postgresql(Band) is True


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_a_model_routed_only_off_postgresql_is_skipped():
    assert routing.migration_aliases(Band) == ['nonpg']
    assert routing.migrates_to_postgresql(Band) is False


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_an_unrouted_app_is_unaffected():
    """The router answers `None` for every other app, so Django falls through to allowing
    each alias -- `default` among them, which is PostgreSQL."""
    assert routing.migrates_to_postgresql(Owner) is True


@override_settings(DATABASE_ROUTERS=[_ToNonPgModel()])
def test_the_model_itself_is_forwarded_to_the_router():
    """`allow_migrate_model` passes `model=`, which `db_for_write` cannot, so a router
    discriminating one model of an app from the rest is answered as it asked."""
    assert routing.migrates_to_postgresql(Band) is False
    assert routing.migrates_to_postgresql(Release) is True


@override_settings(DATABASE_ROUTERS=[_ToBoth()])
def test_a_model_on_both_backends_still_earns_its_enforcement():
    """`any`, not `all`: withholding the rules would leave `.delete()` destroying rows on the
    PostgreSQL alias, which is the one direction this kit must never fail in."""
    assert routing.migrates_to_postgresql(Band) is True


@override_settings(DATABASE_ROUTERS=[_ReplicaOnly()])
def test_a_router_that_only_routes_queries_moves_no_tables():
    assert routing.migrates_to_postgresql(Band) is True


@override_settings(DATABASE_ROUTERS=[_Nowhere()])
def test_a_model_migrated_nowhere_is_skipped():
    assert routing.migration_aliases(Band) == []
    assert routing.migrates_to_postgresql(Band) is False


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_the_note_names_the_alias_and_its_vendor():
    note = routing.vendor_skip_note(Band)
    assert note.startswith(f"'{Band._meta.db_table}' skipped:")
    assert "'nonpg'" in note and 'sqlite' in note
    assert 'Python scoping still applies' not in note


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_the_note_promises_python_scoping_only_when_asked():
    assert routing.vendor_skip_note(Band, python_scoping=True).endswith(
        'Python scoping still applies.'
    )


@override_settings(DATABASE_ROUTERS=[_Nowhere()])
def test_the_note_survives_a_model_routed_nowhere():
    """A model no alias takes has no vendor to name, and the note still has to render --
    it is printed inside a report that walks every app."""
    assert 'no configured alias' in routing.vendor_skip_note(Band)


@pytest.mark.parametrize('model', [Band, Release])
def test_the_gate_is_a_no_op_with_the_suite_s_own_settings(model):
    assert routing.migrates_to_postgresql(model) is True


# --- The generator under a routing-away router. Here rather than in test_command.py so the
# --- routers above are declared once: the gate is one answer, read by both halves.


def _command():
    from guitars.management.enforcement.command import Command

    command = Command()
    command.existing.triggers.clear()
    command.existing.soft_deletes.clear()
    command.existing.soft_delete_related.clear()
    return command


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_a_routed_away_app_earns_no_operations():
    from django.apps import apps

    assert _command()._build_operations(apps.get_app_config('testapp')) == []


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_a_routed_away_model_is_named_once_on_stdout():
    """Tenancy discovery and `_build_operations` both reach a tenanted model, and the note
    is byte-identical, so the report dedupes it rather than printing two findings."""
    out, err = StringIO(), StringIO()

    call_command('makeguitarmigrations', 'testapp', stdout=out, stderr=err)

    printed = out.getvalue()
    note = routing.vendor_skip_note(Release, python_scoping=True)
    assert printed.count(note) == 1
    assert note in printed


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_check_is_green_for_a_routed_away_app():
    """Not owed, rather than owed and missing: nothing reaches `check_missing`, so `--check`
    does not raise -- and it must not, or the reporter's CI can never go green."""
    out, err = StringIO(), StringIO()

    call_command('makeguitarmigrations', 'testapp', '--check', stdout=out, stderr=err)

    assert 'Missing or outdated enforcement migrations' not in err.getvalue()


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_recorded_coverage_for_a_routed_away_app_is_not_retired_or_advised_away():
    """The file on disk stays a fact. Nothing DROPs -- `_table_app_labels` maps the table
    nowhere, and retirement drops only on positive evidence -- and no note advises dropping
    it by hand either, the vendor note having already named the model."""
    command = _command()
    assert command._unmapped_cascade_notes() == []
    assert command._unmapped_autofill_notes() == []
    assert command._routed_away_tables()
    assert command._table_app_labels() == {}


@override_settings(DATABASE_ROUTERS=[_ToNonPgModel()])
def test_a_routed_away_cascade_child_loses_only_its_own_rule():
    """Both ends are gated, so the child's cascade rule goes -- while the owner keeps every
    operation of its own, which is what `any()` over the aliases is there to protect."""
    from django.apps import apps

    ops = '\n'.join(_command()._build_operations(apps.get_app_config('testapp')))

    assert f'ON "{Band._meta.db_table}"' not in ops
    assert f'"{Release._meta.db_table}"' in ops


@override_settings(DATABASE_ROUTERS=[_ToNonPgModel()])
def test_the_generator_and_hard_delete_agree_on_a_routed_away_relation():
    """The 2.4.1 shape: `owner_arms` is read by the generator *and* by `hard_delete()`, so a
    relation the generator refuses must yield no arm -- else Python follows what no rule
    covers and destroys what the refusal spared."""
    from django.apps import apps

    from guitars.introspection import owner_arms

    arms = owner_arms(apps.get_models())
    assert Band._meta.db_table not in arms
    for found in arms.values():
        assert all(arm.owner_table != Band._meta.db_table for arm in found)
