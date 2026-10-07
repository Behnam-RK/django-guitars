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
        monkeypatch.setattr(command, '_revive_arm', lambda *args: None)

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
