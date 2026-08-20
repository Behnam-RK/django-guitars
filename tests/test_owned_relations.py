"""Tests for owner-side soft-delete ownership (``OwningForeignKey``, 2.3.0): the rule that
fires when the *owner* holds the foreign key, and the last-owner guard that keeps it from
archiving something a sibling owner still points at."""

import pytest
from django.apps import apps as django_apps
from django.db import models
from django.db.models import CASCADE, PROTECT, SET_NULL
from django.test.utils import isolate_apps

from guitars.introspection import rule_update_cycle_edges
from guitars.models import OwningForeignKey, SetarModel
from guitars.models.soft_deletion import _owned_fields
from tests.testapp.models import Album, Band, Ensemble, Merch, Orchestra, PressKit


@pytest.fixture
def band(db):
    return Band.objects.create(name='Rush')


# ─── the field itself ───


def _owner_field_errors(on_delete) -> list[str]:
    """Check-ids raised by an ``OwningForeignKey`` declared with *on_delete*, in a throwaway
    app registry so neither model outlives the call."""

    @isolate_apps('tests.testapp')
    def _build() -> list[str]:
        class Kit(models.Model):
            class Meta:
                app_label = 'testapp'

        class Owner(models.Model):
            kit = OwningForeignKey(Kit, on_delete=on_delete)

            class Meta:
                app_label = 'testapp'

        return [error.id for error in Owner._meta.get_field('kit').check()]

    return _build()


def test_owning_foreign_key_refuses_on_delete_cascade():
    """CASCADE says deleting the target deletes this row -- the opposite of ownership, and
    the direction the inbound cascade rule would be emitted in."""
    assert _owner_field_errors(CASCADE) == ['guitars.E001']


def test_owning_foreign_key_accepts_a_non_cascade_on_delete():
    assert _owner_field_errors(PROTECT) == []


def test_owning_foreign_key_refuses_a_non_primary_key_to_field():
    """The rule correlates ``old."<fk>"`` against the dependent's *primary* key -- the same
    thing that makes ownership into an MTI child work -- so any other target stamps the
    wrong row, or a row of a type the comparison cannot even be made against."""

    @isolate_apps('tests.testapp')
    def _build() -> list[str]:
        class Kit(models.Model):
            slug = models.CharField(max_length=10, unique=True)

            class Meta:
                app_label = 'testapp'

        class Owner(models.Model):
            kit = OwningForeignKey(Kit, on_delete=PROTECT, to_field='slug')

            class Meta:
                app_label = 'testapp'

        return [error.id for error in Owner._meta.get_field('kit').check()]

    assert _build() == ['guitars.E002']


def test_owning_foreign_key_reports_an_unresolvable_to_field_rather_than_raising():
    """``to_field`` naming nothing is Django's own ``fields.E312``. Resolving the target
    field to compare it against the primary key raises ``FieldDoesNotExist``, which would
    escape the check framework and replace every reported error with a traceback."""

    @isolate_apps('tests.testapp')
    def _build() -> list[str]:
        class Kit(models.Model):
            class Meta:
                app_label = 'testapp'

        class Owner(models.Model):
            kit = OwningForeignKey(Kit, on_delete=PROTECT, to_field='nope')

            class Meta:
                app_label = 'testapp'

        return [error.id for error in Owner._meta.get_field('kit').check()]

    assert _build() == ['fields.E312']


def test_hard_delete_does_not_follow_an_owned_relation_the_generator_refused():
    """The Python twin of the generator's candidate test. A self-owning relation carries no
    rule -- it would be infinite rule recursion -- so following it here would remove exactly
    what the soft-delete path left alone."""

    @isolate_apps('tests.testapp')
    def _build() -> list:
        class SelfOwner(SetarModel):
            previous = OwningForeignKey('self', on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        return _owned_fields(SelfOwner)

    assert _build() == []


def test_owning_foreign_key_accepts_a_target_reached_through_mti():
    """``Merch.featured_orchestra`` resolves to Orchestra's parent-link primary key, which
    *is* its primary key -- the E002 guard must not read that as a redirected ``to_field``."""
    assert Merch._meta.get_field('featured_orchestra').check() == []


def test_owning_foreign_key_deconstructs_to_its_public_path():
    """A generated migration records the path literally and is already applied in consuming
    projects, so it names ``guitars.models``, not the module the class happens to live in."""
    _name, path, _args, _kwargs = Album._meta.get_field('press_kit').deconstruct()

    assert path == 'guitars.models.OwningForeignKey'


# ─── the rule ───


@pytest.mark.django_db
def test_soft_deleting_the_owner_soft_deletes_what_it_owns(band):
    kit = PressKit.objects.create(headline='Hemispheres, reissued')
    album = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)

    album.delete()

    assert not PressKit.objects.filter(pk=kit.pk).exists()
    assert PressKit._archives.filter(pk=kit.pk).exists()


