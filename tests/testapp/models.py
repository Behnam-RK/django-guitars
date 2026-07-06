from django.db.models import (
    CASCADE,
    SET_NULL,
    CharField,
    ForeignKey,
    IntegerField,
    ManyToManyField,
)
from django.utils.functional import cached_property

from guitars.models import DutarModel, GuitarModel, SetarModel


class Riff(DutarModel):
    """Basic helpers only (DutarModel) — no timestamps, no soft deletion.

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


class Genre(SetarModel):
    """Timestamps only (SetarModel) — no soft deletion. Also the m2m target."""

    name = CharField(max_length=50)

    def __str__(self) -> str:
        return self.name


class Band(GuitarModel):
    name = CharField(max_length=100)
    nickname = CharField(max_length=100, null=True, blank=True)
    genres = ManyToManyField(Genre, related_name='bands', blank=True)

    def __str__(self) -> str:
        return self.name

    @cached_property
    def shout(self) -> str:
        """A cached_property used to test refresh-driven cache invalidation."""
        return self.name.upper()


class Album(GuitarModel):
    title = CharField(max_length=100)
    band = ForeignKey(Band, on_delete=CASCADE, related_name='albums')
    # SET_NULL (not CASCADE) to a soft-deletable model -- exercises the "skip non-CASCADE
    # relation" branches in cascade-rule generation and instance hard_delete's DFS collection.
    producer = ForeignKey(
        Band, on_delete=SET_NULL, null=True, blank=True, related_name='produced_albums'
    )

    def __str__(self) -> str:
        return self.title


class Ensemble(GuitarModel):
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


class Section(GuitarModel):
    """Soft-deletable model with a CASCADE FK to an MTI child (the FK target).

    Exercises that the cascade soft-delete rule lands on the owner (Ensemble) table.
    """

    name = CharField(max_length=100)
    orchestra = ForeignKey(Orchestra, on_delete=CASCADE, related_name='sections')

    def __str__(self) -> str:
        return self.name
