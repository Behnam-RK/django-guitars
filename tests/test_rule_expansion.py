"""An ``UPDATE`` of a table other tables cascade from is one statement (#80, ADR 0039). Cascade
and owned rules were ``ON UPDATE`` rules, planned on every ``UPDATE`` (a ``Band`` rename was twelve
statements); they are now arms of the owner's trigger, behind one early exit."""

import json

import pytest

from tests.conftest import scalar
from tests.testapp.models import Band, Ensemble, Label, Offer, Orchestra, Setlist, Tier


def _statements(model) -> int:
    """Statements the rewriter makes of one ``UPDATE`` of *model*'s table: ``EXPLAIN``'s JSON
    carries one plan per rewritten query. Run on no row -- planning is what is being counted."""
    table, pk = model._meta.db_table, model._meta.pk.column
    plans = scalar(f'EXPLAIN (FORMAT JSON) UPDATE "{table}" SET "{pk}" = "{pk}" WHERE "{pk}" = 0')
    return len(plans if isinstance(plans, list) else json.loads(plans))


@pytest.mark.parametrize(
    'model',
    [Band, Offer, Tier, Label, Ensemble, Orchestra, Setlist],
    ids=lambda model: model.__name__,
)
def test_an_update_of_a_cascade_owner_is_one_statement(model, db):
    assert _statements(model) == 1
