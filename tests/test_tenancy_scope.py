"""The tenant frame: nesting, bypass, and the ``@tenanted`` decorator.

Nothing here touches the database -- ``scope.py`` is pure ``ContextVar`` bookkeeping. What
it must get right is *fail-closed* behaviour: an absent dimension, and a dimension
explicitly bound to ``None``, both have to read as "no scope", because the alternative
(treating ``None`` as "match everything") fails OPEN.
"""

import asyncio

import pytest

from guitars.tenancy import (
    TenantScopeError,
    get_tenant,
    is_bypassed,
    tenancy_bypassed,
    tenant,
    tenanted,
)
from guitars.tenancy.scope import BYPASS, MULTI_VALUE_TYPES


# ─────────────────────────────── the frame ─────────────────────────────── #


def test_no_scope_is_an_empty_frame():
    assert get_tenant() == {}
    assert is_bypassed() is False


def test_entering_a_scope_publishes_the_dimension():
    with tenant(shop=7):
        assert get_tenant() == {BYPASS: False, 'shop': 7}


def test_frame_is_restored_on_exit_even_when_the_body_raises():
    with pytest.raises(RuntimeError), tenant(shop=7):
        raise RuntimeError('boom')

    assert get_tenant() == {}


def test_get_tenant_returns_a_copy_so_callers_cannot_corrupt_the_live_frame():
    with tenant(shop=7):
        snapshot = get_tenant()
        snapshot['shop'] = 'tampered'
        snapshot['injected'] = True

        assert get_tenant() == {BYPASS: False, 'shop': 7}


def test_nested_scopes_merge_and_restore():
    with tenant(shop=1):
        with tenant(user=2):
            # Merged, not replaced: the outer dimension is still active.
            assert get_tenant() == {BYPASS: False, 'shop': 1, 'user': 2}
        assert get_tenant() == {BYPASS: False, 'shop': 1}


def test_inner_scope_overrides_the_same_dimension_then_restores_it():
    with tenant(shop=1):
        with tenant(shop=2):
            assert get_tenant()['shop'] == 2
        assert get_tenant()['shop'] == 1


def test_multiple_dimensions_in_one_call():
    with tenant(shop=1, user=2):
        assert get_tenant() == {BYPASS: False, 'shop': 1, 'user': 2}


@pytest.mark.parametrize('collection', [[1, 2], (1, 2), {1, 2}, frozenset({1, 2})])
def test_every_multi_value_type_is_accepted_as_a_dimension_value(collection):
    """The three modules that special-case collections must agree on the type list."""
    assert isinstance(collection, MULTI_VALUE_TYPES)
    with tenant(shop=collection):
        assert get_tenant()['shop'] == collection


# ─────────────────────────────── bypass ─────────────────────────────── #


def test_bypass_sets_the_reserved_key_and_is_reported():
    with tenancy_bypassed():
        assert is_bypassed() is True
        assert get_tenant()[BYPASS] is True

    assert is_bypassed() is False


def test_an_explicit_scope_inside_a_bypass_re_enforces():
    """The documented rule: entering a tenant always enforces.

    Without this, a bypassed maintenance job that opens a scope for one tenant would keep
    reading every tenant's rows -- failing open in exactly the place the author was trying
    to be careful.
    """
    with tenancy_bypassed():
        assert is_bypassed() is True
        with tenant(shop=1):
            assert is_bypassed() is False
            assert get_tenant()['shop'] == 1
        assert is_bypassed() is True


def test_bypass_nested_inside_a_scope_restores_the_scope_afterwards():
    with tenant(shop=1):
        with tenancy_bypassed():
            assert is_bypassed() is True
        assert is_bypassed() is False
        assert get_tenant()['shop'] == 1


# ──────────────────────── lazy values are refused ──────────────────────── #


def test_a_queryset_is_refused_as_a_dimension_value():
    """A QuerySet would be evaluated *during* the publish, recursing back into it.

    The naive symptom is a RecursionError raised from somewhere unrelated, so the mistake
    is named at the point it is made.
    """
    from tests.testapp.models import Band

    with pytest.raises(TypeError, match='got a QuerySet'):
        with tenant(shop=Band.objects.all()):
            pass


def test_a_queryset_nested_in_a_collection_is_also_refused():
    from tests.testapp.models import Band

    with pytest.raises(TypeError, match='got a QuerySet'):
        with tenant(shop=[1, Band.objects.all()]):
            pass


def test_the_frame_is_not_mutated_when_a_lazy_value_is_refused():
    from tests.testapp.models import Band

    with tenant(shop=1):
        with pytest.raises(TypeError):
            with tenant(shop=Band.objects.all()):
                pass
        # Refused before the ContextVar was touched.
        assert get_tenant()['shop'] == 1


