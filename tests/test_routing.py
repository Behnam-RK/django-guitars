"""The router gate: whether this kit's PostgreSQL enforcement applies to a model. Every test
here is registry-and-settings only -- the gate must never open a connection, which
`test_no_router_consults_no_connection` asserts by making every connection access raise."""

import types
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import CommandError, call_command
from django.test import override_settings

from guitars import routing
from tests.conftest import clear_cascade_coverage
from tests.crossapp_owner.models import Owner
from tests.testapp.models import Album, Band, Release


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


class _AlbumToNonPg:
    """Routes the model that *declares* the cascade key away, leaving its target on
    PostgreSQL -- the only arrangement that isolates the model gate from the target gate."""

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if hints.get('model') is Album:
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
    # The Python half is a clause of the one note, not a per-caller suffix -- see the
    # dedupe test below for what having two spellings of one skip cost.
    assert note.endswith('Python scoping still applies where a tenanted manager declares it.')


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_the_note_is_one_string_per_model_whoever_asks():
    """Both the generator and tenancy discovery reach a tenanted model, and the report dedupes
    on equality -- so a per-caller suffix printed the same skip twice, differing by a sentence."""
    assert routing.vendor_skip_note(Band) == routing.vendor_skip_note(Band)
    assert 'Python scoping still applies' in routing.vendor_skip_note(Band)


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
    clear_cascade_coverage(command)
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
    note = routing.vendor_skip_note(Release)
    assert note in printed
    # The count is the whole assertion, and it has to be taken over the string the callers
    # actually append: counting a *longer* string no printed line can contain would report 1
    # whether the dedupe fired, misfired, or was deleted outright.
    assert printed.count(note) == 1
    # And no second line about the same table under any other spelling: counting the note
    # alone would still read 1 if one caller started rendering a variant of it.
    naming_it = [line for line in printed.splitlines() if Release._meta.db_table in line]
    assert len(naming_it) == 1, naming_it


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
    # Seeded, or the assertion is true because nothing is recorded rather than because the
    # routing filter fired -- the helper clears both families, so there is nothing otherwise.
    key = (Band._meta.db_table, Release._meta.db_table, None)
    command.existing.soft_delete_related[key] = 'abc'
    command.existing.soft_delete_revive[key] = 'def'

    assert command._routed_away_tables() >= {Band._meta.db_table, Release._meta.db_table}
    # Empty hosting is what makes the assertion below bite: the note's earlier "both tables
    # still map" branch cannot be what suppresses it, so the routing filter is.
    assert command._table_app_labels() == {}
    # Both tables map nowhere, so retirement withholds the drop on positive evidence -- and
    # the by-hand note is withheld too, the vendor note having already named the model.
    assert command._unmapped_cascade_notes() == []
    assert command._retired_cascade_operations(apps.get_app_config('testapp')) == []


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


@override_settings(DATABASE_ROUTERS=[_ToNonPgModel()])
def test_a_routed_away_model_contributes_no_rule_edge():
    """An edge from a model this kit writes no rule for is *invented*, and the graph's own note
    says an invented edge is worse than a missing one: it closes a cycle that cannot form and
    refuses every edge on it, withholding a legitimate rule between two PostgreSQL tables."""
    from django.apps import apps

    from guitars.introspection import _rule_update_edges

    edges = _rule_update_edges(apps.get_models())

    assert edges, 'the fixture registry should still produce edges'
    # A cascade edge is ``(target, declaring_table)``, so a routed-away ``Band`` is the
    # *target* of ``Album``'s key -- named at index 0, never index 1. Asserting on index 1
    # alone would be ``[] == []`` against the ungated code.
    assert (Band._meta.db_table, Album._meta.db_table) not in edges
    assert not [edge for edge in edges if Band._meta.db_table in edge]


@override_settings(DATABASE_ROUTERS=[_AlbumToNonPg()])
def test_a_routed_away_declaring_model_drops_the_edge_it_would_contribute():
    """The **model** gate specifically. A cascade edge is ``(target, declaring_table)``, so
    routing the *target* away is what the sibling above covers; only routing the model that
    declares the key isolates this branch, which no other test reaches."""
    from django.apps import apps

    from guitars.introspection import _rule_update_edges

    edges = _rule_update_edges(apps.get_models())

    assert edges, 'the fixture registry should still produce edges'
    assert (Band._meta.db_table, Album._meta.db_table) not in edges


