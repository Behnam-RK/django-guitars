"""The cascade as an arm of the owner's trigger (#80, ADR 0039), by what it does to rows. Raw
statements, not the ORM: Django's collector names every level itself, so an ORM-driven test
passes with the arms dropped."""

from importlib import import_module

import pytest
from django.db.models import CASCADE
from django.test.utils import isolate_apps
from django.apps import apps as django_apps
from django.db import connection, models, transaction
from django.db.utils import NotSupportedError

from tests.conftest import execute, rows, scalar
from guitars.introspection import CascadeKind, classify_cascade
from guitars.management.enforcement import operations as operations_module
from guitars.management.enforcement.command import Command
from guitars.models import SetarModel
from guitars.sql import _identifiers
from guitars.tenancy import tenancy_bypassed
from tests.testapp.models import Album, Band, Catalog, Label, Listing, Merch, TouringFestival


@pytest.fixture
def two_bands(db):
    made = []
    for name in ('Rush', 'Yes'):
        band = Band.objects.create(name=name)
        album = Album.objects.create(title=f'{name}-album', band=band)
        Merch.objects.create(description=f'{name}-shirt', album=album)
        made.append(band)
    return made


def _live(model) -> int:
    return model.objects.count()


def test_one_statement_archives_several_owners_and_every_level_below(two_bands):
    execute(
        'UPDATE testapp_band SET _deleted_at = NOW() WHERE id = ANY(%s)',
        params=[[band.pk for band in two_bands]],
    )

    assert (_live(Band), _live(Album), _live(Merch)) == (0, 0, 0)


def test_a_child_carries_its_parents_own_stamp_down_every_level(two_bands):
    stamp = '2020-01-01 00:00:00+00'
    execute(
        'UPDATE testapp_band SET _deleted_at = %s WHERE id = %s', params=[stamp, two_bands[0].pk]
    )

    stamps = {
        scalar(f'SELECT _deleted_at FROM {table} WHERE _deleted_at IS NOT NULL')
        for table in ('testapp_band', 'testapp_album', 'testapp_merch')
    }
    assert len(stamps) == 1 and None not in stamps


def test_a_child_archived_earlier_keeps_its_own_stamp(two_bands):
    album = Album._all_objects.get(band=two_bands[0])
    execute(
        "UPDATE testapp_album SET _deleted_at = '2019-05-05 00:00:00+00' WHERE id = %s",
        params=[album.pk],
    )

    execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[two_bands[0].pk])

    assert str(
        scalar('SELECT _deleted_at FROM testapp_album WHERE id = %s', [album.pk])
    ).startswith('2019-05-05')


def test_an_update_that_moves_no_deleted_at_cascades_nothing(two_bands):
    execute('UPDATE testapp_band SET name = name || %s', params=['!'])

    assert (_live(Band), _live(Album), _live(Merch)) == (2, 2, 2)


def test_the_hard_deletion_switch_stops_the_arms(two_bands):
    """The same switch every rule reads: while it is on, nothing cascades."""
    with transaction.atomic():
        execute("SELECT set_config('rules.hard_deletion', 'on', TRUE)")
        execute(
            'UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[two_bands[0].pk]
        )

    assert (_live(Band), _live(Album), _live(Merch)) == (1, 2, 2)


def test_a_key_moving_in_the_same_statement_still_finds_its_children(db):
    """The arm reads the owner's *before* image for the key, as the rule read ``old.``: a child
    pointing at the old code is found, where the after image would find none."""
    catalog = Catalog.objects.create(code='A')
    Listing.objects.create(catalog=catalog, name='one')

    execute(
        "UPDATE testapp_catalog SET code = 'B', _deleted_at = NOW() WHERE id = %s",
        params=[catalog.pk],
    )

    assert Listing.objects.count() == 0
    # The deferred foreign key is left dangling by the move; settled so teardown can check it.
    execute("UPDATE testapp_listing SET catalog_id = 'B'")


