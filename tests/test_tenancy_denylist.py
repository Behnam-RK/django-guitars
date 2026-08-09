"""The deny-list must not fall behind the querysets it guards.

When a required tenant scope is missing, ``TenantedManager`` hands back a queryset whose
database-touching methods raise ``TenantScopeError``. That list is maintained **by name**,
which is a standing hazard: adding a method to one of guitars' querysets, or upgrading
Django, silently produces a path that reaches the database unscoped. Before
``FORCE ROW LEVEL SECURITY`` is enabled the database will not stop it either, so the
failure mode is a silent cross-tenant read -- or, for ``hard_delete``, permanent deletion
of every tenant's rows.

So this module refuses to let a method be *undecided*. Every public method guitars adds to
a queryset must be either denied or explicitly declared safe, with a reason -- and, lower
down, so must every public method **Django's own** ``QuerySet`` declares: a hand-maintained
frozen set (``MUST_BE_DENIED``, historical) only ever names what someone remembered to add,
so a Django release that adds a queryset method used to leak silently past every test here.
``test_djangos_own_queryset_surface_is_fully_classified`` walks
``django.db.models.QuerySet`` itself instead, so a new method fails loudly by name until
someone classifies it -- which is what makes the CI matrix in ``ci.yml`` (Python x Django x
Postgres) worth having: this drift check now runs against three different Django versions,
not one.
"""

import inspect

import pytest
from django.db import connections
from django.db.models import QuerySet as DjangoQuerySet

from guitars.models import HardDeletableQuerySet, LiveQuerySet
from guitars.tenancy import TenantScopeError, tenancy_bypassed
from guitars.tenancy.querysets import _ALLOWED_UNSCOPED, _untenanted_queryset_class
from tests.testapp.models import Release


#: The runtime allow-list (guitars/tenancy/querysets.py) is the source of truth the sweep
#: actually reads; this module's classification below documents and cross-checks it, but
#: does not duplicate it -- see test_allowed_unscoped_methods_still_exist_upstream and
#: test_allowed_unscoped_methods_all_carry_a_reason.
SAFE_BY_DESIGN = _ALLOWED_UNSCOPED

#: Historical: the frozen subset of Django's database-touching API this module named
#: before the dynamic check below existed. Kept, and still asserted against below by
#: ``test_documented_database_methods_are_denied``, because these are pinned findings
#: (``hard_delete``, ``_raw_delete``, ``explain`` were each once missing from the ported
#: deny-list) -- but a Django *addition* is now caught by
#: ``test_djangos_own_queryset_surface_is_fully_classified`` instead of by remembering to
#: extend this set. ``none``/``create``/``bulk_create`` are handled but not by simple
#: denial (``none`` stays usable; the writes report-or-raise per enforcement mode), so
#: they are asserted separately below.
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
    """Names *this* class declares, excluding dunders.

    Django's own QuerySet API used to be covered by MUST_BE_DENIED alone -- a frozen
    literal someone had to remember to extend. It is now cross-checked dynamically too,
    below, against django.db.models.QuerySet itself.
    """
    return {name for name in vars(cls) if not name.startswith('__')}


def _denying_class(base):
    return _untenanted_queryset_class(base)


# ──────────────────────── Django's own QuerySet surface ───────────────────────── #
#
# MUST_BE_DENIED, above, is what a human remembered to write down. The tests in this
# section instead walk django.db.models.QuerySet itself, so a method Django adds --
# not one guitars adds -- fails loudly by name instead of shipping unclassified. Checked
# live against Django 5.0.14, 5.2.15 and 6.0.6 while writing this: all three expose the
# identical 68 public members classified below, so there is no drift today; this is what
# catches the next one.
#
# Manager.__dict__ is deliberately not enumerated here: both Django versions checked
# have zero own non-dunder members on django.db.models.Manager (most Manager methods are
# proxied from QuerySet via from_queryset, so there is nothing of Manager's own to
# classify), which matches ADR 0004's finding that Manager is not a distinct enforcement
# surface -- the gap on the save() path it describes is real, but it is a Model method
# (_do_insert/_do_update), not a Manager one, and out of scope for a QuerySet drift test.

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

#: Not separately overridden -- but every one of these is implemented, in Django itself,
#: by delegating to an already-denied primitive. The nine async members call
#: sync_to_async(self.<sync method>) (verified with inspect.getsource on all nine while
#: writing this), so self.<sync method> already resolves to the override on the denying
#: class. get/first/last/earliest/latest/in_bulk/contains/get_or_create/update_or_create
#: all build a clone of self (same denying class) and then iterate, .get(), or .exists()
#: it, which is the _fetch_all/exists chokepoint again. No production code needed these
#: added to querysets.py; TestDeniedViaChainActuallyRaises below proves it behaviorally
#: rather than trusting this reasoning statically.
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

