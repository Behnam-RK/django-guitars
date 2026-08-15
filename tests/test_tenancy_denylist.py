"""The deny-list must not fall behind the querysets it guards -- a method added to a
guitars queryset, or to Django's own, must be denied or explicitly declared safe. Walks
``django.db.models.QuerySet`` itself, not a hand-maintained set, so an addition fails loud."""

import inspect

import pytest
from django.db import connections
from django.db.models import QuerySet as DjangoQuerySet

from guitars.models import HardDeletableQuerySet, LiveQuerySet
from guitars.tenancy import TenantScopeError, tenancy_bypassed
from guitars.tenancy.querysets import _ALLOWED_UNSCOPED, _untenanted_queryset_class
from tests.testapp.models import Release


#: The runtime allow-list is the source of truth the sweep reads; this module's
#: classification below cross-checks it without duplicating it.
SAFE_BY_DESIGN = _ALLOWED_UNSCOPED

#: Historical pinned findings (hard_delete, _raw_delete, explain were each once missing
#: from the ported deny-list) -- a Django *addition* is now caught dynamically instead.
MUST_BE_DENIED = {
    '_fetch_all',
    'count',
    'exists',
    'aggregate',
    'explain',
    'acount',
    'aexists',
    'aaggregate',
    'update',
    'delete',
    'bulk_update',
    'aupdate',
    'adelete',
    'abulk_update',
    'iterator',
    'aiterator',
    'hard_delete',
    '_hard_delete_own_table',
    '_raw_delete',
}

_QUERYSETS = [LiveQuerySet, HardDeletableQuerySet]


def _own_members(cls) -> set[str]:
    """Names *this* class declares, excluding dunders."""
    return {name for name in vars(cls) if not name.startswith('__')}


def _denying_class(base):
    return _untenanted_queryset_class(base)


# ──────────────────────── Django's own QuerySet surface ───────────────────────── #
# Walks django.db.models.QuerySet itself, so a method Django adds fails loudly. Manager
# isn't enumerated: both checked Django versions have zero own non-dunder members.

#: Explicitly overridden with _deny / _deny_query_write in guitars/tenancy/querysets.py.
DJANGO_DENIED_DIRECTLY = {
    'aaggregate',
    'abulk_update',
    'acount',
    'adelete',
    'aexists',
    'aggregate',
    'aiterator',
    'aupdate',
    'bulk_update',
    'count',
    'delete',
    'exists',
    'explain',
    'iterator',
    'update',
}

#: Not separately overridden -- each delegates, in Django itself, to an already-denied
#: primitive (async members via sync_to_async; get/first/... via clone+.get()/.exists()).
#: TestDeniedViaChainActuallyRaises below proves it behaviorally, not just by reasoning.
DJANGO_DENIED_VIA_CHAIN = {
    'acontains',
    'aearliest',
    'aexplain',
    'afirst',
    'aget',
    'aget_or_create',
    'ain_bulk',
    'alast',
    'alatest',
    'aupdate_or_create',
    'contains',
    'earliest',
    'first',
    'get',
    'get_or_create',
    'in_bulk',
    'last',
    'latest',
    'update_or_create',
}

#: Overridden individually, not aliased to a bare denial, so audit mode can report and
#: proceed. Asserted specially below.
DJANGO_INDIVIDUALLY_HANDLED = {'abulk_create', 'acreate', 'bulk_create', 'create', 'none'}

#: Chain-building or metadata, never touching the database by themselves. Must match
#: _ALLOWED_UNSCOPED's Django-side names exactly.
DJANGO_LAZY_SAFE = {
    'alias',
    'all',
    'annotate',
    'as_manager',
    'complex_filter',
    'dates',
    'datetimes',
    'db',
    'defer',
    'difference',
    'distinct',
    'exclude',
    'extra',
    'filter',
    'intersection',
    'only',
    'order_by',
    'ordered',
    'prefetch_related',
    'query',
    'resolve_expression',
    'reverse',
    'select_for_update',
    'select_related',
    'union',
    'using',
    'values',
    'values_list',
}

#: raw() returns a RawQuerySet that never passes back through the denying queryset, so
#: leaving it lazy would hand out an unscoped escape hatch. Absent from _ALLOWED_UNSCOPED,
#: so the sweep denies it like any other unclassified method -- no explicit alias needed.
DJANGO_DENIED_BY_SWEEP = {'raw'}


