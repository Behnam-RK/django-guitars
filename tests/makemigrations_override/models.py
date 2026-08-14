"""Model for tests/test_makemigrations_override.py only. Starts with no migrations
checked in -- the point is watching ``makemigrations`` generate both layers from a
clean slate. Not in ``LOCAL_APPS``, so ignored unless a test names it explicitly."""

from django.db.models import CharField

from guitars.models import SetarModel


class Probe(SetarModel):
    name = CharField(max_length=100)

    def __str__(self) -> str:
        return self.name
