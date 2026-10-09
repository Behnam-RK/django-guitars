"""Strict-xfail repros of the shapes #66 still has open on 2.20.0. A fix flips the ``--check``
tests first: delete them, regenerate the fixture app, run with ``--create-db`` (a reused database
keeps old objects), and drop the marker from the database tests, which stay as the pins."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import ProgrammingError
from django.test import override_settings

from tests.conftest import execute, scalar


def _check_report(*app_labels) -> str:
    """``makeguitarmigrations --check``'s report, failing an assertion if it passes. Its missing-
    coverage error names nothing; the report on stdout does. Any other refusal is a real failure."""
    out, err = StringIO(), StringIO()
    try:
        call_command('makeguitarmigrations', *app_labels, '--check', stdout=out, stderr=err)
    except CommandError as error:
        if 'to create missing migrations' not in str(error):
            pytest.fail(f'--check refused before reporting: {error}', pytrace=False)
        return out.getvalue() + err.getvalue()
    raise AssertionError('--check passed over this history')


def _has_updated_at_trigger(table: str) -> bool:
    return bool(
        scalar(
            'SELECT count(*) FROM pg_trigger WHERE tgrelid = %s::regclass '
            "AND tgname = 'updated_at_trigger'",
            [table],
        )
    )


# ---- (a) a model deleted, then recreated on the same table, generating at every step --------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason='#66 (a): own-table coverage of a dropped table is never forgotten, so the recreated '
    'model reads as covered and gets no rule or trigger',
)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.issue66_recreated'])
def test_a_recreated_model_is_named_as_uncovered():
    report = _check_report('issue66_recreated')

    assert 'Soft Delete Rule on "issue66_recreated_part" table!' in report


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason='#66 (a): no soft-delete rule, so DELETE removes the row, and no updated_at trigger',
)
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


# ---- (b) a renamed model's old table retaken by a new model --------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason='#66 (b): the rename walk keeps coverage under a name live again, so the retaking '
    'model reads as covered by its predecessor',
)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.issue66_retaken'])
def test_a_model_retaking_a_renamed_tables_name_is_named_as_uncovered():
    report = _check_report('issue66_retaken')

    assert 'Soft Delete Rule on "issue66_retaken_crew" table!' in report


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason='#66 (b): no soft-delete rule, so DELETE removes the row, and no updated_at trigger',
)
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


# ---- (c) a model moved to another app and back ---------------------------------------------


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason='#66 (c): a move back keeps the record filed before the first move, so the owned '
    'sweep naming the intermediate table is never re-emitted',
)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.issue66_anc', 'tests.issue66_shop'])
def test_a_model_moved_back_is_named_as_stale():
    report = _check_report('issue66_anc', 'issue66_shop')

    assert 'Owned Sweep on "issue66_anc_hub" that is owned by "issue66_shop_keeper"' in report


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason='#66 (c): the sweep body names issue66_shop_hub, which the move back renamed away, so '
    'every UPDATE of the keeper table fails',
)
def test_updating_and_archiving_an_owner_of_a_model_moved_back_works(db):
    hub = scalar(
        'INSERT INTO issue66_anc_hub (name, _created_at, _updated_at) '
        "VALUES ('h', now(), now()) RETURNING id"
    )
    execute(
        'INSERT INTO issue66_shop_keeper (hub_id, _created_at, _updated_at) '
        'VALUES (%s, now(), now())',
        params=[hub],
    )

    try:
        execute('UPDATE issue66_shop_keeper SET hub_id = hub_id')
    except ProgrammingError as error:
        # Only the failure this pins counts; any other is the fixture breaking, not #66.
        if 'issue66_shop_hub' not in str(error):
            pytest.fail(f'UPDATE failed for another reason: {error}', pytrace=False)
        raise AssertionError('the sweep still names issue66_shop_hub') from error
    execute('UPDATE issue66_shop_keeper SET _deleted_at = now()')

    assert scalar('SELECT count(*) FROM issue66_anc_hub WHERE _deleted_at IS NOT NULL') == 1
