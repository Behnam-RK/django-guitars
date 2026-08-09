"""Direct tests for guitars.introspection's extracted, standalone helpers.

Most of introspection.py's coverage comes from the consumers that actually depend on
getting it right -- test_schema_qualified.py (column_owner/owns_column, via the
enforcement generator) and test_mti.py/test_tenancy_edges.py (is_mti_child, via
makeguitarmigrations and the RLS policy generator). ``mti_root`` is different: it was
extracted from two duplicated inline walks in guitars/models/soft_deletion.py (M5, #12)
with no consumer-level test of its own, so it gets one directly here -- none of these
touch the database, since walking ``_meta.parents`` needs no connection.
"""

from guitars.introspection import mti_root
from tests.testapp.models import Festival, HeadlineFestival, Riff, TouringFestival


def test_a_non_mti_model_is_its_own_root():
    assert mti_root(Riff) is Riff


def test_the_root_of_an_mti_chain_is_itself_its_own_root():
    assert mti_root(Festival) is Festival


def test_a_middle_child_resolves_to_the_root():
    assert mti_root(TouringFestival) is Festival


def test_a_leaf_three_levels_down_resolves_to_the_root():
    assert mti_root(HeadlineFestival) is Festival
