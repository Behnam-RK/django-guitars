"""Models for tests/test_legacy_migrations.py only, one CASCADE FK exercising a trigger,
soft-delete rule, and cascade-rule together. Named ``Legacy*``: reusing ``Band``/``Album``
would collide on Django's index-name check (E030)."""

from django.db.models import CASCADE, CharField, ForeignKey

from guitars.models import SetarModel


class LegacyBand(SetarModel):
    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class LegacyAlbum(SetarModel):
    title = CharField(max_length=100)
    band = ForeignKey(LegacyBand, on_delete=CASCADE, related_name='albums')

    def __str__(self) -> str:
        return self.title
