"""Strict-xfail repros of the shapes #66 still has open on 2.20.0 (the issue's re-scope comment).
Each asserts the **correct** end state, so it fails today and a fix flips it. Its fixture app then
needs regenerating and the marker goes; an accidental fix fails the suite on the strict marker."""

from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import override_settings

from tests.conftest import execute, scalar


def _check_report(*app_labels) -> str:
    """``makeguitarmigrations --check``'s report, raising if it passes. The ``CommandError`` says
    only "create missing migrations"; what is missing is named on stdout and stderr."""
    out, err = StringIO(), StringIO()
    with pytest.raises(CommandError):
        call_command('makeguitarmigrations', *app_labels, '--check', stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


# ---- (a) a model deleted, then recreated on the same table, generating at every step --------


@pytest.mark.xfail(
    strict=True,
    reason='#66 (a): own-table coverage of a dropped table is never forgotten, so the recreated '
    'model reads as covered and gets no rule or trigger',
)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.issue66_recreated'])
def test_a_recreated_model_is_named_as_uncovered():
    assert 'issue66_recreated_part' in _check_report('issue66_recreated')


@pytest.mark.xfail(strict=True, reason='#66 (a): no soft-delete rule, so DELETE removes the row')
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


# ---- (b) a renamed model's old table retaken by a new model --------------------------------


@pytest.mark.xfail(
    strict=True,
    reason='#66 (b): the rename walk keeps coverage under a name live again, so the retaking '
    'model reads as covered by its predecessor',
)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.issue66_retaken'])
def test_a_model_retaking_a_renamed_tables_name_is_named_as_uncovered():
    assert 'issue66_retaken_crew' in _check_report('issue66_retaken')


@pytest.mark.xfail(strict=True, reason='#66 (b): no soft-delete rule, so DELETE removes the row')
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


# ---- (c) a model moved to another app and back ---------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason='#66 (c): a move back keeps the record filed before the first move, so the owned '
    'sweep naming the intermediate table is never re-emitted',
)
@override_settings(LOCAL_APPS=['tests.testapp', 'tests.issue66_anc', 'tests.issue66_shop'])
def test_a_model_moved_back_is_named_as_stale():
    assert 'issue66_shop_keeper' in _check_report('issue66_anc', 'issue66_shop')


@pytest.mark.xfail(
    strict=True,
    reason='#66 (c): the sweep body names issue66_shop_hub, which the move back renamed away',
)
def test_archiving_an_owner_of_a_model_moved_back_works(db):
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
        execute('UPDATE issue66_shop_keeper SET _deleted_at = now()')

    assert scalar('SELECT count(*) FROM issue66_anc_hub WHERE _deleted_at IS NOT NULL') == 1
