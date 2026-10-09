"""The shapes #66 had open on 2.20.0 (ADR 0043): a model deleted and recreated, a rename whose old
table a new model retakes, a model moved to another app and back. Each fixture app's last
migration is what regenerating wrote once the scan replayed the graph."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import override_settings

from guitars.management import _generator
from tests.conftest import execute, scalar


FIXTURES = [
    'tests.issue66_recreated',
    'tests.issue66_retaken',
    'tests.issue66_anc',
    'tests.issue66_shop',
]


def _has_updated_at_trigger(table: str) -> bool:
    return bool(
        scalar(
            'SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass '
            "AND tgname = 'updated_at_trigger'",
            [table],
        )
    )


@override_settings(LOCAL_APPS=['tests.testapp', *FIXTURES])
def test_every_history_converges_to_nothing_left_to_write():
    """Not merely "something was written": a scan that re-emitted what it wrote would never go
    green, so ``--check`` over all four apps is the pin against endless regeneration."""
    call_command(
        'makeguitarmigrations',
        *(label.rsplit('.', 1)[-1] for label in FIXTURES),
        '--check',
        stdout=StringIO(),
        stderr=StringIO(),
    )


def _report_without(monkeypatch, hidden: set[tuple[str, str]], *app_labels) -> str:
    """``--check``'s report over the fixture histories as they stood before *hidden* files were
    written: what the scan has to read as uncovered for regenerating to write them. Raises
    ``AssertionError`` if the check passes, and fails on any refusal that is not "missing"."""
    real = _generator.iter_migration_files

    def _iter(app):
        for path, content in real(app):
            if (app.label, path.stem) not in hidden:
                yield path, content

    monkeypatch.setattr(_generator, 'iter_migration_files', _iter)
    out, err = StringIO(), StringIO()
    try:
        call_command('makeguitarmigrations', *app_labels, '--check', stdout=out, stderr=err)
    except CommandError as error:
        if 'to create missing migrations' not in str(error):
            pytest.fail(f'--check refused before reporting: {error}', pytrace=False)
        return out.getvalue() + err.getvalue()
    raise AssertionError('--check passed over this history')


@override_settings(LOCAL_APPS=['tests.testapp', *FIXTURES])
def test_a_recreated_model_is_named_as_uncovered(monkeypatch):
    report = _report_without(
        monkeypatch, {('issue66_recreated', '0007_auto_enforcement')}, 'issue66_recreated'
    )

    assert 'Soft Delete Rule on "issue66_recreated_part" table!' in report


@override_settings(LOCAL_APPS=['tests.testapp', *FIXTURES])
def test_a_model_retaking_a_renamed_tables_name_is_named_as_uncovered(monkeypatch):
    report = _report_without(
        monkeypatch, {('issue66_retaken', '0008_auto_enforcement')}, 'issue66_retaken'
    )

    assert 'Soft Delete Rule on "issue66_retaken_crew" table!' in report


@override_settings(LOCAL_APPS=['tests.testapp', *FIXTURES])
def test_a_model_moved_back_has_its_stale_sweep_named(monkeypatch):
    hidden = {('issue66_anc', '0008_auto_enforcement'), ('issue66_shop', '0009_auto_enforcement')}

    report = _report_without(monkeypatch, hidden, 'issue66_anc', 'issue66_shop')

    assert 'Owned Sweep on "issue66_anc_hub" that is owned by "issue66_shop_keeper"' in report


def test_a_recreated_models_delete_keeps_the_row(db):
    maker = scalar(
        'INSERT INTO issue66_recreated_maker (name, _created_at, _updated_at) '
        "VALUES ('m', now(), now()) RETURNING id"
    )
    execute(
        'INSERT INTO issue66_recreated_part (owner_id, _created_at, _updated_at) '
        'VALUES (%s, now(), now())',
        params=[maker],
    )

    execute('DELETE FROM issue66_recreated_part')

    assert scalar('SELECT count(*) FROM issue66_recreated_part') == 1
    assert _has_updated_at_trigger('issue66_recreated_part')


def test_a_retaking_models_delete_keeps_the_row(db):
    boss = scalar(
        'INSERT INTO issue66_retaken_boss (name, _created_at, _updated_at) '
        "VALUES ('b', now(), now()) RETURNING id"
    )
    execute(
        'INSERT INTO issue66_retaken_crew (lead_id, _created_at, _updated_at) '
        'VALUES (%s, now(), now())',
        params=[boss],
    )

    execute('DELETE FROM issue66_retaken_crew')

    assert scalar('SELECT count(*) FROM issue66_retaken_crew') == 1
    assert _has_updated_at_trigger('issue66_retaken_crew')


def test_updating_and_archiving_an_owner_of_a_model_moved_back_works(db):
    """The sweep named the table the move back renamed away, so every UPDATE of the owner failed."""
    hub = scalar(
        'INSERT INTO issue66_anc_hub (name, _created_at, _updated_at) '
        "VALUES ('h', now(), now()) RETURNING id"
    )
    execute(
        'INSERT INTO issue66_shop_keeper (hub_id, _created_at, _updated_at) '
        'VALUES (%s, now(), now())',
        params=[hub],
    )

    with transaction.atomic():
        execute('UPDATE issue66_shop_keeper SET hub_id = hub_id')
        execute('UPDATE issue66_shop_keeper SET _deleted_at = now()')

    assert scalar('SELECT count(*) FROM issue66_anc_hub WHERE _deleted_at IS NOT NULL') == 1


@pytest.mark.parametrize('table', ['issue66_recreated_part', 'issue66_retaken_crew'])
def test_a_table_taken_after_its_models_went_away_has_its_own_rule(table, db):
    rule = scalar(
        "SELECT count(*) FROM pg_rules WHERE tablename = %s AND rulename = 'soft_delete'", [table]
    )
    assert rule == 1
