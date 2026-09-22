"""The inverse cascade (issue #51): clearing a parent's ``_deleted_at`` revives the children
that archive took, and only those. Provenance is the archive timestamp, so every test archives
at an **explicit** instant -- except ``test_the_ordinary_orm_path_revives_too``, its subject."""

from datetime import datetime, timezone

import pytest
from django.db.models import Case, DateTimeField, Value, When

from tests.testapp.models import Album, Band, Merch, Setlist, SetlistEntry


T_CHILD = datetime(2020, 1, 1, tzinfo=timezone.utc)
T_PARENT = datetime(2021, 6, 15, 12, 30, tzinfo=timezone.utc)
T_OTHER = datetime(2022, 3, 3, tzinfo=timezone.utc)


def _archive(model, pk, at):
    model._all_objects.filter(pk=pk).update(_deleted_at=at)


def _revive(model, *pks):
    """Through ``_all_objects``: ``objects`` filters archived rows out of the ``WHERE`` as well
    as the ``SELECT``, so the same statement through it matches nothing and reports nothing."""
    model._all_objects.filter(pk__in=pks).update(_deleted_at=None)


def _stamp(model, pk):
    return model._all_objects.get(pk=pk)._deleted_at


@pytest.mark.django_db(transaction=True)
def test_reviving_a_parent_revives_the_children_it_archived():
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='Hemispheres', band=band)
    _archive(Band, band.pk, T_PARENT)

    _revive(Band, band.pk)

    assert _stamp(Album, album.pk) is None


@pytest.mark.django_db(transaction=True)
def test_a_child_archived_independently_is_not_revived():
    """The assertion the whole design exists for. A naive predicate flip would resurrect a row
    the caller archived deliberately, which fails toward *exposing* data -- worse than the
    silent stranding it replaces. The guard is the archive timestamp, which ADR 0023 made exact."""
    band = Band.objects.create(name='Yes')
    album = Album.objects.create(title='Fragile', band=band)
    _archive(Album, album.pk, T_CHILD)
    _archive(Band, band.pk, T_PARENT)

    _revive(Band, band.pk)

    assert _stamp(Album, album.pk) == T_CHILD


@pytest.mark.django_db(transaction=True)
def test_the_revive_chains_to_a_grandchild():
    band = Band.objects.create(name='Genesis')
    album = Album.objects.create(title='Foxtrot', band=band)
    merch = Merch.objects.create(description='poster', album=album)
    _archive(Band, band.pk, T_PARENT)

    _revive(Band, band.pk)

    assert _stamp(Album, album.pk) is None
    assert _stamp(Merch, merch.pk) is None


@pytest.mark.django_db(transaction=True)
def test_a_grandchild_archived_independently_is_spared_at_depth():
    band = Band.objects.create(name='Camel')
    album = Album.objects.create(title='Mirage', band=band)
    merch = Merch.objects.create(description='patch', album=album)
    _archive(Merch, merch.pk, T_CHILD)
    _archive(Band, band.pk, T_PARENT)

    _revive(Band, band.pk)

    assert _stamp(Album, album.pk) is None
    assert _stamp(Merch, merch.pk) == T_CHILD


@pytest.mark.django_db(transaction=True)
def test_the_ordinary_orm_path_revives_too():
    """``.delete()`` is what a consumer actually runs, and it works for a different reason:
    ``Collector`` archives children first through their own rule and then the parent, both at
    one ``transaction_timestamp()``, so the values agree by coincidence rather than by the copy."""
    band = Band.objects.create(name='Gong')
    album = Album.objects.create(title='Angel', band=band)
    band_pk = band.pk
    band.delete()
    assert _stamp(Album, album.pk) == _stamp(Band, band_pk)

    _revive(Band, band_pk)

    assert _stamp(Album, album.pk) is None


@pytest.mark.django_db(transaction=True)
def test_the_via_rule_revives_its_own_relation():
    """``Merch.bonus_album`` is the second CASCADE key to ``Album``, so it takes the ``_VIA``
    form -- a second rule on the same table, which needs its own inverse."""
    band = Band.objects.create(name='Egg')
    album = Album.objects.create(title='Polite', band=band)
    carried = Merch.objects.create(description='carried', bonus_album=album)
    spared = Merch.objects.create(description='spared', bonus_album=album)
    _archive(Merch, spared.pk, T_CHILD)
    _archive(Album, album.pk, T_PARENT)

    _revive(Album, album.pk)

    assert _stamp(Merch, carried.pk) is None
    assert _stamp(Merch, spared.pk) == T_CHILD


@pytest.mark.django_db(transaction=True)
def test_one_statement_reviving_two_parents_takes_only_each_ones_own():
    """No statement-level sweep is needed here, unlike the owned family: the predicate is
    per-pair and reads nothing the statement changes but the child's own column."""
    first = Band.objects.create(name='Soft Machine')
    second = Band.objects.create(name='Hatfield')
    early = Album.objects.create(title='Third', band=first)
    late = Album.objects.create(title='Rotters', band=second)
    Band._all_objects.filter(pk__in=[first.pk, second.pk]).update(
        _deleted_at=Case(
            When(pk=first.pk, then=Value(T_PARENT)),
            default=Value(T_OTHER),
            output_field=DateTimeField(),
        )
    )

    _revive(Band, first.pk, second.pk)

    assert _stamp(Album, early.pk) is None
    assert _stamp(Album, late.pk) is None