def test_the_revive_still_clears_what_the_archive_took(two_bands):
    execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[two_bands[0].pk])
    execute('UPDATE testapp_band SET _deleted_at = NULL WHERE id = %s', params=[two_bands[0].pk])

    assert (_live(Band), _live(Album), _live(Merch)) == (2, 2, 2)


@pytest.mark.django_db(transaction=True)
def test_a_cascaded_child_is_stamped_whatever_the_trigger_depth():
    """The arm's ``UPDATE`` runs at depth 1; the row trigger has no guard to suppress it."""
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='2112', band=band)
    before = scalar('SELECT _updated_at FROM testapp_album WHERE id = %s', [album.pk])

    execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[band.pk])

    assert scalar('SELECT _updated_at FROM testapp_album WHERE id = %s', [album.pk]) > before


def test_no_cascade_or_owned_rule_is_left_in_the_database(db):
    """Retired by 0085, which is what ``migrate`` ran: only each table's own ``soft_delete``
    (and the MTI redirect) remain among ``soft_delete*`` rules that fire on ``UPDATE``."""
    left = rows(
        "SELECT rulename FROM pg_rules WHERE tablename LIKE 'testapp\\_%' "
        "AND (rulename LIKE 'soft\\_delete\\_related%' OR rulename LIKE 'soft\\_delete\\_owned%')"
    )

    assert left == []


def test_the_migrations_reverse_rebuilds_the_rules_and_the_revive_only_trigger(db):
    """Unapplying 0085 leaves no gap: every rule it dropped is back, as 2.18 wrote it, and the
    owner's trigger is the revive-only one again. Run off the migration's own ``reverse_sql``."""
    module = import_module('tests.testapp.migrations.0085_auto_enforcement')
    legacy, current = (
        'soft_delete_revive_on_12_testapp_band',
        'soft_delete_cascade_on_12_testapp_band',
    )

    with transaction.atomic():
        for operation in reversed(module.Migration.operations):
            with connection.cursor() as cursor:
                cursor.execute(operation.reverse_sql)
        rules = scalar(
            "SELECT count(*) FROM pg_rules WHERE tablename LIKE 'testapp\\_%' "
            "AND (rulename LIKE 'soft\\_delete\\_related%' OR rulename LIKE 'soft\\_delete\\_owned%')"
        )
        body = scalar('SELECT prosrc FROM pg_proc WHERE proname = %s', [legacy])
        gone = scalar('SELECT count(*) FROM pg_proc WHERE proname = %s', [current])
        transaction.set_rollback(True)

    assert rules > 0
    assert 'guitars_archived' not in body and 'guitars_revived' in body
    assert gone == 0


def test_archiving_a_row_while_rewriting_its_key_is_refused_over_a_live_child(two_bands):
    """The rule read ``old.`` per row; the arm pairs a row across the statement on its primary
    key, which this statement moves. Refused, as the self cascade refuses it (ADR 0018), rather
    than leaving the album live under an archived band."""
    with pytest.raises(NotSupportedError, match='primary key it also rewrote'):
        with transaction.atomic():
            execute(
                'UPDATE testapp_band SET id = id + 1000, _deleted_at = NOW() WHERE id = %s',
                params=[two_bands[0].pk],
            )

    assert Album.objects.filter(band=two_bands[0]).count() == 1


def test_a_key_rewrite_archiving_a_row_nothing_holds_is_allowed(db):
    """Nothing is left live, so there is nothing to refuse: only a live child holding a key is a
    leak."""
    band = Band.objects.create(name='Childless')

    execute(
        'UPDATE testapp_band SET id = id + 1000, _deleted_at = NOW() WHERE id = %s',
        params=[band.pk],
    )

    assert Band.objects.filter(pk=band.pk + 1000).count() == 0


def test_a_key_rewrite_that_archives_nothing_is_not_refused(db):
    band = Band.objects.create(name='Childless')

    execute('UPDATE testapp_band SET id = id + 1000 WHERE id = %s', params=[band.pk])

    assert Band.objects.filter(pk=band.pk + 1000).count() == 1