# ─────────────────────────── @tenanted decorator ─────────────────────────── #


def test_tenanted_opens_a_scope_from_the_default_parameter():
    @tenanted
    def handler(tenant):
        return get_tenant()

    assert handler(7) == {BYPASS: False, 'tenant': 7}


def test_tenanted_reads_a_custom_parameter_name():
    @tenanted(arg='shop')
    def handler(shop):
        return get_tenant()

    assert handler(7) == {BYPASS: False, 'shop': 7}


def test_tenanted_separates_the_parameter_name_from_the_dimension():
    """The parameter belongs to the signature; the dimension must match the manager.

    Collapsing the two would open a dimension no manager scopes on -- which then fails
    closed on the next scoped read, far from the decorator that got it wrong.
    """

    @tenanted(arg='target_shop', dimension='shop')
    def handler(target_shop):
        return get_tenant()

    assert handler(7) == {BYPASS: False, 'shop': 7}


def test_tenanted_reads_the_value_however_it_is_passed():
    @tenanted(arg='shop')
    def handler(a, shop=None):
        return get_tenant()['shop']

    assert handler(1, 7) == 7  # positionally
    assert handler(1, shop=8) == 8  # by keyword

    @tenanted(arg='shop')
    def with_default(shop=9):
        return get_tenant()['shop']

    assert with_default() == 9  # via its default


def test_tenanted_fails_closed_on_a_none_tenant():
    @tenanted(arg='shop')
    def handler(shop):
        raise AssertionError('body must not run')

    with pytest.raises(TenantScopeError, match='non-None'):
        handler(None)


def test_tenanted_lets_python_raise_its_own_missing_argument_error():
    """A missing required argument is a programming error in the caller, not a scope error.

    Re-labelling it would hide a plain TypeError behind a tenancy message.
    """

    @tenanted(arg='shop')
    def handler(shop):
        raise AssertionError('body must not run')

    with pytest.raises(TypeError, match='shop'):
        handler()


def test_tenanted_rejects_a_parameter_that_does_not_exist_at_decoration_time():
    with pytest.raises(TypeError, match='no .*shop.* parameter'):

        @tenanted(arg='shop')
        def handler(other):
            pass


def test_tenanted_rejects_generator_functions():
    """A generator body runs at iteration, by which time the scope has closed."""
    with pytest.raises(TypeError, match='generator functions'):

        @tenanted
        def handler(tenant):
            yield 1


def test_tenanted_rejects_async_generator_functions():
    with pytest.raises(TypeError, match='generator functions'):

        @tenanted
        async def handler(tenant):
            yield 1


def test_tenanted_preserves_the_wrapped_callables_identity():
    @tenanted
    def handler(tenant):
        """Docstring survives."""

    assert handler.__name__ == 'handler'
    assert handler.__doc__ == 'Docstring survives.'


async def test_tenanted_scope_survives_await():
    """The reason state lives in a ContextVar rather than a thread-local."""

    @tenanted(arg='shop')
    async def handler(shop):
        before = get_tenant()['shop']
        await asyncio.sleep(0)
        return before, get_tenant()['shop']

    assert await handler(7) == (7, 7)


async def test_tenanted_supports_an_object_whose_call_is_async():
    """Such an object is not itself a coroutine function.

    Taking the sync branch would close the scope before the caller ever awaited, so the
    check reads ``__call__`` off the type.
    """

    class Handler:
        async def __call__(self, shop):
            await asyncio.sleep(0)
            return get_tenant()['shop']

    wrapped = tenanted(Handler(), arg='shop')

    assert await wrapped(7) == 7


async def test_tenanted_async_fails_closed_on_none_before_awaiting_the_body():
    @tenanted(arg='shop')
    async def handler(shop):
        raise AssertionError('body must not run')

    with pytest.raises(TenantScopeError, match='non-None'):
        await handler(None)


async def test_tenanted_async_lets_python_raise_its_own_missing_argument_error():
    @tenanted(arg='shop')
    async def handler(shop):
        raise AssertionError('body must not run')

    with pytest.raises(TypeError, match='shop'):
        await handler()


async def test_concurrent_tasks_do_not_see_each_others_scopes():
    """Each task gets its own copy of the context -- the isolation the whole design rests on."""
    seen = {}

    async def work(name, value):
        with tenant(shop=value):
            await asyncio.sleep(0)  # yield, giving the other task a chance to interleave
            seen[name] = get_tenant()['shop']

    await asyncio.gather(work('a', 1), work('b', 2))

    assert seen == {'a': 1, 'b': 2}
