"""What a cascade writes into the child's ``_deleted_at`` (issue #53): the parent's own value,
and only into a child that was still live. Every timestamp here is an **explicit** one, never
``NOW()`` -- a test whose equality comes from the clock reads pytest-django, not the rule."""

from datetime import datetime, timezone

import pytest
from django.db.models import Case, DateTimeField, Value, When

from tests.testapp.models import Album, Band, Merch, PressKit


#: Distinct and far apart, so an assertion cannot pass by two clock reads coinciding.
T_CHILD = datetime(2020, 1, 1, tzinfo=timezone.utc)
T_PARENT = datetime(2021, 6, 15, 12, 30, tzinfo=timezone.utc)


def _archive(model, pk, at):
    """Archive one row at an explicit instant, through the manager that can see it."""
    model._all_objects.filter(pk=pk).update(_deleted_at=at)


def _deleted_at(model, pk):
    return model._all_objects.get(pk=pk)._deleted_at


def models_case(first_pk, when_first, otherwise):
    """``CASE WHEN id = first_pk THEN … ELSE … END`` -- one statement, two values, which is
    the only way to tell a per-row copy from a single evaluated ``NOW()``."""
    return Case(
        When(pk=first_pk, then=Value(when_first)),
        default=Value(otherwise),
        output_field=DateTimeField(),
    )


@pytest.mark.django_db(transaction=True)
def test_a_cascaded_child_carries_its_parents_timestamp():
    """``new._deleted_at``, not ``NOW()``: one archive is one instant, whoever wrote it. The
    parent is archived from Python here, which is the path the two used to disagree on."""
    band = Band.objects.create(name='Rush')
    album = Album.objects.create(title='Hemispheres', band=band)

    _archive(Band, band.pk, T_PARENT)

    assert _deleted_at(Album, album.pk) == T_PARENT


@pytest.mark.django_db(transaction=True)
def test_a_child_archived_earlier_keeps_its_own_timestamp():
    """The bug. Without ``AND _deleted_at IS NULL`` the cascade re-stamped a row that was
    already gone, losing when it went -- and with it the only evidence of *what* archived it."""
    band = Band.objects.create(name='Yes')
    album = Album.objects.create(title='Fragile', band=band)

    _archive(Album, album.pk, T_CHILD)
    _archive(Band, band.pk, T_PARENT)

    assert _deleted_at(Album, album.pk) == T_CHILD


@pytest.mark.django_db(transaction=True)
def test_the_parents_timestamp_chains_to_a_grandchild():
    """Each level's rule fires on the level above's ``UPDATE`` and copies the same value, so
    depth needs no separate mechanism -- and a whole archive reads as one instant."""
    band = Band.objects.create(name='Genesis')
    album = Album.objects.create(title='Foxtrot', band=band)
    merch = Merch.objects.create(description='poster', album=album)

    _archive(Band, band.pk, T_PARENT)

    assert _deleted_at(Album, album.pk) == T_PARENT
    assert _deleted_at(Merch, merch.pk) == T_PARENT


@pytest.mark.django_db(transaction=True)
def test_a_grandchild_archived_earlier_is_spared_at_every_depth():
    band = Band.objects.create(name='Camel')
    album = Album.objects.create(title='Mirage', band=band)
    merch = Merch.objects.create(description='patch', album=album)

    _archive(Merch, merch.pk, T_CHILD)
    _archive(Band, band.pk, T_PARENT)

    assert _deleted_at(Album, album.pk) == T_PARENT
    assert _deleted_at(Merch, merch.pk) == T_CHILD


@pytest.mark.django_db(transaction=True)
def test_the_via_rule_behaves_the_same():
    """``Merch.bonus_album`` is the second CASCADE key to ``Album``, so it takes the ``_VIA``
    form -- a separate template until 1.1.0, and a separate rule on the same table still."""
    band = Band.objects.create(name='Gong')
    album = Album.objects.create(title='Angel', band=band)
    carried = Merch.objects.create(description='carried', bonus_album=album)
    spared = Merch.objects.create(description='spared', bonus_album=album)

    _archive(Merch, spared.pk, T_CHILD)
    _archive(Album, album.pk, T_PARENT)

    assert _deleted_at(Merch, carried.pk) == T_PARENT
    assert _deleted_at(Merch, spared.pk) == T_CHILD


@pytest.mark.django_db(transaction=True)
def test_one_statement_archiving_two_parents_gives_each_child_its_own():
    """The rule's action expands into the caller's ``UPDATE`` as a join, so a multi-row
    statement is not one value broadcast -- each child reads the row that archived it."""
    first = Band.objects.create(name='Soft Machine')
    second = Band.objects.create(name='Hatfield')
    early = Album.objects.create(title='Third', band=first)
    late = Album.objects.create(title='Rotters', band=second)

    Band._all_objects.filter(pk__in=[first.pk, second.pk]).update(
        _deleted_at=models_case(first.pk, T_CHILD, T_PARENT)
    )

    assert _deleted_at(Album, early.pk) == T_CHILD
    assert _deleted_at(Album, late.pk) == T_PARENT


@pytest.mark.django_db(transaction=True)
def test_the_owned_family_was_already_correct():
    """Kept beside the others so the contrast that found the bug stays visible: the owned rule
    has carried ``AND _deleted_at IS NULL`` since 2.3.0, and is untouched by this fix."""
    kit = PressKit.objects.create(headline='spared')
    band = Band.objects.create(name='Egg')
    album = Album.objects.create(title='Polite', band=band, press_kit=kit)

    _archive(PressKit, kit.pk, T_CHILD)
    album.delete()

    assert _deleted_at(PressKit, kit.pk) == T_CHILD