@pytest.mark.django_db(transaction=True)
def test_a_child_of_another_parent_archived_at_the_same_instant_is_not_revived():
    """The **correlation**, which timestamp matching alone does not give. Every other test
    here spares its bystander by stamp, so stripping ``"band_id" = old."id"`` from the rule
    leaves them all green -- this one archives the bystander's parent at the *same* instant."""
    revived = Band.objects.create(name='Gong')
    untouched = Band.objects.create(name='Henry Cow')
    mine = Album.objects.create(title='Angel', band=revived)
    theirs = Album.objects.create(title='Legend', band=untouched)
    # One instant for both families of rows, so only the foreign key can tell them apart.
    Band._all_objects.filter(pk__in=[revived.pk, untouched.pk]).update(_deleted_at=T_PARENT)
    assert _stamp(Album, mine.pk) == _stamp(Album, theirs.pk) == T_PARENT

    _revive(Band, revived.pk)

    assert _stamp(Album, mine.pk) is None
    # Its parent is still archived, so reviving it would expose a row under a dead parent.
    assert _stamp(Album, theirs.pk) == T_PARENT


@pytest.mark.django_db(transaction=True)
def test_a_re_stamped_parent_can_no_longer_revive_its_children():
    """The one hole the timestamp copy opens rather than closes. Moving an already-archived
    parent's ``_deleted_at`` fires neither rule -- ``old`` and ``new`` are both ``IS NOT NULL``
    -- so the children keep the old value and a later revive matches none. Fails toward hiding."""
    band = Band.objects.create(name='Henry Cow')
    album = Album.objects.create(title='Legend', band=band)
    _archive(Band, band.pk, T_PARENT)
    _archive(Band, band.pk, T_OTHER)

    _revive(Band, band.pk)

    assert _stamp(Album, album.pk) == T_PARENT


@pytest.mark.django_db(transaction=True)
def test_the_self_referential_family_stays_archive_only():
    """A self-referential CASCADE key takes a trigger (ADR 0018) and gains no inverse, while
    its ordinary CASCADE children do revive -- the split. The descendant does not even carry the
    root's timestamp: that trigger still writes ``NOW()``, so no provenance test could match."""
    root = Setlist.objects.create(title='root')
    child = Setlist.objects.create(title='child', parent=root)
    entry = SetlistEntry.objects.create(song='entry', setlist=root)
    _archive(Setlist, root.pk, T_PARENT)
    assert _stamp(Setlist, child.pk) != T_PARENT

    _revive(Setlist, root.pk)

    assert _stamp(SetlistEntry, entry.pk) is None
    assert _stamp(Setlist, child.pk) is not None


@pytest.mark.django_db(transaction=True)
def test_a_revive_through_the_live_manager_changes_nothing_and_says_nothing():
    """The issue's secondary item, confirmed. ``LiveManager.get_queryset()`` filters
    ``_deleted_at__isnull=True``, and ``bulk_update`` builds its ``WHERE`` from the queryset --
    so a revive through ``objects`` matches no row, returns 0, and raises nothing."""
    band = Band.objects.create(name='Gentle Giant')
    album = Album.objects.create(title='Acquiring', band=band)
    _archive(Band, band.pk, T_PARENT)

    stale = Band._all_objects.get(pk=band.pk)
    stale._deleted_at = None
    assert Band.objects.bulk_update([stale], ['_deleted_at']) == 0

    assert _stamp(Band, band.pk) == T_PARENT
    assert _stamp(Album, album.pk) == T_PARENT


@pytest.mark.django_db(transaction=True)
def test_the_same_revive_through_all_objects_works_and_fires_the_rule():
    """Which is what ties the secondary item to the primary one: the manager a caller has to
    remember is the one the rule needs them on."""
    band = Band.objects.create(name='Gryphon')
    album = Album.objects.create(title='Red Queen', band=band)
    _archive(Band, band.pk, T_PARENT)

    stale = Band._all_objects.get(pk=band.pk)
    stale._deleted_at = None
    assert Band._all_objects.bulk_update([stale], ['_deleted_at']) == 1

    assert _stamp(Band, band.pk) is None
    assert _stamp(Album, album.pk) is None


def test_the_retirement_reverse_splices_updated_at_only_where_the_child_owns_it():
    """The reverse rebuilds the trigger, so it has to ask the same question the forward asked.
    A table mapping to no model, and a child without the column, both answer "no splice"."""
    from guitars.management.enforcement.command import Command

    command = Command()

    assert command._revive_updated_at('no_such_table') == ''
    # `testapp_riff` is a TarModel: no `_updated_at` of its own to stamp.
    assert command._revive_updated_at('testapp_riff') == ''
    assert command._revive_updated_at('testapp_album') == ', _updated_at = NOW()'