@pytest.mark.django_db
def test_a_null_owned_foreign_key_is_a_no_op(band):
    """``WHERE id = old.press_kit_id`` matches nothing when the column is NULL, which is why
    a nullable owned relation needs no guard of its own."""
    kit = PressKit.objects.create(headline='Unclaimed')
    album = Album.objects.create(title='Hemispheres', band=band)

    album.delete()

    assert PressKit.objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db
def test_a_surviving_owner_keeps_the_owned_row_alive(band):
    """The last-owner guard. Emitted unconditionally rather than only where a unique
    constraint proves single ownership -- constraint-shaped SQL goes silently wrong the day
    the constraint is dropped."""
    kit = PressKit.objects.create(headline='Shared')
    first = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)

    first.delete()

    assert PressKit.objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db
def test_the_last_owner_going_soft_deletes_the_shared_row(band):
    kit = PressKit.objects.create(headline='Shared')
    first = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    second = Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)

    first.delete()
    second.delete()

    assert PressKit._archives.filter(pk=kit.pk).exists()


@pytest.mark.django_db
def test_an_already_archived_owned_row_is_not_restamped(band):
    """``AND _deleted_at IS NULL`` on the dependent: a second owner going must not move an
    archived row's timestamp forward. Archived through the *other* owning column -- deleting
    the kit itself would have ``SET_NULL`` clear both keys, leaving the guard untested."""
    kit = PressKit.objects.create(headline='Shared')
    kit_pk = kit.pk  # Model.delete() clears the instance's pk
    first = Album.objects.create(title='Hemispheres', band=band, alt_press_kit=kit)
    second = Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)
    first.delete()  # the only alt_press_kit owner, so the kit is stamped now
    stamped_at = PressKit._archives.get(pk=kit_pk)._deleted_at
    assert stamped_at is not None

    second.delete()

    assert PressKit._archives.get(pk=kit_pk)._deleted_at == stamped_at


@pytest.mark.django_db
def test_two_owned_foreign_keys_to_one_table_each_get_their_own_rule(band):
    """Album owns through both ``press_kit`` and ``alt_press_kit``. A shared rule name would
    have left one of these two rows alive with no error anywhere."""
    kit = PressKit.objects.create(headline='Primary')
    alt = PressKit.objects.create(headline='Alternate')
    album = Album.objects.create(title='Hemispheres', band=band, press_kit=kit, alt_press_kit=alt)

    album.delete()

    assert PressKit._archives.filter(pk=kit.pk).exists()
    assert PressKit._archives.filter(pk=alt.pk).exists()


@pytest.mark.django_db
def test_the_rule_reaches_an_owned_row_whose_deleted_at_lives_on_an_mti_ancestor(band):
    """``Merch.featured_orchestra`` points at an MTI child; the stamp belongs on Ensemble,
    correlated by the primary-key value every table in the chain shares."""
    orchestra = Orchestra.objects.create(name='LSO', conductor='Davis')
    album = Album.objects.create(title='Hemispheres', band=band)
    merch = Merch.objects.create(
        description='Tour shirt', album=album, featured_orchestra=orchestra
    )

    merch.delete()

    assert not Orchestra.objects.filter(pk=orchestra.pk).exists()
    assert Ensemble._archives.filter(pk=orchestra.pk).exists()


@pytest.mark.django_db
def test_one_statement_deleting_every_owner_leaves_the_shared_row_alive(band):
    """Per *statement*: a rule action runs before the original update, so each owner still
    reads as live to its siblings' guards. Pinned because it fails safe, and because lifting
    it means a statement-level trigger rather than a rule."""
    kit = PressKit.objects.create(headline='Shared')
    Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)

    Album.objects.filter(press_kit=kit).delete()  # one statement, both owners

    assert not Album.objects.filter(press_kit=kit).exists()  # the owners did go
    assert PressKit.objects.filter(pk=kit.pk).exists()  # the shared target did not


@pytest.mark.django_db
def test_one_statement_still_stamps_owned_rows_that_are_not_shared(band):
    """Control for the above: the limit is the shared guard, not multi-row statements. The
    same single DELETE over two owners of two *distinct* targets stamps both."""
    first_kit = PressKit.objects.create(headline='One')
    second_kit = PressKit.objects.create(headline='Two')
    Album.objects.create(title='Hemispheres', band=band, press_kit=first_kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=second_kit)

    Album.objects.filter(press_kit__in=[first_kit, second_kit]).delete()

    assert PressKit._archives.filter(pk=first_kit.pk).exists()
    assert PressKit._archives.filter(pk=second_kit.pk).exists()


