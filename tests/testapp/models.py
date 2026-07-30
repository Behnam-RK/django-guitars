from django.db.models import (
    CASCADE,
    SET_NULL,
    CharField,
    ForeignKey,
    IntegerField,
    ManyToManyField,
)
from django.utils.functional import cached_property

from guitars.models import DutarModel, GuitarModel, LiveManager, SetarModel, TarModel
from guitars.tenancy import TenantedManager


class Riff(TarModel):
    """Basic helpers only (TarModel) — no timestamps, no soft deletion.

    ``band`` is a CASCADE FK to a soft-deletable model from a model that is
    itself NOT soft-deletable — exercises the plain (non-``_all_objects``)
    hard-delete path for cascade children.
    """

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


class Band(SetarModel):
    name = CharField(max_length=100)
    nickname = CharField(max_length=100, null=True, blank=True)
    genres = ManyToManyField(Genre, related_name='bands', blank=True)

    def __str__(self) -> str:
        return self.name

    @cached_property
    def shout(self) -> str:
        """A cached_property used to test refresh-driven cache invalidation."""
        return self.name.upper()


class Album(SetarModel):
    title = CharField(max_length=100)
    band = ForeignKey(Band, on_delete=CASCADE, related_name='albums')
    # SET_NULL (not CASCADE) to a soft-deletable model -- exercises the "skip non-CASCADE
    # relation" branches in cascade-rule generation and instance hard_delete's DFS collection.
    producer = ForeignKey(
        Band, on_delete=SET_NULL, null=True, blank=True, related_name='produced_albums'
    )

    def __str__(self) -> str:
        return self.title


class Ensemble(SetarModel):
    """MTI parent (full kit) — owns _updated_at / _deleted_at on its own table."""

    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Orchestra(Ensemble):
    """Single-level MTI child — its metadata columns live on the Ensemble table.

    MTI children of a soft-deletable base must declare their own ``Meta`` so the parent's
    partial ``_deleted_at`` index isn't re-declared against this table's non-local column
    (Django ``models.E016``). An empty ``Meta`` is enough; the managers are still inherited.
    """

    conductor = CharField(max_length=100)

    class Meta:
        pass

    def __str__(self) -> str:
        return f'{self.name} ({self.conductor})'


class ChamberOrchestra(Orchestra):
    """Multi-level MTI child — metadata still resolves to the Ensemble root table."""

    seats = IntegerField(default=0)

    class Meta:
        pass


class Section(SetarModel):
    """Soft-deletable model with a CASCADE FK to an MTI child (the FK target).

    Exercises that the cascade soft-delete rule lands on the owner (Ensemble) table.
    """

    name = CharField(max_length=100)
    orchestra = ForeignKey(Orchestra, on_delete=CASCADE, related_name='sections')

    def __str__(self) -> str:
        return self.name


# ─────────────────────────────── tenancy ─────────────────────────────── #
#
# `tests/settings.py` sets GUITARS_TENANT_MODEL = 'testapp.Label' and
# GUITARS_TENANT_FIELD = 'label'. The field name is deliberately NOT the default
# 'tenant': it has to name the column, the reverse accessor, the `tenant.label` session
# setting, the policy predicate and the scope dimension, and only a non-default value
# proves all five moved together. The default name is covered by the subprocess probes in
# `tests/test_ladder.py`.
#
# The models above stay untenanted on purpose. A project adopting tenancy does so model by
# model, and the two kinds have to coexist -- an untenanted model must not start demanding
# a scope, and a tenanted one must not stop.


class Label(SetarModel):
    """The tenant. Soft-deletable on purpose, which is the more demanding shape.

    A CASCADE tenant FK means `makeguitarmigrations` writes one cascade soft-delete rule
    onto *this* table per tenanted model, so soft-deleting a label archives its rows. That
    interacts with row-level security in a way worth having under test rather than in a
    docstring: see `tests/test_tenancy_models.py`.
    """

    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Release(GuitarModel):
    """The ordinary tenanted model: own-table tenant column, full kit."""

    title = CharField(max_length=100)

    def __str__(self) -> str:
        return self.title


class Track(GuitarModel):
    """Tenanted, with a CASCADE FK to another tenanted model.

    Two rules meet on this table -- the cascade soft-delete from `Release` and its own
    tenant policy -- so it covers a `DO ALSO` rule firing under `FORCE ROW LEVEL SECURITY`.
    """

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
    """Second MTI level: the tenant column is *two* tables up.

    The case `column_owner` exists for -- predicating the owner-join against the immediate
    parent would reference a table that has no tenant column either.
    """

    capacity = IntegerField(default=0)

    class Meta:
        pass


class Booking(SetarModel):
    """Hand-declared `TenantedManager` over `LiveManager`, tenant FK declared by hand.

    The composition path a project takes when it wants scoping without the `GuitarModel`
    rung -- a tenant FK it declares itself (so `editable=False` and the templated
    `related_name` are its own choice, not the rung's) and `GUITARS_TENANT_AUTOFILL`
    honoured rather than overridden. Only `objects` is scoped, so `_archives` and
    `_all_objects` stay unscoped here: that asymmetry is the point, and it is what
    `GuitarModel` exists to stop you having to get right.
    """

    venue = CharField(max_length=100)
    label = ForeignKey(Label, on_delete=CASCADE, related_name='bookings')

    objects = TenantedManager(_manager_class=LiveManager, label='label')

    def __str__(self) -> str:
        return self.venue


class Review(SetarModel):
    """A multi-hop dimension: scoped through a relation, with no local tenant column.

    Python scoping applies; a row-level-security policy cannot, because there is nothing on
    this table to predicate on. `makeguitarmigrations` and `audittenancy` must both say so
    out loud instead of skipping it silently.
    """

    body = CharField(max_length=200)
    release = ForeignKey(Release, on_delete=CASCADE, related_name='reviews')

    objects = TenantedManager(_manager_class=LiveManager, label='release__label')

    def __str__(self) -> str:
        return self.body