#: Overridden individually in querysets.py, not aliased to a bare denial, so audit mode can
#: report and proceed instead of hard-raising. Asserted specially in
#: test_row_creating_writes_are_intercepted_rather_than_plainly_denied and
#: test_none_stays_usable_on_an_unscoped_queryset, below.
DJANGO_INDIVIDUALLY_HANDLED = {'abulk_create', 'acreate', 'bulk_create', 'create', 'none'}

#: Chain-building or metadata: return a new (lazy) queryset, or a value that never
#: required a query, so they never touch the database by themselves. Must match
#: _ALLOWED_UNSCOPED's Django-side names exactly -- see
#: test_django_lazy_safe_matches_allowed_unscoped.
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

#: *Calling* raw() doesn't touch the database -- it returns a RawQuerySet -- but that
#: RawQuerySet is a distinct class that never passes back through the denying queryset at
#: all, so leaving it lazy would hand out an unscoped escape hatch. M5 (#12) resolved the
#: inconsistency with hard_delete (denied on the same "the database's job" reasoning) by
#: denying raw() too: it is simply absent from _ALLOWED_UNSCOPED, so
#: _apply_default_deny_sweep denies it like any other unclassified Django method -- no
#: explicit alias in guitars/tenancy/querysets.py needed. See
#: test_raw_is_denied_without_a_scope_and_permitted_under_bypass.
DJANGO_DENIED_BY_SWEEP = {'raw'}


def _django_queryset_public_members() -> set[str]:
    """django.db.models.QuerySet's own public members.

    Private helpers (_clone, _chain, _merge_sanity_check, ...) are deliberately excluded
    from this enumeration: Django routes every independent database-touching path through
    the public methods classified above, or through _fetch_all (DJANGO_DENIED_DIRECTLY) --
    private helpers are plumbing invoked *by* those public methods, not separate entry
    points of their own, so classifying dozens of them by hand would add volume without
    adding coverage.
    """
    return {name for name in vars(DjangoQuerySet) if not name.startswith('_')}


def test_djangos_own_queryset_surface_is_fully_classified():
    """The drift test this module used to be missing.

    Not parametrized over LiveQuerySet/HardDeletableQuerySet like the tests below --
    Django's QuerySet class is the same one whichever guitars queryset subclasses it, so
    there is exactly one surface to check, not two.
    """
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


#: The two DJANGO_DENIED_VIA_CHAIN names that open a real connection before ever reaching
#: the denial: Django wraps update_or_create/aupdate_or_create's whole body in
#: transaction.atomic(using=self.db) *before* the internal get() that would raise, so
#: entering the method at all needs a connection -- the denial still fires from get(),
#: just one frame later than for get_or_create. These carry an explicit django_db mark
#: below; everything else must NOT -- a name that turns out to need one is a sign the
#: reasoning needs re-checking, not an opportunity to mark it "just in case".
_NEEDS_DB = {'update_or_create', 'aupdate_or_create'}


def _chain_params(names):
    return [
        pytest.param(name, marks=pytest.mark.django_db(transaction=True))
        if name in _NEEDS_DB
        else name
        for name in sorted(names)
    ]


class TestDeniedViaChainActuallyRaises:
    """Proof, not just reasoning: every DJANGO_DENIED_VIA_CHAIN member actually raises.

    ``Release`` is a GuitarModel, so ``Release.objects`` with no active tenant scope is
    the denying queryset every name here reasons about. No test is marked ``db`` --
    same reasoning as ``test_acreate_is_denied_without_a_scope`` in
    test_tenancy_internals.py: the denial fires from a pure-Python override before the
    chain ever compiles SQL, so no real lookup values, an existing row, or a database
    connection are needed -- only arguments shaped enough for Python to accept the call.
    An async test that *did* open one without closing it would leak a connection past
    this session's teardown; not opening one at all is simpler than remembering to.

    ``update_or_create``/``aupdate_or_create`` are the one exception -- see ``_NEEDS_DB``
    above. The async one also needs the ``_close_executor_connections`` fixture below,
    for the same reason ``TestAsyncTwins`` in test_tenancy_internals.py does.
    """

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
        """See TestAsyncTwins in test_tenancy_internals.py: a harmless no-op for the
        17 of 19 cases that never opened a connection in the first place."""
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
    """No member of a guitars queryset may be left undecided.

    This is the test that catches the next ``hard_delete``: a method added to a queryset
    without anyone deciding whether an unscoped caller may reach the database through it.
    """
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
    """Every name in MUST_BE_DENIED must exist somewhere, or the list is lying.

    ``test_documented_database_methods_are_denied`` skips names a queryset does not have
    (``hard_delete`` is absent from ``LiveQuerySet``). That skip means a **typo** in
    MUST_BE_DENIED would be quietly ignored, leaving the list looking longer than the
    protection it actually asserts. Verified separately so the skip stays narrow.
    """
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
    """Pin both halves of the finding that motivated this module.

    ``hard_delete`` was absent from the ported deny-list. It is also sync-only, so a
    future ``ahard_delete`` must be denied deliberately rather than assumed absent -- this
    fails when one is added, prompting that decision.
    """
    denying = _denying_class(HardDeletableQuerySet)

    assert 'hard_delete' in _own_members(denying)
    assert not hasattr(HardDeletableQuerySet, 'ahard_delete'), (
        'HardDeletableQuerySet gained an async hard_delete -- add it to the deny-list in '
        'guitars/tenancy/querysets.py and to MUST_BE_DENIED here.'
    )