@pytest.mark.django_db
def test_the_hard_deletion_switch_suppresses_the_owned_rule(band):
    """``hard_delete()`` opts out of every rule the same way, so the owned rule must carry
    the identical ``<> 'on'`` guard -- otherwise it would stamp a row about to be removed."""
    kit = PressKit.objects.create(headline='Doomed')
    album = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)

    Album._all_objects.filter(pk=album.pk).hard_delete()

    assert PressKit.objects.filter(pk=kit.pk).exists()  # untouched: the rule did not fire


# ─── hard_delete ───


@pytest.mark.django_db(transaction=True)
def test_hard_delete_removes_what_the_row_owned(band):
    """Without this the owned row is stranded: nothing points at it any more and no later
    cascade can reach it."""
    kit = PressKit.objects.create(headline='Doomed')
    album = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)

    album.hard_delete()

    assert not PressKit._all_objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_spares_a_row_another_live_owner_still_owns(band):
    """The Python twin of the rule's ``NOT EXISTS``: the two must agree, or ``hard_delete()``
    destroys exactly what the soft-delete path deliberately spared."""
    kit = PressKit.objects.create(headline='Shared')
    first = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)

    first.hard_delete()

    assert PressKit._all_objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_removes_a_row_owned_by_a_cascade_child(band):
    """Ownership is followed through the CASCADE collection, not only from the instance the
    call was made on: hard-deleting the band reaches the album's press kit."""
    kit = PressKit.objects.create(headline='Doomed')
    Album.objects.create(title='Hemispheres', band=band, press_kit=kit)

    band.hard_delete()

    assert not PressKit._all_objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_removes_the_whole_chain_of_an_owned_mti_row(band):
    album = Album.objects.create(title='Hemispheres', band=band)
    orchestra = Orchestra.objects.create(name='LSO', conductor='Davis')
    merch = Merch.objects.create(
        description='Tour shirt', album=album, featured_orchestra=orchestra
    )

    merch.hard_delete()

    assert not Orchestra._all_objects.filter(pk=orchestra.pk).exists()
    assert not Ensemble._all_objects.filter(pk=orchestra.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_spares_a_row_an_archived_owner_still_references(band):
    """An archived owner's foreign key is still on disk. The rule ignores it -- it only
    stamps a column -- but removing the row here would leave that key dangling and fail the
    deferred constraint at ``COMMIT``, so the target is spared and stays archived."""
    kit = PressKit.objects.create(headline='Shared')
    first = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    second = Album.objects.create(title='Permanent Waves', band=band, press_kit=kit)
    second.delete()  # soft: the row survives, still holding press_kit_id

    first.hard_delete()

    assert PressKit._all_objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_leaves_alone_what_the_generator_refused_to_own(band):
    """``Orchestra.programme`` is the refused owner-side MTI case: no rule exists, so
    soft-deleting the orchestra leaves the press kit alive. hard_delete() must agree --
    following the relation here would destroy exactly what the other path spared."""
    kit = PressKit.objects.create(headline='Programme')
    orchestra = Orchestra.objects.create(name='LSO', conductor='Davis', programme=kit)

    orchestra.hard_delete()

    assert PressKit.objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_spares_a_row_referenced_through_another_column(band):
    """The guard is per column for the *rule*, which only stamps. ``hard_delete()`` removes
    the row, so a surviving key of any kind -- here Album's second owning column -- dangles
    at ``COMMIT`` and aborts the whole transaction."""
    kit = PressKit.objects.create(headline='Shared')
    first = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Album.objects.create(title='Permanent Waves', band=band, alt_press_kit=kit)

    first.hard_delete()

    assert PressKit._all_objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_spares_a_row_a_refused_relation_still_points_at(band):
    """``Orchestra.programme`` carries no rule, but it does carry a column. Removing the kit
    would leave it dangling just the same."""
    kit = PressKit.objects.create(headline='Shared')
    album = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    Orchestra.objects.create(name='LSO', conductor='Davis', programme=kit)

    album.hard_delete()

    assert PressKit._all_objects.filter(pk=kit.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_hard_delete_returns_for_a_row_a_later_group_stopped_referencing(band):
    """``Orchestra.programme`` holds the kit back on the first pass; the orchestra is then
    collected as the merch's owned row, and the second pass comes back for the kit. Order
    counts too: the kit's group runs last, or the deferred key check fails at ``COMMIT``."""
    kit = PressKit.objects.create(headline='Shared')
    album = Album.objects.create(title='Hemispheres', band=band, press_kit=kit)
    orchestra = Orchestra.objects.create(name='LSO', conductor='Davis', programme=kit)
    Merch.objects.create(description='Tour shirt', album=album, featured_orchestra=orchestra)

    album.hard_delete()

    assert not Orchestra._all_objects.filter(pk=orchestra.pk).exists()
    assert not PressKit._all_objects.filter(pk=kit.pk).exists()


def test_rule_update_cycle_edges_reports_every_edge_on_a_cycle_and_no_other():
    """The graph both layers read. Four models form a diamond that closes back on itself,
    and a fifth points into it without being reachable from it -- the one edge that is not
    on a cycle, and so the one rule that may still be written."""

    @isolate_apps('tests.testapp')
    def _build() -> set:
        class W(SetarModel):
            back = OwningForeignKey('X', on_delete=SET_NULL, null=True, related_name='+')

            class Meta:
                app_label = 'testapp'

        class X(SetarModel):
            class Meta:
                app_label = 'testapp'

        class Y(SetarModel):
            down = OwningForeignKey(W, on_delete=SET_NULL, null=True, related_name='+')
            up = models.ForeignKey(X, on_delete=CASCADE, related_name='+')

            class Meta:
                app_label = 'testapp'

        class Z(SetarModel):
            down = OwningForeignKey(W, on_delete=SET_NULL, null=True, related_name='+')
            up = models.ForeignKey(X, on_delete=CASCADE, related_name='+')

            class Meta:
                app_label = 'testapp'

        class P(SetarModel):
            into = OwningForeignKey(X, on_delete=SET_NULL, null=True, related_name='+')

            class Meta:
                app_label = 'testapp'

        return rule_update_cycle_edges([W, X, Y, Z, P])

    # X -> {Y, Z} by cascade, Y/Z -> W by ownership, W -> X by ownership: two loops sharing
    # the X -> ... -> W -> X spine. P -> X points in, and nothing points back out to P.
    assert _build() == {
        ('testapp_x', 'testapp_y'),
        ('testapp_x', 'testapp_z'),
        ('testapp_y', 'testapp_w'),
        ('testapp_z', 'testapp_w'),
        ('testapp_w', 'testapp_x'),
    }


def test_rule_update_cycle_edges_ignores_relations_that_carry_no_rule():
    """Only what the generator would actually write is in the graph: a target that is not
    soft-deletable, a non-CASCADE key, and the structural MTI parent-link all drop out."""

    @isolate_apps('tests.testapp')
    def _build() -> set:
        class Plain(models.Model):
            class Meta:
                app_label = 'testapp'

        class Loose(SetarModel):
            owned = OwningForeignKey(Plain, on_delete=SET_NULL, null=True, related_name='+')
            weak = models.ForeignKey('Loose', on_delete=SET_NULL, null=True, related_name='+')
            tag = models.CharField(max_length=10)

            class Meta:
                app_label = 'testapp'

        class LooseChild(Loose):
            class Meta:
                app_label = 'testapp'

        return rule_update_cycle_edges([Plain, Loose, LooseChild])

    assert _build() == set()


def test_hard_delete_does_not_follow_ownership_that_closes_a_two_model_cycle(monkeypatch):
    """The multi-table twin of the self-owning refusal: neither rule is written, so neither
    relation may be followed here. ``_owned_fields`` reads the same graph the generator does
    precisely so the two cannot disagree about which relations carry a rule."""

    @isolate_apps('tests.testapp')
    def _build() -> list:
        class Left(SetarModel):
            partner = OwningForeignKey('Right', on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        class Right(SetarModel):
            partner = OwningForeignKey('Left', on_delete=SET_NULL, null=True)

            class Meta:
                app_label = 'testapp'

        # ``isolate_apps`` swaps ``Options.apps``, not the global registry ``_owned_fields``
        # reads, so neither model would otherwise be in the graph it builds.
        monkeypatch.setattr(django_apps, 'get_models', lambda: [Left, Right])
        return _owned_fields(Left) + _owned_fields(Right)

    assert _build() == []


def test_a_subclass_of_owning_foreign_key_keeps_its_own_deconstructed_path():
    """The frozen path is pinned for ``OwningForeignKey`` itself only. A subclass recording
    the base path would rebuild as the base field, silently dropping whatever it added."""

    class NarrowOwningForeignKey(OwningForeignKey):
        pass

    assert OwningForeignKey(PressKit, on_delete=SET_NULL).deconstruct()[1] == (
        'guitars.models.OwningForeignKey'
    )
    assert NarrowOwningForeignKey(PressKit, on_delete=SET_NULL).deconstruct()[1].endswith(
        'NarrowOwningForeignKey'
    )
