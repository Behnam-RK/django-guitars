"""Models for tests/test_mti_incremental.py only. ``Ancestor`` starts fully migrated
and enforced. ``Descendant`` is an MTI child added later with only a schema migration
checked in -- the one-commit-later state the test upgrades from. Not in ``LOCAL_APPS``."""

from django.db.models import CharField

from guitars.models import SetarModel


class Ancestor(SetarModel):
    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Descendant(Ancestor):
    detail = CharField(max_length=100)

    class Meta:
        # Without this, the autodetector inherits SoftDeletableModel.Meta's index onto
        # this MTI child too -- `_deleted_at` lives only on Ancestor's table.
        pass

    def __str__(self) -> str:
        return f'{self.name}: {self.detail}'