def _django_queryset_public_members() -> set[str]:
    """django.db.models.QuerySet's own public members -- private helpers excluded, since
    Django routes every database-touching path through a public method or _fetch_all."""
    return {name for name in vars(DjangoQuerySet) if not name.startswith('_')}


def test_djangos_own_queryset_surface_is_fully_classified():
    """The drift test this module used to be missing. Not parametrized like the tests
    below -- Django's QuerySet is the same class either guitars queryset subclasses."""
    classified = (
        DJANGO_DENIED_DIRECTLY
        | DJANGO_DENIED_VIA_CHAIN
        | DJANGO_INDIVIDUALLY_HANDLED
        | DJANGO_LAZY_SAFE
        | DJANGO_DENIED_BY_SWEEP
    )

    unclassified = sorted(_django_queryset_public_members() - classified)
    assert not unclassified, (
        f'django.db.models.QuerySet gained public member(s) {unclassified} that no bucket '
        f'in tests/test_tenancy_denylist.py names. Decide, do not default: if it can reach '
        f'the database on an unscoped queryset, deny it in guitars/tenancy/querysets.py and '
        f'add it to DJANGO_DENIED_DIRECTLY -- or confirm it already raises by delegating to '
        f'a denied primitive and add it to DJANGO_DENIED_VIA_CHAIN, proven by a new case in '
        f'TestDeniedViaChainActuallyRaises. Otherwise it is lazy: add it to DJANGO_LAZY_SAFE '
        f'with a reason.'
    )

    # And the reverse direction: a classified name Django no longer has means the
    # classification, not Django, has gone stale -- just as much a drift as an addition.
    stale = sorted(classified - _django_queryset_public_members())
    assert not stale, (
        f'{stale} no longer exist on django.db.models.QuerySet. Remove from whichever '
        f'DJANGO_* set still names them, or update it if Django renamed rather than '
        f'removed it.'
    )


#: The two DJANGO_DENIED_VIA_CHAIN names that open a real connection before the denial:
#: Django wraps update_or_create's body in atomic() before the get() that raises.
_NEEDS_DB = {'update_or_create', 'aupdate_or_create'}


def _chain_params(names):
    return [
        pytest.param(name, marks=pytest.mark.django_db(transaction=True))
        if name in _NEEDS_DB
        else name
        for name in sorted(names)
    ]


class TestDeniedViaChainActuallyRaises:
    """Proof, not just reasoning: every DJANGO_DENIED_VIA_CHAIN member actually raises. No
    test is marked ``db`` -- the denial fires before the chain compiles SQL, except
    ``update_or_create``/``aupdate_or_create`` (see ``_NEEDS_DB``)."""

    #: Positional/keyword arguments each name needs to be called at all -- not to succeed,
    #: since every one of them raises before touching the database. Anything absent here
    #: is called with no arguments.
    _CALL_ARGS: dict[str, tuple] = {
        'contains': (Release(pk=1),),
        'acontains': (Release(pk=1),),
        'earliest': ('pk',),
        'aearliest': ('pk',),
        'latest': ('pk',),
        'alatest': ('pk',),
        'in_bulk': ([1],),
        'ain_bulk': ([1],),
    }
    _CALL_KWARGS: dict[str, dict] = {
        'get_or_create': {'id': 1},
        'aget_or_create': {'id': 1},
        'update_or_create': {'id': 1},
        'aupdate_or_create': {'id': 1},
    }

    @pytest.fixture(autouse=True)
    async def _close_executor_connections(self):
        """See TestAsyncTwins in test_tenancy_internals.py -- a no-op for most cases."""
        yield
        from asgiref.sync import sync_to_async

        await sync_to_async(connections.close_all)()

    @pytest.mark.parametrize(
        'name',
        _chain_params(n for n in DJANGO_DENIED_VIA_CHAIN if not n.startswith('a')),
        ids=str,
    )
    def test_sync_members_raise(self, name):
        method = getattr(Release.objects, name)
        with pytest.raises(TenantScopeError):
            method(*self._CALL_ARGS.get(name, ()), **self._CALL_KWARGS.get(name, {}))

    @pytest.mark.parametrize(
        'name',
        _chain_params(n for n in DJANGO_DENIED_VIA_CHAIN if n.startswith('a')),
        ids=str,
    )
    async def test_async_members_raise(self, name):
        method = getattr(Release.objects, name)
        with pytest.raises(TenantScopeError):
            await method(*self._CALL_ARGS.get(name, ()), **self._CALL_KWARGS.get(name, {}))