def test_the_router_is_asked_about_the_concrete_model_not_a_proxy():
    """``related_model`` for a ``ForeignKey(SomeProxy)`` *is* the proxy, which the generator
    normalises away before gating -- so a gate asking about the proxy refuses what the
    generator emits, and a dropped cycle edge bricks every table on that cycle."""
    from django.db import models as django_models
    from django.test.utils import isolate_apps

    from guitars.models import SetarModel

    @isolate_apps('tests.testapp')
    def build():
        class Concrete(SetarModel):
            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        class ConcreteProxy(Concrete):
            class Meta:
                app_label = 'testapp'
                proxy = True

        class Referrer(SetarModel):
            target = django_models.ForeignKey(
                ConcreteProxy, on_delete=django_models.CASCADE, related_name='+'
            )

            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        return Concrete, ConcreteProxy, Referrer

    concrete, proxy, referrer = build()
    # The trap itself, asserted rather than assumed.
    assert referrer._meta.get_field('target').related_model is proxy

    class _ProxyAppToNonPg:
        """Routes by *model identity*, so it answers for the proxy and not for its concrete."""

        def allow_migrate(self, db, app_label, model_name=None, **hints):
            if hints.get('model') is proxy:
                return db == 'nonpg'
            return None

    with override_settings(DATABASE_ROUTERS=[_ProxyAppToNonPg()]):
        # The proxy resolves to its concrete model, which no router sends anywhere.
        assert routing.migrates_to_postgresql(proxy) is True
        assert routing.migrates_to_postgresql(concrete) is True


@override_settings(DATABASE_ROUTERS=[_ToNonPg()])
def test_a_routed_away_tenanted_table_is_not_called_a_disagreement():
    """An applied policy on a routed-away model stays, and its objects are real. It is absent
    from ``expected.tables`` because nothing writes it one *now*, not because the models
    stopped expecting it -- which is what the audit used to report."""
    from guitars.tenancy.discovery import expected_coverage, routed_away_tables

    away = routed_away_tables()

    assert Release._meta.db_table in away
    # The premise: it really is missing from the coverage the audit compares against.
    assert Release._meta.db_table not in expected_coverage().tables


def test_nothing_is_routed_away_without_a_router():
    from guitars.tenancy.discovery import routed_away_tables

    assert routed_away_tables() == set()


def test_audittenancy_refuses_a_non_postgresql_alias():
    """Every probe it runs reads a PostgreSQL catalog, so on another backend it would die in
    the driver rather than report -- and a green audit that examined nothing is the one
    failure an audit must not have."""
    with pytest.raises(CommandError, match='sqlite'):
        call_command('audittenancy', '--database', 'nonpg', stdout=StringIO())


def test_sweepowned_refuses_a_non_postgresql_alias():
    with pytest.raises(CommandError, match='sqlite'):
        call_command('sweepowned', '--database', 'nonpg', stdout=StringIO())


@override_settings(DATABASE_ROUTERS=[_AlbumToNonPg()])
def test_a_routed_away_model_declares_no_owning_fields():
    """The runtime half of the gate, on ``hard_delete()``'s path: the generator writes no owned
    rule for a routed-away model, and following in Python what no rule covers destroys what the
    refusal spared."""
    from guitars.models.soft_deletion import _declared_owning_fields

    assert _declared_owning_fields(Album) == []


def test_retire_enforcement_stands_aside_on_a_non_postgresql_connection():
    """Its body is pure PostgreSQL catalog SQL. Skipped rather than failed, which is what
    ``RunSQL`` does when the router says no: recorded as applied, having done nothing."""
    from guitars.operations import RetireEnforcement

    executed = []

    class _Editor:
        connection = types.SimpleNamespace(vendor='sqlite', alias='nonpg')

        def execute(self, *args, **kwargs):
            executed.append(args)

    RetireEnforcement('any_table').database_forwards('testapp', _Editor(), None, None)

    assert executed == []
