from django.db.models import (
    CASCADE,
    DO_NOTHING,
    SET_NULL,
    CharField,
    ForeignKey,
    IntegerField,
    ManyToManyField,
)
from django.utils.functional import cached_property

from guitars.models import (
    DutarModel,
    GuitarModel,
    LiveManager,
    OwningForeignKey,
    SetarModel,
    TarModel,
)
from guitars.tenancy import tenanted_manager


class Riff(TarModel):
    """Basic helpers only (TarModel), no timestamps or soft deletion -- ``band`` is a
    CASCADE FK to a soft-deletable model, exercising the plain hard-delete path."""

    name = CharField(max_length=50)
    band = ForeignKey('Band', on_delete=CASCADE, null=True, blank=True, related_name='riffs')

    def __str__(self) -> str:
        return self.name

    @cached_property
    def shout(self) -> str:
        """A cached_property used to test refresh-driven cache invalidation."""
        return self.name.upper()


class Genre(DutarModel):
    """Timestamps only (DutarModel) — no soft deletion. Also the m2m target."""

    name = CharField(max_length=50)

    def __str__(self) -> str:
        return self.name


class WhisperMixin:
    """A plain mixin contributing a ``cached_property`` -- proves refresh-driven
    invalidation reaches properties inherited from anywhere in the MRO, not just the class."""

    @cached_property
    def whisper(self) -> str:
        return self.name.lower()


class Band(WhisperMixin, SetarModel):
    name = CharField(max_length=100)
    nickname = CharField(max_length=100, null=True, blank=True)
    genres = ManyToManyField(Genre, related_name='bands', blank=True)

    def __str__(self) -> str:
        return self.name

    @cached_property
    def shout(self) -> str:
        """A cached_property used to test refresh-driven cache invalidation."""
        return self.name.upper()


class PressKit(SetarModel):
    """Owned target: the thing an ``Album`` owns rather than merely points at. Soft-deletable,
    since only a soft-deletable dependent has a ``_deleted_at`` for the rule to stamp."""

    headline = CharField(max_length=100)

    def __str__(self) -> str:
        return self.headline


class Album(SetarModel):
    title = CharField(max_length=100)
    band = ForeignKey(Band, on_delete=CASCADE, related_name='albums')
    # SET_NULL (not CASCADE) to a soft-deletable model -- exercises the "skip non-CASCADE
    # relation" branches in cascade-rule generation and instance hard_delete's DFS collection.
    producer = ForeignKey(
        Band, on_delete=SET_NULL, null=True, blank=True, related_name='produced_albums'
    )
    # Two owned FKs to one table: the owned rule is always named after its column, so these
    # must produce two distinctly-named rules rather than one silently replacing the other.
    press_kit = OwningForeignKey(
        PressKit, on_delete=DO_NOTHING, null=True, blank=True, related_name='albums'
    )
    alt_press_kit = OwningForeignKey(
        PressKit, on_delete=DO_NOTHING, null=True, blank=True, related_name='alt_albums'
    )

    def __str__(self) -> str:
        return self.title


class Ensemble(SetarModel):
    """MTI parent (full kit) — owns _updated_at / _deleted_at on its own table."""

    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Orchestra(Ensemble):
    """Single-level MTI child -- its own ``Meta`` (even empty) stops the parent's partial
    ``_deleted_at`` index from re-declaring against this table's non-local column (E016)."""

    conductor = CharField(max_length=100)
    # Owned FK on an MTI child's own table while _deleted_at lives on Ensemble: the rule
    # would fire on the ancestor, where old."programme_id" names nothing. Warns, emits nothing.
    programme = OwningForeignKey(
        'PressKit', on_delete=DO_NOTHING, null=True, blank=True, related_name='orchestras'
    )

    class Meta:
        pass

    def __str__(self) -> str:
        return f'{self.name} ({self.conductor})'


class ChamberOrchestra(Orchestra):
    """Multi-level MTI child — metadata still resolves to the Ensemble root table."""

    seats = IntegerField(default=0)

    class Meta:
        pass


class Merch(SetarModel):
    """Two independent CASCADE FKs to ``Album``, so hard-deleting the shared root makes
    the DFS in ``soft_deletion._collect`` visit this model twice -- the "already queued"
    branch. Not ``Riff``: its rule-less Collector cascade would orphan a live row."""

    description = CharField(max_length=100)
    album = ForeignKey(Album, on_delete=CASCADE, null=True, blank=True, related_name='merch')
    bonus_album = ForeignKey(
        Album, on_delete=CASCADE, null=True, blank=True, related_name='bonus_merch'
    )
    # Owned dependent reached through MTI: Orchestra's _deleted_at lives on Ensemble, and the
    # rule must correlate against that table -- an MTI chain shares one primary-key value.
    featured_orchestra = OwningForeignKey(
        Orchestra, on_delete=DO_NOTHING, null=True, blank=True, related_name='featured_by'
    )

    def __str__(self) -> str:
        return self.description


