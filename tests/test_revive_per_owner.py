"""One revive trigger per owner table (2.16.0, #70): every cascade key's revive as an arm of one
function, which leaves at its first test unless the statement revived a row. The per-key
triggers it replaces are retired, each reverse rebuilding the trigger it dropped."""

from __future__ import annotations

import pytest
from django.apps import apps
from django.db import migrations

from guitars.management.enforcement import operations as operations_module
from guitars.management.enforcement.command import Command
from tests.conftest import clear_cascade_coverage, scalar


def _app():
    return apps.get_app_config('testapp')


def _forward(operation: str):
    source = operation[operation.index('migrations.RunSQL') :].rstrip().rstrip(',')
    return eval(source, {'migrations': migrations})  # noqa: S307 - our own output


@pytest.mark.django_db
class TestTheDatabase:
    def test_each_owner_table_carries_one_revive_trigger(self):
        """The label table owns eleven cascade keys, flat and joined: one trigger, not eleven."""
        counts = dict(
            (table, count)
            for table, count in _rows(
                "SELECT tgrelid::regclass::text, count(*) FROM pg_trigger "
                "WHERE tgname LIKE 'soft\\_delete\\_revive%%' AND NOT tgisinternal "
                'GROUP BY 1'
            )
        )
        assert counts['testapp_label'] == 1
        assert set(counts.values()) == {1}

    def test_no_per_key_revive_is_left(self):
        assert not scalar(
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'soft\\_delete\\_revive\\_%%' "
            "AND tgname NOT LIKE 'soft\\_delete\\_revive\\_on\\_%%'"
        )


def _rows(sql):
    from django.db import connection  # noqa: PLC0415

    with connection.cursor() as cursor:
        cursor.execute(sql)
        return cursor.fetchall()


class TestTheOwnersOperation:
    def test_one_arm_per_key_behind_one_early_exit(self):
        command = Command()
        clear_cascade_coverage(command)
        (revive,) = [
            op
            for op in command._revive_operations(_app())
            if op.startswith('# Soft Delete Revive Trigger on "testapp_album" table!')
        ]
        body = _forward(revive).sql

        assert body.count('UPDATE "testapp_merch" AS guitars_child') == 2
        assert body.index('AND EXISTS (') < body.index('UPDATE "testapp_merch"')
        assert body.count('CREATE TRIGGER') == 1

    def test_an_owner_whose_every_arm_is_refused_gets_no_trigger(self, monkeypatch):
        command = Command()
        clear_cascade_coverage(command)
        monkeypatch.setattr(command, '_revive_arm', lambda *args, **kwargs: None)

        assert command._revive_operations(_app()) == []

    def test_an_owner_named_with_dollar_quoting_is_refused_and_named(self, monkeypatch):
        command = Command()
        command._skipped_rule_notes.clear()
        clear_cascade_coverage(command)
        monkeypatch.setattr(operations_module, '_revive_owner_name', lambda table: 'x$$y')

        assert command._revive_operations(_app()) == []
        assert any(
            "Revive trigger on 'testapp_album' skipped" in note
            for note in command._skipped_rule_notes
        )


class TestTheTransition:
    """Every per-key revive a project recorded is retired, its key still cascading or not."""

    @staticmethod
    def _retirements(key):
        command = Command()
        clear_cascade_coverage(command)
        command.existing.soft_delete_related[key] = 'abc'
        command.existing.soft_delete_revive[key] = 'def'
        return command._retired_cascade_operations(_app())

    def test_a_key_still_cascading_keeps_its_rule_and_loses_its_trigger(self):
        (retirement,) = self._retirements(('testapp_album', 'testapp_band', None))

        assert retirement.startswith('# Soft Delete Revive Trigger retired on "testapp_album"')
        assert 'DROP TRIGGER IF EXISTS' in _forward(retirement).sql

    def test_its_reverse_rebuilds_the_flat_trigger(self):
        (retirement,) = self._retirements(('testapp_album', 'testapp_band', None))

        reverse = _forward(retirement).reverse_sql
        assert 'CREATE TRIGGER "soft_delete_revive_12_testapp_band_13_testapp_album"' in reverse
        assert 'guitars_child."band_id" = guitars_revived."id"' in reverse

    def test_a_joined_keys_reverse_rebuilds_the_joined_trigger(self):
        (retirement,) = self._retirements(('testapp_touringfestival', 'testapp_label', None))

        reverse = _forward(retirement).reverse_sql
        assert 'UPDATE "testapp_festival" AS guitars_child' in reverse
        assert 'guitars_link."festival_ptr_id"' in reverse
        assert 'RAISE' not in reverse


