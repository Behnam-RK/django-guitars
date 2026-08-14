"""Direct tests for guitars.introspection's extracted, standalone helpers. Most coverage
comes from consumers that depend on getting it right; ``mti_root`` has none, since it was
extracted from two duplicated inline walks (M5, #12), so it gets one directly here."""

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
