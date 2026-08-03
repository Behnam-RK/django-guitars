"""Models for tests/test_mti_incremental.py only.

``Ancestor`` starts alone, fully migrated and enforced (``migrations/0002_auto_enforcement.py``
is real, current-generator output -- not hand-written, and not the pre-1.1.0 shape
``tests.legacy_migrations`` deliberately is). ``Descendant`` is added later, an MTI child of
``Ancestor``, with only a schema migration checked in
(``migrations/0003_descendant.py``) -- the one-commit-later state this file's test exists
to upgrade from.

Never touched by the rest of the suite -- not in ``LOCAL_APPS``, so
``makeguitarmigrations``/``audittenancy`` ignore it unless a test names it explicitly.
"""

from django.db.models import CharField

from guitars.models import SetarModel


class Ancestor(SetarModel):
    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name


class Descendant(Ancestor):
    detail = CharField(max_length=100)

    class Meta:
        # Without this, Django's own migration autodetector inherits
        # SoftDeletableModel.Meta's `_deleted_at` partial index onto this MTI child's
        # CreateModel too -- `_deleted_at` lives only on Ancestor's table, not this one,
        # so `migrate` fails outright with "column does not exist". See
        # tests/testapp/models.py's WorldTour/StadiumTour for the same override.
        pass

    def __str__(self) -> str:
        return f'{self.name}: {self.detail}'