def test_rewriting_the_key_of_an_already_archived_row_is_not_refused(two_bands):
    """No live child holds its key: archived with it, the children are not a leak."""
    execute('UPDATE testapp_band SET _deleted_at = NOW() WHERE id = %s', params=[two_bands[0].pk])

    with transaction.atomic():
        execute('SET CONSTRAINTS ALL DEFERRED')
        execute('UPDATE testapp_band SET id = id + 1000 WHERE id = %s', params=[two_bands[0].pk])
        execute(
            'UPDATE testapp_album SET band_id = band_id + 1000 WHERE band_id = %s',
            params=[two_bands[0].pk],
        )


def test_the_refusal_reads_a_joined_key_too(db):
    """``TouringFestival.promoter`` keeps its ``_deleted_at`` one table up, on ``Festival``: the
    joined form of the check finds the live descendant through its parent link."""
    with tenancy_bypassed():
        label = Label.objects.create(name='Roadshow')
        TouringFestival.objects.create(name='Tour', market=label, promoter=label)

        with pytest.raises(NotSupportedError, match='primary key it also rewrote'):
            with transaction.atomic():
                execute(
                    'UPDATE testapp_label SET id = id + 1000, _deleted_at = NOW() WHERE id = %s',
                    params=[label.pk],
                )


# ---- A name holding ``$$`` (#80): the arm is spliced into a dollar-quoted body, which it would close.


def _forward(operation: str):
    from django.db import migrations  # noqa: PLC0415

    source = operation[operation.index('migrations.RunSQL') :].rstrip().rstrip(',')
    return eval(source, {'migrations': migrations})  # noqa: S307 - our own output


@isolate_apps('tests.testapp')
def _dollar_models():
    class DollarOwner(SetarModel):
        class Meta:
            app_label = 'testapp'
            db_table = 'testapp_dollar$$owner'

    class DollarChild(SetarModel):
        owner = models.ForeignKey(DollarOwner, on_delete=CASCADE, related_name='+')

        class Meta:
            app_label = 'testapp'
            db_table = 'testapp_dollar$$child'

    return DollarOwner, DollarChild


def test_the_tag_is_dollar_dollar_unless_a_name_holds_it():
    quote = operations_module._dollar_quote

    assert quote('"a"', '"b"') == '$$'
    assert quote('"a$$b"') == '$guitars$'
    assert quote('"a$$b"', '"$guitars$"') == '$guitars1$'


def test_a_key_naming_dollar_quoting_is_still_a_cascade_not_a_refusal():
    """The rule needed no quoting and the arm gets another tag, so no key is left without its
    cascade -- a refusal here retired a recorded rule that still worked."""
    owner, child = _dollar_models()

    kind = classify_cascade(child, child._meta.get_field('owner'), CASCADE, owner._meta.db_table)

    assert kind is CascadeKind.RULE


def test_an_arm_naming_dollar_quoting_makes_a_function_the_database_accepts(db):
    """Rendered into the real template and executed: with ``$$`` this is a syntax error, and the
    body names the child both in an arm and in the leak check."""
    owner, child = _dollar_models()
    command = Command()
    key = (child._meta.db_table, owner._meta.db_table, 'owner_id')
    arms = {
        'arms': command._revive_arm(key, child, 'owner_id', 'id'),
        'archive_arms': command._archive_arm(key, child, 'owner_id', 'id'),
        'leak_checks': command._leak_check(key, child, 'owner_id', 'id'),
    }
    slots = {'function': '"dol$$fn"', 'primary_key': 'id', **arms}
    slots['dollar'] = operations_module._dollar_quote(*slots.values())

    sql = operations_module._soft_delete._CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION.format(**slots)

    assert slots['dollar'] == '$guitars$' and 'dollar$$child' in sql
    with transaction.atomic():
        execute(sql)
        assert scalar("SELECT count(*) FROM pg_proc WHERE proname = 'dol$$fn'") == 1
        transaction.set_rollback(True)


