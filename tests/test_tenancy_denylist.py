"""The deny-list must not fall behind the querysets it guards.

When a required tenant scope is missing, ``TenantedManager`` hands back a queryset whose
database-touching methods raise ``TenantScopeError``. That list is maintained **by name**,
which is a standing hazard: adding a method to one of guitars' querysets, or upgrading
Django, silently produces a path that reaches the database unscoped. Before
``FORCE ROW LEVEL SECURITY`` is enabled the database will not stop it either, so the
failure mode is a silent cross-tenant read -- or, for ``hard_delete``, permanent deletion
of every tenant's rows.

So this module refuses to let a method be *undecided*. Every public method guitars adds to
a queryset must be either denied or explicitly declared safe, with a reason. A new method
fails these tests until someone classifies it.
"""

import inspect

import pytest

from guitars.models import HardDeletableQuerySet, LiveQuerySet
from guitars.tenancy.manager import _untenanted_queryset_class


#: Methods guitars adds to its querysets that do NOT reach the database, with the reason.
#: Lazy, queryset-returning members are safe because they chain into a denying clone --
#: evaluating that clone is what raises, at the ``_fetch_all`` chokepoint.
SAFE_BY_DESIGN = {
    'lives': 'property returning self.filter(...) -- lazy, chains into a denying clone',
    'archives': 'property returning self.filter(...) -- lazy, chains into a denying clone',
}

#: Django's own database-touching API, which the deny-list also has to cover. Frozen here
#: so a Django upgrade that renames or adds one of these surfaces as a failure rather than
#: as a hole. ``none``/``create``/``bulk_create`` are handled but not by simple denial
#: (``none`` stays usable; the writes report-or-raise per enforcement mode), so they are
#: asserted separately below.
MUST_BE_DENIED = {
    '_fetch_all',
    'count',
    'exists',
    'aggregate',
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
}

_QUERYSETS = [LiveQuerySet, HardDeletableQuerySet]


def _own_members(cls) -> set[str]:
    """Names *this* class declares, excluding dunders.

    Django's own QuerySet API is covered by MUST_BE_DENIED rather than enumerated here;
    what matters for drift is what guitars itself adds.
    """
    return {name for name in vars(cls) if not name.startswith('__')}


def _denying_class(base):
    return _untenanted_queryset_class(base)


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
        f'the database, add it to the deny-list in guitars/tenancy/manager.py; if it is '
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
        'guitars/tenancy/manager.py and to MUST_BE_DENIED here.'
    )


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
