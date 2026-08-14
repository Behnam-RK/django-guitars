"""Tests for guitars.tenancy.manager -- that the guards it installs are reachable (see
``test_soft_deletion.py::TestManagerQuerySetClass`` for the seam). No models or database --
guard *behaviour* is covered by the tenanted-model tests."""

from __future__ import annotations

import pytest
from django.db import models

from guitars.models.soft_deletion import AllObjectsManager, ArchiveManager, LiveManager
from guitars.tenancy import (
    TenantedManagerBase,
    TenantScopeError,
    tenancy_bypassed,
    tenant,
    tenanted_manager,
)
from tests.testapp.models import Band


#: Every manager class GuitarModel will wrap, plus the plain Django default.
MANAGER_CLASSES = [models.Manager, LiveManager, ArchiveManager, AllObjectsManager]


def _bind(manager, model=Band):
    """Attach *manager* to a model outside ``contribute_to_class`` -- enough to exercise
    ``get_queryset()`` without a concrete tenanted model existing yet."""
    manager.model = model
    manager._db = None
    manager._hints = {}
    return manager


@pytest.mark.parametrize('manager_class', MANAGER_CLASSES, ids=lambda c: c.__name__)
class TestTheGuardIsOnWhatTheManagerHandsOut:
    """The scoped path must return the guarded queryset, for every wrapped manager."""

    def test_the_manager_advertises_a_guarded_queryset_class(self, manager_class):
        manager = tenanted_manager(_manager_class=manager_class, tenant='name')

        # Sanity: the class-level declaration is the thing the next test proves reachable.
        assert manager._queryset_class.__name__ == '_GuardedQuerySet'

    def test_the_scoped_queryset_carries_the_guard(self, manager_class):
        manager = _bind(tenanted_manager(_manager_class=manager_class, tenant='name'))

        with tenant(tenant='Rush'):
            queryset = manager.get_queryset()

        # `issubclass` against the advertised class: the guard has to be on the object the
        # caller receives, not merely on an attribute of the manager.
        assert isinstance(queryset, manager._queryset_class)
        assert type(queryset).bulk_create is manager._queryset_class.bulk_create

    def test_the_bypassed_queryset_carries_the_guard_too(self, manager_class):
        """Bypass turns the tenant *filter* off, not the write guard -- it's a no-op by its
        own logic (``apply_write_guard`` returns early), not because the method is missing."""
        manager = _bind(tenanted_manager(_manager_class=manager_class, tenant='name'))

        with tenancy_bypassed():
            queryset = manager.get_queryset()

        assert isinstance(queryset, manager._queryset_class)

    def test_the_unscoped_queryset_denies_instead(self, manager_class):
        """No scope: a denying queryset, not a guarded one. The other half of the fork."""
        manager = _bind(tenanted_manager(_manager_class=manager_class, tenant='name'))

        queryset = manager.get_queryset()

        assert type(queryset).__name__ == '_UntenantedQuerySet'
        with pytest.raises(TenantScopeError, match='needs an active tenant scope'):
            queryset.count()


class TestCustomQuerySetMethodsSurvive:
    """Wrapping must not cost the underlying queryset's own API. ``LiveQuerySet.lives`` is
    what ``LiveManager.get_queryset`` applies, so losing it would break soft deletion."""

    def test_on_the_scoped_path(self):
        manager = _bind(tenanted_manager(_manager_class=LiveManager, tenant='name'))

        with tenant(tenant='Rush'):
            assert hasattr(manager.get_queryset(), 'lives')

    def test_on_the_denying_path(self):
        """Present, so a missing scope raises TenantScopeError rather than AttributeError --
        the denying queryset subclasses the manager's own for exactly this."""
        manager = _bind(tenanted_manager(_manager_class=LiveManager, tenant='name'))

        queryset = manager.get_queryset()

        assert hasattr(queryset, 'lives')
        with pytest.raises(TenantScopeError):
            queryset.lives.count()


@pytest.mark.parametrize('manager_class', MANAGER_CLASSES, ids=lambda c: c.__name__)
class TestTenantedManagerBase:
    """The marker M5 (#12) added when the factory was renamed off ``TenantedManager`` --
    ``_tenant_dimensions`` was previously the only way to recognise a tenant-scoped
    manager from outside; this gives external code an actual type."""

    def test_every_manager_the_factory_builds_is_an_instance(self, manager_class):
        manager = tenanted_manager(_manager_class=manager_class, tenant='name')

        assert isinstance(manager, TenantedManagerBase)

    def test_the_wrapped_manager_class_is_unaffected(self, manager_class):
        """The marker is additive -- it must not make an ordinary manager instance of
        ``manager_class`` look tenant-scoped too."""
        assert not isinstance(manager_class(), TenantedManagerBase)

    def test_the_wrapped_managers_own_type_is_still_recognised(self, manager_class):
        """Wrapping must not cost the identity the caller wrapped -- a tenanted
        ``LiveManager`` is still, structurally, a ``LiveManager``."""
        manager = tenanted_manager(_manager_class=manager_class, tenant='name')

        assert isinstance(manager, manager_class)