@pytest.mark.parametrize('queryset_class', _QUERYSETS, ids=lambda c: c.__name__)
def test_every_guitars_queryset_member_is_classified(queryset_class):
    """No member of a guitars queryset may be left undecided -- catches the next
    ``hard_delete``: a method added without anyone deciding if it's reachable unscoped."""
    denying = _denying_class(queryset_class)
    overridden = _own_members(denying)

    unclassified = sorted(
        name
        for name in _own_members(queryset_class)
        if name not in overridden and name not in SAFE_BY_DESIGN
    )

    assert not unclassified, (
        f'{queryset_class.__name__} declares {unclassified}, which the tenancy deny-list '
        f'neither overrides nor declares safe. Decide, do not default: if it can reach '
        f'the database, add it to the deny-list in guitars/tenancy/querysets.py; if it is '
        f'lazy or otherwise harmless, add it to SAFE_BY_DESIGN with the reason.'
    )


def test_must_be_denied_names_are_real():
    """Every name in MUST_BE_DENIED must exist somewhere, or the list is lying -- the
    per-queryset test below skips absent names, which would quietly hide a typo."""
    known = set()
    for queryset_class in _QUERYSETS:
        known |= _own_members(queryset_class)
        known |= {name for name in dir(queryset_class) if not name.startswith('__')}
        known |= _own_members(_denying_class(queryset_class))

    unknown = sorted(MUST_BE_DENIED - known)
    assert not unknown, (
        f'MUST_BE_DENIED names methods that exist on no queryset: {unknown}. Either they '
        f'were renamed upstream (so the real method is now undenied) or these are typos '
        f'asserting nothing.'
    )


@pytest.mark.parametrize('queryset_class', _QUERYSETS, ids=lambda c: c.__name__)
def test_documented_database_methods_are_denied(queryset_class):
    """Every method in MUST_BE_DENIED actually resolves to a denying implementation."""
    denying = _denying_class(queryset_class)
    overridden = _own_members(denying)

    # Only assert about methods this queryset actually has -- hard_delete lives on
    # HardDeletableQuerySet, so LiveQuerySet is not expected to carry it. Django's own
    # methods are present on both.
    expected = {
        name
        for name in MUST_BE_DENIED
        if hasattr(queryset_class, name) or name in _own_members(denying)
    }
    missing = sorted(expected - overridden)

    assert not missing, (
        f'{queryset_class.__name__}: these reach the database but are not denied when a '
        f'tenant scope is missing: {missing}'
    )


def test_hard_delete_is_denied_and_has_no_async_twin():
    """``hard_delete`` was absent from the ported deny-list, and is sync-only -- a future
    ``ahard_delete`` must be denied deliberately, not assumed absent."""
    denying = _denying_class(HardDeletableQuerySet)

    assert 'hard_delete' in _own_members(denying)
    assert not hasattr(HardDeletableQuerySet, 'ahard_delete'), (
        'HardDeletableQuerySet gained an async hard_delete -- add it to the deny-list in '
        'guitars/tenancy/querysets.py and to MUST_BE_DENIED here.'
    )


def test_raw_delete_and_explain_are_denied():
    """``_raw_delete`` compiles a DeleteQuery off ``self.query`` directly (unfiltered
    across every tenant); ``explain`` bypasses ``_fetch_all``. Both were once undenied."""
    denying = _denying_class(LiveQuerySet)

    assert '_raw_delete' in _own_members(denying)
    assert 'explain' in _own_members(denying)


def test_none_stays_usable_on_an_unscoped_queryset():
    """``.none()`` must not raise: framework-level empties resolve through it."""
    denying = _denying_class(HardDeletableQuerySet)

    assert 'none' in _own_members(denying)
    assert not inspect.isdatadescriptor(denying.none)


