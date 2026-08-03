"""Model for tests/test_makemigrations_override.py only.

Starts with no migrations checked in at all (just an empty ``migrations`` package) --
the whole point is to watch ``makemigrations`` generate both layers from a clean slate.

Never touched by the rest of the suite -- not in ``LOCAL_APPS``, so
``makeguitarmigrations``/``audittenancy`` ignore it unless a test names it explicitly.
"""

from django.db.models import CharField

from guitars.models import SetarModel


class Probe(SetarModel):
    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name