class TestWhereTheTriggerIsWritten:
    """Hosted by the owner table's app, or -- an owner outside ``LOCAL_APPS`` -- by an app
    contributing an arm, where 2.15's per-key trigger was written."""

    @staticmethod
    def _without_band(command, monkeypatch):
        hosting = command._table_app_labels

        def unhosted():
            return {table: app for table, app in hosting().items() if table != 'testapp_band'}

        monkeypatch.setattr(command, '_table_app_labels', unhosted)
        return command

    def test_an_owner_outside_local_apps_is_hosted_by_a_contributing_app(self, monkeypatch):
        command = self._without_band(Command(), monkeypatch)
        clear_cascade_coverage(command)

        assert command._revive_host('testapp_band') == 'testapp'
        assert any(
            op.startswith('# Soft Delete Revive Trigger on "testapp_band" table!')
            for op in command._revive_operations(_app())
        )

    def test_an_owner_routed_off_postgresql_gets_no_host(self, monkeypatch):
        """Routed away maps to nothing by design (ADR 0022): a contributing app on PostgreSQL
        must not take a trigger on a table that is not there."""
        from tests.testapp.models import Band  # noqa: PLC0415

        command = self._without_band(Command(), monkeypatch)
        command._revive_arms_by_owner()  # the sweep, before the owner is routed away
        routed = operations_module.migrates_to_postgresql
        monkeypatch.setattr(
            operations_module, 'migrates_to_postgresql', lambda model: model is not Band and routed(model)
        )

        assert command._revive_host('testapp_band') is None

    def test_its_retirement_lands_where_it_was_created(self, monkeypatch):
        command = self._without_band(Command(), monkeypatch)
        arms = command._revive_arms_by_owner()
        monkeypatch.setattr(
            command,
            '_revive_arms_by_owner',
            lambda: {owner: kept for owner, kept in arms.items() if owner != 'testapp_band'},
        )
        command.existing.soft_delete_revive_owner[('testapp_band',)] = 'abc'
        command.existing.soft_delete_revive_owner_dependencies[('testapp_band',)] = [
            ('testapp', '0074_auto_enforcement')
        ]

        assert any(
            'retired on "testapp_band" table!' in op
            for op in command._retired_trigger_operations(_app())
        )

    def test_a_superseded_revive_is_retired_though_its_child_is_not_local(self, monkeypatch):
        """No positive evidence is needed: the models still call for its key, and the owner's
        trigger carries its arm. The guard that withholds an unowed rule's drop does not apply."""
        command = Command()
        clear_cascade_coverage(command)
        hosting = command._table_app_labels
        monkeypatch.setattr(
            command,
            '_table_app_labels',
            lambda: {table: app for table, app in hosting().items() if table != 'testapp_album'},
        )
        key = ('testapp_album', 'testapp_band', None)
        command.existing.soft_delete_revive[key] = 'def'

        (retirement,) = command._retired_cascade_operations(_app())

        assert retirement.startswith('# Soft Delete Revive Trigger retired on "testapp_album"')