def test_raw_delete_and_explain_are_denied():
    """Pin the finding that motivated adding these two: ``_raw_delete`` compiles a
    ``DeleteQuery`` straight off ``self.query`` and executes it -- unscoped, an
    unfiltered DELETE across every tenant -- and ``explain`` executes an ``EXPLAIN``,
    bypassing ``_fetch_all`` entirely. Both were absent from the ported deny-list."""
    denying = _denying_class(LiveQuerySet)

    assert '_raw_delete' in _own_members(denying)
    assert 'explain' in _own_members(denying)


def test_none_stays_usable_on_an_unscoped_queryset():
    """``.none()`` must not raise: framework-level empties resolve through it."""
    denying = _denying_class(HardDeletableQuerySet)

    assert 'none' in _own_members(denying)
    assert not inspect.isdatadescriptor(denying.none)


def test_row_creating_writes_are_intercepted_rather_than_plainly_denied():
    """``create``/``bulk_create`` route through the enforcement mode, not a bare raise.

    They must be *overridden* (so audit mode can report and proceed) rather than aliased to
    the denying helper, which would raise unconditionally and defeat audit mode.
    """
    denying = _denying_class(HardDeletableQuerySet)
    overridden = _own_members(denying)

    for name in ('create', 'acreate', 'bulk_create', 'abulk_create'):
        assert name in overridden, f'{name} is not intercepted on an unscoped queryset'
        assert denying.__dict__[name] is not denying.__dict__.get('_deny_query_write'), (
            f'{name} is aliased to the bare denial, so audit mode could not report-and-proceed'
        )


# ────────────────────────── the allow-list sweep itself ───────────────────────── #


def test_allowed_unscoped_methods_still_exist_upstream():
    """_ALLOWED_UNSCOPED is the sweep's actual source of truth (guitars/tenancy/querysets.py);
    a name it lists that exists nowhere -- Django renamed or removed it, or a guitars
    queryset method was renamed -- is dead weight silently no longer meaning anything.
    """
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
    """DJANGO_LAZY_SAFE documents Django's own lazy/metadata surface for the drift test
    above; _ALLOWED_UNSCOPED is what the sweep actually reads at runtime. They must name
    the same Django methods (plus guitars' own lives/archives), or this module's
    documentation and the runtime allow-list have drifted apart.
    """
    assert set(_ALLOWED_UNSCOPED) == DJANGO_LAZY_SAFE | {'lives', 'archives'}


def test_raw_is_denied_without_a_scope_and_permitted_under_bypass():
    """M5 (#12): raw() is now denied unscoped rather than left lazy -- see
    DJANGO_DENIED_BY_SWEEP's comment for why leaving it allowed would be a real gap.
    tenancy_bypassed() remains the explicit, greppable way to use it unscoped.
    """
    with pytest.raises(TenantScopeError, match='raw'):
        Release.objects.raw('SELECT id FROM testapp_release')

    with tenancy_bypassed():
        # A RawQuerySet is lazy like any other -- calling raw() itself must not raise.
        Release.objects.raw('SELECT id FROM testapp_release')


def test_an_unclassified_guitars_namespaced_method_is_denied_by_the_sweep_alone():
    """The whole point of the inversion: a brand-new method needs no entry added to
    _ALLOWED_UNSCOPED, MUST_BE_DENIED, or any other bucket in this file to be denied.
    Defined here with its module spoofed to look guitars-authored -- matching what a real
    addition to guitars/models/soft_deletion.py would look like -- and denied purely by
    _apply_default_deny_sweep noticing nothing classified it.
    """

    class _FutureQuerySet(LiveQuerySet):
        def totally_unclassified(self):
            return list(self)

    _FutureQuerySet.totally_unclassified.__module__ = 'guitars.models.soft_deletion'
    denying = _denying_class(_FutureQuerySet)
    instance = denying(Release)

    with pytest.raises(TenantScopeError, match='totally_unclassified'):
        instance.totally_unclassified()


def test_an_unclassified_guitars_namespaced_property_is_denied_as_a_property():
    """Same as the method case above, but for a ``@property`` -- the sweep must replace it
    with another data descriptor (so ``instance.name`` raises on *access*, not on a call)
    rather than a plain denying function that would need to be invoked to fire.
    """

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
    """The sweep's one deliberate exception: a method whose defining module is neither
    Django's nor guitars' own is a downstream consumer's, and stays lazy/reachable --
    it can only reach the database through a primitive this module already denies.
    """

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
