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
    def test_each_owner_table_carries_one_cascade_trigger(self):
        """The label table owns eleven cascade keys, flat and joined: one trigger, not eleven."""
        counts = dict(
            (table, count)
            for table, count in _rows(
                "SELECT tgrelid::regclass::text, count(*) FROM pg_trigger "
                "WHERE tgname LIKE 'soft\\_delete\\_cascade\\_on%%' AND NOT tgisinternal "
                'GROUP BY 1'
            )
        )
        assert counts['testapp_label'] == 1
        assert set(counts.values()) == {1}

    def test_the_revive_only_owner_trigger_is_retired(self):
        """What 2.16.0 -- 2.18.x wrote, superseded by the one that archives as well (#80)."""
        assert not scalar(
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'soft\\_delete\\_revive\\_on%%'"
        )

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
            if op.startswith('# Soft Delete Cascade Trigger on "testapp_album" table!')
        ]
        body = _forward(revive).sql

        # Two keys to merch, each an archive arm and a revive arm (#80, ADR 0039).
        assert body.count('UPDATE "testapp_merch" AS guitars_child') == 4
        assert body.count('SET _deleted_at = guitars_archived._deleted_at') == 2
        assert body.count('SET _deleted_at = NULL') == 2
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
        monkeypatch.setattr(operations_module, '_cascade_owner_name', lambda table: 'x$$y')

        assert command._revive_operations(_app()) == []
        assert any(
            "Cascade trigger on 'testapp_album' skipped" in note
            for note in command._skipped_rule_notes
        )


