from django.db.models import CASCADE, CharField, ForeignKey, ManyToManyField
from django.utils.functional import cached_property

from guitars.models import GuitarModel, SetarModel


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

    def __str__(self) -> str:
        return self.title