class Patron(SetarModel):
    """Owns the MTI *root* while ``Merch`` owns its child. Collecting the root removes every
    table in the chain, so ``Merch.featured_orchestra`` dangles -- and ``get_fields()`` on
    the root never reports it, only a descendant's does. See ``_still_referenced``."""

    name = CharField(max_length=100)
    ensemble = OwningForeignKey(
        Ensemble, on_delete=DO_NOTHING, null=True, blank=True, related_name='patrons'
    )

    def __str__(self) -> str:
        return self.name


class Stagehand(SetarModel):
    """Leaf of a two-deep ownership chain: owned by ``Rider``, which is itself owned by
    ``Residency``. Every other owned relation in this app is one hop, so nothing else
    exercises a rule whose own ``UPDATE`` fires a second rule."""

    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Rider(SetarModel):
    """Middle of the chain: an owned target that is itself an owner. Soft-deleting a
    ``Residency`` stamps this row by rule, and *that* stamp must fire the rule stamping
    its ``Stagehand``; ``hard_delete()`` must remove the three in dependency order."""

    clause = CharField(max_length=100)
    stagehand = OwningForeignKey(
        Stagehand, on_delete=DO_NOTHING, null=True, blank=True, related_name='riders'
    )

    def __str__(self) -> str:
        return self.clause


class Residency(SetarModel):
    """Top of the chain."""

    venue_name = CharField(max_length=100)
    rider = OwningForeignKey(
        Rider, on_delete=DO_NOTHING, null=True, blank=True, related_name='residencies'
    )

    def __str__(self) -> str:
        return self.venue_name


class Section(SetarModel):
    """Soft-deletable, CASCADE FK to an MTI child -- exercises the cascade rule landing
    on the owner (Ensemble) table, not the child."""

    name = CharField(max_length=100)
    orchestra = ForeignKey(Orchestra, on_delete=CASCADE, related_name='sections')

    def __str__(self) -> str:
        return self.name


# ─── tenancy: GUITARS_TENANT_FIELD = 'label', not the default 'tenant' -- only a
# non-default value proves the column, GUC, predicate, and dimension moved together. ───


class Label(SetarModel):
    """The tenant, soft-deletable on purpose (the more demanding shape) -- a CASCADE
    tenant FK means soft-deleting a label archives its rows. See test_tenancy_models.py."""

    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Release(GuitarModel):
    """The ordinary tenanted model: own-table tenant column, full kit."""

    title = CharField(max_length=100)

    def __str__(self) -> str:
        return self.title


class Track(GuitarModel):
    """Tenanted, CASCADE FK to another tenanted model -- two rules meet here: the cascade
    soft-delete from Release and its own tenant policy, under FORCE ROW LEVEL SECURITY."""

    title = CharField(max_length=100)
    release = ForeignKey(Release, on_delete=CASCADE, related_name='tracks')

    def __str__(self) -> str:
        return self.title


class Tour(GuitarModel):
    """Root of a tenanted MTI chain. Owns the tenant column on its own table."""

    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class WorldTour(Tour):
    """First MTI level: the tenant column is one table up."""

    continents = IntegerField(default=1)

    class Meta:
        pass


class StadiumTour(WorldTour):
    """Second MTI level: the tenant column is *two* tables up -- the case ``column_owner``
    exists for, since the immediate parent has no tenant column either."""

    capacity = IntegerField(default=0)

    class Meta:
        pass


class Booking(SetarModel):
    """Hand-declared ``tenanted_manager()`` over ``LiveManager``, tenant FK declared by
    hand -- only ``objects`` is scoped; ``GuitarModel`` exists so nobody has to get
    that asymmetry right themselves."""

    venue = CharField(max_length=100)
    label = ForeignKey(Label, on_delete=CASCADE, related_name='bookings')

    objects = tenanted_manager(_manager_class=LiveManager, label='label')

    def __str__(self) -> str:
        return self.venue


class Review(SetarModel):
    """A multi-hop dimension: scoped through a relation, no local tenant column -- Python
    scoping applies, RLS cannot, and both generators must say so out loud."""

    body = CharField(max_length=200)
    release = ForeignKey(Release, on_delete=CASCADE, related_name='reviews')

    objects = tenanted_manager(_manager_class=LiveManager, label='release__label')

    def __str__(self) -> str:
        return self.body