def test_two_relations_sharing_a_cascade_key_get_an_arm_each(monkeypatch):
    """A model keyed to an MTI parent and to its child reaches one owner through two keys that
    share the cascade key's ``None`` form. Keyed on that, the second arm replaced the first."""
    import types  # noqa: PLC0415

    from django.db.models import CASCADE, ForeignKey  # noqa: PLC0415
    from django.test.utils import isolate_apps, override_settings  # noqa: PLC0415

    from guitars.models import SetarModel  # noqa: PLC0415

    @isolate_apps('tests.testapp')
    def _models():
        class Parent(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Child(Parent):
            class Meta:
                app_label = 'testapp'

        class Referrer(SetarModel):
            p = ForeignKey(Parent, on_delete=CASCADE, related_name='ps')
            c = ForeignKey(Child, on_delete=CASCADE, related_name='cs')

            class Meta:
                app_label = 'testapp'

        return Parent, Child, Referrer

    parent, child, referrer = _models()
    fake = types.SimpleNamespace(
        name='fake.app', label='fakeapp', get_models=lambda: [parent, child, referrer]
    )
    monkeypatch.setattr(operations_module.django_apps, 'get_app_configs', lambda: [fake])
    with override_settings(LOCAL_APPS=['fake.app']):
        command = Command()
        command.reverse_relations_mapping[parent] = {
            (referrer, referrer._meta.get_field('p'), CASCADE)
        }
        command.reverse_relations_mapping[child] = {
            (referrer, referrer._meta.get_field('c'), CASCADE)
        }
        arms = command._revive_arms_by_owner()[parent._meta.db_table]

    assert sorted(column for _model, column in arms.values()) == ['c_id', 'p_id']


class TestAScopedRunNamesTheOwnersTrigger:
    """Its arms can come from the apps in scope -- an MTI descendant's key above all -- while
    only a run including the owner's app writes it (#70)."""

    def test_an_out_of_date_trigger_is_named(self):
        command = Command()
        command.existing.soft_delete_revive_owner[('testapp_band',)] = 'stale0000000'

        notes = command._scoped_trigger_retirement_notes({'crossapp_owner'})

        assert any(
            '"soft_delete_revive_on_12_testapp_band" on \'testapp_band\' is out of date' in note
            for note in notes
        )

    def test_a_missing_trigger_is_named(self):
        command = Command()
        del command.existing.soft_delete_revive_owner[('testapp_band',)]

        notes = command._scoped_trigger_retirement_notes({'crossapp_owner'})

        assert any("'testapp_band' is missing" in note for note in notes)

    def test_a_trigger_no_key_calls_for_is_named(self, monkeypatch):
        command = Command()
        arms = command._revive_arms_by_owner()
        monkeypatch.setattr(
            command,
            '_revive_arms_by_owner',
            lambda: {owner: kept for owner, kept in arms.items() if owner != 'testapp_band'},
        )

        notes = command._scoped_trigger_retirement_notes({'crossapp_owner'})

        assert any("'testapp_band' is no longer called for" in note for note in notes)

    def test_nothing_is_named_when_the_owners_app_is_in_scope(self):
        command = Command()
        command.existing.soft_delete_revive_owner[('testapp_band',)] = 'stale0000000'

        assert not [
            note
            for note in command._scoped_trigger_retirement_notes({'testapp'})
            if note.startswith('Revive trigger')
        ]

    def test_an_up_to_date_project_names_nothing(self):
        assert Command()._scoped_trigger_retirement_notes({'crossapp_owner'}) == []


def test_a_superseded_revive_is_retired_only_by_its_host():
    """Not by every app that records nothing on its owner: one host, or two apps drop one object."""
    command = Command()
    clear_cascade_coverage(command)
    command.existing.soft_delete_revive[('testapp_album', 'testapp_band', None)] = 'def'

    assert command._retired_cascade_operations(apps.get_app_config('crossapp_owner')) == []


class TestAQuietReadReportsNothing:
    """A scoped run compares digests through the same builder; a refusal is reported once, by
    the run that emits it, not again by every run that only asks."""

    def test_a_refused_owner(self, monkeypatch):
        command = Command()
        command._skipped_rule_notes.clear()
        monkeypatch.setattr(operations_module, '_revive_owner_name', lambda table: 'x$$y')

        assert command._revive_owner_slots('testapp_album', quiet=True) is None
        assert command._skipped_rule_notes == []

    def test_a_refused_arm(self):
        from tests.test_command import _revive_dollar_models  # noqa: PLC0415

        _owner, child = _revive_dollar_models()
        command = Command()
        command._skipped_rule_notes.clear()
        key = ('testapp_dollar$$child', 'testapp_dollar_owner', None)

        assert command._revive_arm(key, child, 'owner_id', 'id', quiet=True) is None
        assert command._skipped_rule_notes == []
