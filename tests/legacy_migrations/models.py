"""Models for tests/test_legacy_migrations.py only.

Deliberately shaped like ``tests.testapp``'s original ``Band``/``Album`` pair (see
``git show 2ba86a3^:tests/testapp/models.py``, before the 1.1.0 SQL-inlining change): one
CASCADE foreign key, so the fixture exercises a trigger, a soft-delete rule, and a
cascade-rule all at once, same as the real history this app's migrations are modelled on.
Named ``LegacyBand``/``LegacyAlbum`` rather than reusing ``Band``/``Album`` verbatim --
``SoftDeletableModel.Meta``'s partial index is named ``%(class)s_deleted_at``, so a model
of the same name as one already in ``tests.testapp`` collides on Django's own index-name
system check (E030) the moment both apps are installed together.

Never touched by the rest of the suite -- not in ``LOCAL_APPS``, so
``makeguitarmigrations``/``audittenancy`` ignore it unless a test names it explicitly.
"""

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
