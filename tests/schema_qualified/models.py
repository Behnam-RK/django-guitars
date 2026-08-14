"""Models for tests/test_schema_qualified.py only. ``Event``'s ``db_table`` is
schema-qualified, pre-quoted (see ``_quote_table``'s docstring for why). Tenanted so it
proves triggers, soft-delete, and RLS together. Not in ``LOCAL_APPS``."""

from django.db.models import CharField

from guitars.models import GuitarModel


class Event(GuitarModel):
    name = CharField(max_length=100)

    class Meta(GuitarModel.Meta):
        db_table = '"analytics"."events"'

    def __str__(self) -> str:
        return self.name
