"""Models for tests/test_schema_qualified.py only.

``Event``'s ``db_table`` is schema-qualified, in Django's own pre-quoted convention
(``'"analytics"."events"'``) -- the one thing nothing else in this suite exercises.
Tenanted (``GuitarModel``) so the same table proves triggers, soft-delete rules *and* the
tenant RLS policy all correctly target a table outside ``public``, in one place, through
ordinary ORM reads and writes rather than only through generated-SQL inspection.

The pre-quoted form, not a bare ``'analytics.events'``, is required: Django's own query
compiler calls ``connection.ops.quote_name()`` on ``db_table`` for every ORM query, and
that only passes a string through unchanged when it already starts and ends with ``"`` --
an unqualified-looking ``'analytics.events'`` would be wrapped by Django itself as one
wrong identifier on every read or write, regardless of what guitars emits. See
``guitars.sql._identifiers._quote_table``'s docstring.

Never touched by the rest of the suite -- not in ``LOCAL_APPS``, so
``makeguitarmigrations``/``audittenancy`` ignore it unless a test names it explicitly.
"""

from django.db.models import CharField

from guitars.models import GuitarModel


class Event(GuitarModel):
    name = CharField(max_length=100)

    class Meta(GuitarModel.Meta):
        db_table = '"analytics"."events"'

    def __str__(self) -> str:
        return self.name