class TestTheTransition:
    """Every per-key revive and cascade rule a project recorded is retired, its key still
    cascading or not: the owner's trigger carries both (2.16.0, and 2.19.0 for the rule)."""

    @staticmethod
    def _retirements(key):
        command = Command()
        clear_cascade_coverage(command)
        command.existing.soft_delete_related[key] = 'abc'
        command.existing.soft_delete_revive[key] = 'def'
        return command._retired_cascade_operations(_app())

    def test_a_key_still_cascading_loses_its_rule_and_its_trigger(self):
        rule, revive = self._retirements(('testapp_album', 'testapp_band', None))

        assert rule.startswith('# Soft Delete Related Rule retired on "testapp_album"')
        assert 'DROP RULE IF EXISTS "soft_delete_related_testapp_album"' in _forward(rule).sql
        assert revive.startswith('# Soft Delete Revive Trigger retired on "testapp_album"')
        assert 'DROP TRIGGER IF EXISTS' in _forward(revive).sql

    def test_the_rules_reverse_rebuilds_it_as_it_was_written(self):
        rule, _revive = self._retirements(('testapp_album', 'testapp_band', None))

        reverse = _forward(rule).reverse_sql
        assert 'CREATE OR REPLACE RULE "soft_delete_related_testapp_album"' in reverse
        assert 'AS ON UPDATE TO "testapp_band"' in reverse
        assert 'WHERE "band_id" = old."id"' in reverse

    def test_a_joined_rules_reverse_rebuilds_the_joined_rule(self):
        rule, _revive = self._retirements(('testapp_touringfestival', 'testapp_label', None))

        reverse = _forward(rule).reverse_sql
        assert 'AS ON UPDATE TO "testapp_label"' in reverse
        assert 'UPDATE "testapp_festival"' in reverse
        assert 'RAISE' not in reverse

    def test_a_rule_whose_arm_is_refused_is_not_dropped(self, monkeypatch):
        """Dropping it would end the cascade: its arm is not written."""
        key = ('testapp_album', 'testapp_band', None)
        command = Command()
        clear_cascade_coverage(command)
        command.existing.soft_delete_related[key] = 'abc'
        monkeypatch.setattr(command, '_revive_arm', lambda *args, **kwargs: None)

        assert command._retired_cascade_operations(_app()) == []

    def test_its_reverse_rebuilds_the_flat_trigger(self):
        _rule, retirement = self._retirements(('testapp_album', 'testapp_band', None))

        reverse = _forward(retirement).reverse_sql
        assert 'CREATE TRIGGER "soft_delete_revive_12_testapp_band_13_testapp_album"' in reverse
        assert 'guitars_child."band_id" = guitars_revived."id"' in reverse

    def test_a_joined_keys_reverse_rebuilds_the_joined_trigger(self):
        _rule, retirement = self._retirements(('testapp_touringfestival', 'testapp_label', None))

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
        # A first create: no app has written it yet, so none keeps it.
        command.existing.soft_delete_cascade_owner_dependencies.pop(('testapp_band',), None)

        assert command._revive_host('testapp_band') == 'testapp'
        assert any(
            op.startswith('# Soft Delete Cascade Trigger on "testapp_band" table!')
            for op in command._revive_operations(_app())
        )

    def test_an_owner_routed_off_postgresql_gets_no_host(self, monkeypatch):
        """Routed away maps to nothing by design (ADR 0022): a contributing app on PostgreSQL
        must not take a trigger on a table that is not there."""
        from tests.testapp.models import Band  # noqa: PLC0415

        command = self._without_band(Command(), monkeypatch)
        command._revive_arms_by_owner()  # the sweep, before the owner is routed away
        # Outside ``LOCAL_APPS``, which ``_routed_away_tables`` reads: only the model can say.
        monkeypatch.setattr(command, '_routed_away_tables', frozenset)
        routed = operations_module.migrates_to_postgresql
        monkeypatch.setattr(
            operations_module, 'migrates_to_postgresql', lambda model: model is not Band and routed(model)
        )

        assert command._revive_host('testapp_band') is None

    def test_the_app_that_created_it_keeps_it(self, monkeypatch, settings):
        """A computed host would move as contributors change, and the new one's ``DROP TRIGGER``
        would run before the old one's ``CREATE`` on a fresh ``migrate``: one host for life."""
        settings.LOCAL_APPS = [*settings.LOCAL_APPS, 'tests.crossapp_owner']
        command = self._without_band(Command(), monkeypatch)
        command.existing.soft_delete_cascade_owner_dependencies[('testapp_band',)] = [
            ('crossapp_owner', '0003_auto_enforcement')
        ]

        assert command._revive_host('testapp_band') == 'crossapp_owner'

    def test_even_once_its_table_is_hosted(self, settings):
        """The owner's own app joining ``LOCAL_APPS`` does not move it either."""
        settings.LOCAL_APPS = [*settings.LOCAL_APPS, 'tests.crossapp_owner']
        command = Command()
        command.existing.soft_delete_cascade_owner_dependencies[('testapp_band',)] = [
            ('crossapp_owner', '0003_auto_enforcement')
        ]

        assert command._revive_host('testapp_band') == 'crossapp_owner'

    def test_its_retirement_lands_where_it_was_created(self, monkeypatch):
        command = self._without_band(Command(), monkeypatch)
        arms = command._revive_arms_by_owner()
        monkeypatch.setattr(
            command,
            '_revive_arms_by_owner',
            lambda: {owner: kept for owner, kept in arms.items() if owner != 'testapp_band'},
        )
        command.existing.soft_delete_cascade_owner[('testapp_band',)] = 'abc'
        command.existing.soft_delete_cascade_owner_dependencies[('testapp_band',)] = [
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
        command.existing.soft_delete_cascade_owner[('testapp_band',)] = 'stale0000000'

        notes = command._scoped_trigger_retirement_notes({'crossapp_owner'})

        assert any(
            '"soft_delete_cascade_on_12_testapp_band" on \'testapp_band\' is out of date' in note
            for note in notes
        )

    def test_a_missing_trigger_is_named(self):
        command = Command()
        del command.existing.soft_delete_cascade_owner[('testapp_band',)]

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
        command.existing.soft_delete_cascade_owner[('testapp_band',)] = 'stale0000000'

        assert not [
            note
            for note in command._scoped_trigger_retirement_notes({'testapp'})
            if note.startswith('Cascade trigger')
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
        monkeypatch.setattr(operations_module, '_cascade_owner_name', lambda table: 'x$$y')

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


def test_a_recorded_trigger_whose_every_arm_is_refused_fails_check(monkeypatch):
    """Not emitted, not retired -- its key still calls for an arm -- and so live in every
    migrated database with nothing to say so: named for a hand drop, as the per-key one was."""
    command = Command()
    command._refusals_over_live_rules.clear()
    monkeypatch.setattr(command, '_revive_arm', lambda *args, **kwargs: None)

    command._revive_operations(_app())

    assert any(
        "Cascade trigger on 'testapp_album'" in refusal and 'DROP TRIGGER' in refusal
        for refusal in command._refusals_over_live_rules
    )


def test_a_trigger_already_rid_of_a_deleted_childs_arm_is_not_named(monkeypatch):
    """The owner's trigger re-emitted without the arm is current; a note reading only "is it
    recorded" went on telling every scoped run it was broken, for good."""
    command = Command()
    command.existing.soft_delete_related[('gone_child', 'testapp_band', None)] = 'abc'
    monkeypatch.setattr(command, '_dropped_tables', lambda: {'gone_child': ('testapp', '0099')})

    notes = [
        *command._scoped_cascade_retirement_notes({'crossapp_owner'}),
        *command._scoped_trigger_retirement_notes({'crossapp_owner'}),
    ]

    assert not [note for note in notes if 'soft_delete_revive_on' in note]


def test_a_refused_renamed_owners_hand_drop_names_every_spelling(monkeypatch):
    """PostgreSQL keeps a trigger with its table, so after a rename the live one carries the
    old name: a hand drop of the current spelling alone would leave it."""
    command = Command()
    command._refusals_over_live_rules.clear()
    command.existing.renamed_tables['testapp_album'] = ['testapp_oldalbum']
    monkeypatch.setattr(command, '_revive_arm', lambda *args, **kwargs: None)

    command._revive_operations(_app())

    (refusal,) = [r for r in command._refusals_over_live_rules if "'testapp_album'" in r]
    assert 'soft_delete_cascade_on_16_testapp_oldalbum' in refusal
    assert 'soft_delete_cascade_on_13_testapp_album' in refusal


class TestTheReviveOnlyTriggerIsSuperseded:
    """2.16.0 -- 2.18.x wrote ``soft_delete_revive_on_*``; the trigger that archives too is
    ``soft_delete_cascade_on_*`` (#80, ADR 0039), so the first is retired where the second is
    written. A rename needs ``DROP TRIGGER``, which is why a rename cost what it cost."""

    @staticmethod
    def _command(table='testapp_band'):
        command = Command()
        clear_cascade_coverage(command)
        command.existing.soft_delete_revive_owner[(table,)] = 'abc'
        return command

    @staticmethod
    def _retirement(built, table='testapp_band'):
        (retirement,) = [
            op
            for op in built
            if op.startswith(f'# Soft Delete Revive Trigger retired on "{table}" table!')
        ]
        return retirement

    def test_it_is_dropped_after_the_trigger_that_supersedes_it(self):
        built = self._command()._build_operations(_app())

        new = next(
            i for i, op in enumerate(built) if op.startswith('# Soft Delete Cascade Trigger on "testapp_band"')
        )
        assert new < built.index(self._retirement(built))

    def test_the_drop_covers_both_the_trigger_and_its_function(self):
        forward = _forward(self._retirement(self._command()._build_operations(_app()))).sql

        assert 'DROP TRIGGER IF EXISTS "soft_delete_revive_on_12_testapp_band"' in forward
        assert 'DROP FUNCTION IF EXISTS "soft_delete_revive_on_12_testapp_band"()' in forward

    def test_its_reverse_rebuilds_the_revive_only_trigger_under_its_old_name(self):
        reverse = _forward(self._retirement(self._command()._build_operations(_app()))).reverse_sql

        assert 'CREATE TRIGGER "soft_delete_revive_on_12_testapp_band"' in reverse
        assert 'guitars_revived' in reverse
        assert 'guitars_archived' not in reverse
        assert 'RAISE' not in reverse

    def test_it_is_kept_where_the_trigger_that_supersedes_it_is_not_written(self, monkeypatch):
        command = self._command()
        monkeypatch.setattr(command, '_revive_arm', lambda *args, **kwargs: None)

        built = command._build_operations(_app())

        assert not [op for op in built if 'Revive Trigger retired on "testapp_band"' in op]

    def test_one_whose_owner_has_no_cascade_key_left_goes_with_a_refusing_reverse(self):
        """A table nothing cascades from: no arm to rebuild it from, as any retirement."""
        command = self._command('testapp_riff')

        retirement = self._retirement(command._build_operations(_app()), 'testapp_riff')

        assert 'RAISE' in _forward(retirement).reverse_sql