def test_a_cascade_trigger_whose_own_name_holds_it_is_written_and_runs(db, monkeypatch):
    monkeypatch.setattr(
        operations_module,
        '_cascade_owner_name',
        lambda table: _identifiers._safe_ident('c$$' + table),
    )
    command = Command()
    command.existing.soft_delete_cascade_owner.clear()
    (operation,) = [
        op
        for op in command._revive_operations(django_apps.get_app_config('testapp'))
        if op.startswith('# Soft Delete Cascade Trigger on "testapp_band"')
    ]
    sql = _forward(operation).sql

    assert '$guitars$' in sql
    with transaction.atomic():
        execute(sql)
        assert scalar("SELECT count(*) FROM pg_proc WHERE proname = 'c$$testapp_band'") == 1
        transaction.set_rollback(True)


def test_an_owned_sweep_whose_name_holds_it_is_written_and_runs(db, monkeypatch):
    monkeypatch.setattr(
        operations_module,
        '_owned_sweep_name',
        lambda owner, dependent, fk: _identifiers._safe_ident(f's$${fk}'),
    )
    command = Command()
    command.existing.soft_delete_owned.clear()
    command.existing.soft_delete_owned_sweep.clear()
    sweeps = [
        _forward(op).sql
        for op in command._owned_operations(Album)
        if op.startswith('# Soft Delete Owned Sweep')
    ]

    assert len(sweeps) == 2 and all('$guitars$' in sql for sql in sweeps)
    with transaction.atomic():
        for sql in sweeps:
            execute(sql)
        assert scalar("SELECT count(*) FROM pg_proc WHERE proname LIKE 's$$%'") == 2
        transaction.set_rollback(True)


def test_a_key_reused_inside_the_statement_does_not_hide_the_refusal(two_bands):
    """Rush's id moves and is archived while Yes takes Rush's old id: no key vanishes from the
    after image, yet Rush's live album would be left holding what is now Yes."""
    rush, yes = two_bands

    with pytest.raises(NotSupportedError, match='primary key it also rewrote'):
        with transaction.atomic():
            execute(
                'UPDATE testapp_band SET id = CASE WHEN id = %s THEN %s ELSE %s END, '
                '_deleted_at = CASE WHEN id = %s THEN NOW() END WHERE id IN (%s, %s)',
                params=[rush.pk, rush.pk + 1000, rush.pk, rush.pk, rush.pk, yes.pk],
            )

    assert Album.objects.filter(band=rush).count() == 1


@isolate_apps('tests.testapp')
def _dollar_tree():
    class DollarTree(SetarModel):
        parent = models.ForeignKey('self', on_delete=CASCADE, null=True, related_name='+')

        class Meta:
            app_label = 'testapp'
            db_table = 'testapp_dollar$$tree'

    return DollarTree


def test_a_self_key_naming_dollar_quoting_is_an_arm_the_database_accepts(db):
    """The self trigger refused such a name (ADR 0018); its arm takes another dollar-quote tag
    like any other (ADR 0042), so a key onto the owner's own table is never left without one."""
    tree = _dollar_tree()
    command = Command()
    table = tree._meta.db_table
    key = (table, table, 'parent_id')
    arms = {
        'arms': command._revive_arm(key, tree, 'parent_id', 'id'),
        'archive_arms': command._archive_arm(key, tree, 'parent_id', 'id'),
        'leak_checks': command._leak_check(key, tree, 'parent_id', 'id'),
    }
    slots = {'function': '"dol$$self"', 'primary_key': 'id', **arms}
    slots['dollar'] = operations_module._dollar_quote(*slots.values())

    sql = operations_module._soft_delete._CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION.format(**slots)

    assert slots['dollar'] == '$guitars$' and 'guitars_new."id"' in sql
    with transaction.atomic():
        execute(sql)
        assert scalar("SELECT count(*) FROM pg_proc WHERE proname = 'dol$$self'") == 1
        transaction.set_rollback(True)


def test_a_column_the_model_declares_no_key_on_falls_back_to_the_owners_primary_key():
    """``_referenced_key`` reads the key's own ``to_field``; with no such key it answers as
    every key into the primary key does, untouched."""
    assert operations_module._referenced_key(Band, 'no_such_id', '"id"') == '"id"'