def test_row_creating_writes_are_intercepted_rather_than_plainly_denied():
    """``create``/``bulk_create`` route through the enforcement mode, not a bare raise --
    aliasing to the denying helper would raise unconditionally and defeat audit mode."""
    denying = _denying_class(HardDeletableQuerySet)
    overridden = _own_members(denying)

    for name in ('create', 'acreate', 'bulk_create', 'abulk_create'):
        assert name in overridden, f'{name} is not intercepted on an unscoped queryset'
        assert denying.__dict__[name] is not denying.__dict__.get('_deny_query_write'), (
            f'{name} is aliased to the bare denial, so audit mode could not report-and-proceed'
        )


# ────────────────────────── the allow-list sweep itself ───────────────────────── #


def test_allowed_unscoped_methods_still_exist_upstream():
    """A name _ALLOWED_UNSCOPED lists that exists nowhere -- renamed or removed upstream
    -- is dead weight silently no longer meaning anything."""
    known = (
        _django_queryset_public_members()
        | _own_members(LiveQuerySet)
        | _own_members(HardDeletableQuerySet)
    )
    stale = sorted(set(_ALLOWED_UNSCOPED) - known)
    assert not stale, f'_ALLOWED_UNSCOPED names method(s) that exist nowhere: {stale}'


def test_allowed_unscoped_methods_all_carry_a_reason():
    empty = [name for name, reason in _ALLOWED_UNSCOPED.items() if not reason]
    assert not empty, f'_ALLOWED_UNSCOPED entries with no reason: {empty}'


def test_django_lazy_safe_matches_allowed_unscoped():
    """DJANGO_LAZY_SAFE (documentation) and _ALLOWED_UNSCOPED (runtime) must name the
    same Django methods, plus guitars' own lives/archives, or the two have drifted apart."""
    assert set(_ALLOWED_UNSCOPED) == DJANGO_LAZY_SAFE | {'lives', 'archives'}


def test_raw_is_denied_without_a_scope_and_permitted_under_bypass():
    """M5 (#12): raw() is denied unscoped rather than left lazy. ``tenancy_bypassed()``
    remains the explicit, greppable way to use it unscoped."""
    with pytest.raises(TenantScopeError, match='raw'):
        Release.objects.raw('SELECT id FROM testapp_release')

    with tenancy_bypassed():
        # A RawQuerySet is lazy like any other -- calling raw() itself must not raise.
        Release.objects.raw('SELECT id FROM testapp_release')


def test_an_unclassified_guitars_namespaced_method_is_denied_by_the_sweep_alone():
    """The whole point of the inversion: a brand-new method needs no entry added anywhere
    to be denied -- module spoofed to look guitars-authored, denied by the sweep alone."""

    class _FutureQuerySet(LiveQuerySet):
        def totally_unclassified(self):
            return list(self)

    _FutureQuerySet.totally_unclassified.__module__ = 'guitars.models.soft_deletion'
    denying = _denying_class(_FutureQuerySet)
    instance = denying(Release)

    with pytest.raises(TenantScopeError, match='totally_unclassified'):
        instance.totally_unclassified()


def test_an_unclassified_guitars_namespaced_property_is_denied_as_a_property():
    """Same as the method case, but a ``@property`` -- the sweep must replace it with
    another data descriptor, so access raises, not a call."""

    class _FutureQuerySet(LiveQuerySet):
        @property
        def totally_unclassified_property(self):
            return list(self)

    _FutureQuerySet.totally_unclassified_property.fget.__module__ = 'guitars.models.soft_deletion'
    denying = _denying_class(_FutureQuerySet)
    instance = denying(Release)

    assert inspect.isdatadescriptor(denying.__dict__['totally_unclassified_property'])
    with pytest.raises(TenantScopeError, match='totally_unclassified_property'):
        instance.totally_unclassified_property  # noqa: B018 - the access itself must raise


def test_a_consumers_own_queryset_method_is_left_reachable():
    """The sweep's one deliberate exception: a method whose module is neither Django's
    nor guitars' own is a consumer's, and stays reachable -- it can only reach the
    database through a primitive already denied."""

    class _ConsumerQuerySet(LiveQuerySet):
        def custom_report(self):
            return self.filter()

    assert _ConsumerQuerySet.custom_report.__module__ == __name__
    denying = _denying_class(_ConsumerQuerySet)
    instance = denying(Release)

    # Does not raise: chains into .filter(), itself in _ALLOWED_UNSCOPED, returning
    # another instance of the same denying class -- consuming it is what would raise.
    result = instance.custom_report()
    assert isinstance(result, denying)