# ─────────────────────── a dimension on every rung ─────────────────────── #
# Three-level MTI chain giving `_classify` a model with dimensions split across two
# ancestors *and* one on its own table -- the only combination exercising that branch.


class Festival(SetarModel):
    """MTI root: owns the `market` dimension's column directly."""

    name = CharField(max_length=100)
    market = ForeignKey(Label, on_delete=CASCADE, related_name='festivals')

    def __str__(self) -> str:
        return self.name


class TouringFestival(Festival):
    """First MTI level: ``promoter``'s column lives here, one table up from the leaf."""

    promoter = ForeignKey(Label, on_delete=CASCADE, related_name='touring_festivals')

    class Meta:
        pass


class HeadlineFestival(TouringFestival):
    """Leaf: ``sponsor`` is own-table, ``market``/``promoter`` are each owned by a
    *different* ancestor -- the only shape that triggers ``_classify``'s diamond case."""

    sponsor = ForeignKey(Label, on_delete=CASCADE, related_name='headline_festivals')

    objects = tenanted_manager(
        _manager_class=LiveManager, market='market', promoter='promoter', sponsor='sponsor'
    )

    class Meta:
        pass


# ────────────── an ancestor that holds the column but isn't tenanted ────────────── #
# The tenant column lives on an untenanted MTI ancestor, so the trigger relocates onto that
# ancestor's table (ADR 0009). Three roots, because one chain's refusal would mask the rest.


class Venue(SetarModel):
    """Untenanted MTI root that merely *holds* a tenant column its child scopes on."""

    name = CharField(max_length=100)
    label = ForeignKey(Label, on_delete=CASCADE, related_name='venues')

    def __str__(self) -> str:
        return self.name


class Arena(Venue):
    """The happy path: sole claimant, autofills, so the trigger relocates onto ``Venue``."""

    seats = IntegerField(default=0)

    objects = tenanted_manager(_manager_class=LiveManager, label='label', autofill=True)

    class Meta:
        pass


class Hall(SetarModel):
    """Root whose column two children claim, one of which opts out of autofill."""

    name = CharField(max_length=100)
    label = ForeignKey(Label, on_delete=CASCADE, related_name='halls')

    def __str__(self) -> str:
        return self.name


class ConcertHall(Hall):
    """Wants the trigger; cannot have it, because ``LectureHall`` shares the table."""

    stage = CharField(max_length=50, default='')

    objects = tenanted_manager(_manager_class=LiveManager, label='label', autofill=True)

    class Meta:
        pass


class LectureHall(Hall):
    """Opts out. An MTI insert here writes a row into ``Hall``, so a trigger there would
    overwrite the opt-out ADR 0005 makes auditable as an *absent* trigger."""

    rows = IntegerField(default=0)

    objects = tenanted_manager(_manager_class=LiveManager, label='label', autofill=False)

    class Meta:
        pass


class Court(SetarModel):
    """Root whose one column two children claim under *different* dimensions."""

    name = CharField(max_length=100)
    label = ForeignKey(Label, on_delete=CASCADE, related_name='courts')

    def __str__(self) -> str:
        return self.name


class TennisCourt(Court):
    """Claims ``Court.label_id`` as the ``label`` dimension."""

    net_height = IntegerField(default=0)

    objects = tenanted_manager(_manager_class=LiveManager, label='label', autofill=True)

    class Meta:
        pass


class SquashCourt(Court):
    """Claims the *same column* as ``market`` -- a second GUC, so a second trigger would
    race the first on one table in name order, an ordering nobody declared."""

    walls = IntegerField(default=4)

    objects = tenanted_manager(_manager_class=LiveManager, market='label', autofill=True)

    class Meta:
        pass


class Placard(SetarModel):
    """Owned target reachable from two *different* owner tables -- the shape ``PressKit`` does
    not cover, its owning columns both living on ``testapp_album``. Each owner's rule must
    carry an arm for the other, or one kind's last owner archives what the other holds."""

    caption = CharField(max_length=100)

    def __str__(self) -> str:
        return self.caption


class Kiosk(SetarModel):
    """One of ``Placard``'s two owner tables."""

    label = CharField(max_length=100)
    placard = OwningForeignKey(
        Placard, on_delete=DO_NOTHING, null=True, blank=True, related_name='kiosks'
    )

    def __str__(self) -> str:
        return self.label


class Foyer(SetarModel):
    """The other. Deliberately a distinct table rather than a second column on ``Kiosk``:
    a co-owner arm against another table carries no self-exclusion, which is the half
    ``Album``'s same-table pair cannot exercise."""

    label = CharField(max_length=100)
    placard = OwningForeignKey(
        Placard, on_delete=DO_NOTHING, null=True, blank=True, related_name='foyers'
    )

    def __str__(self) -> str:
        return self.label
