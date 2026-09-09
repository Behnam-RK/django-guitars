"""A proxy is not a multi-table-inheritance child (2.9.1, issue #45). Django fills its
``_meta.parents`` and it declares no field, so it read as one -- and sharing its concrete
model's table, the operations it earned named that table as their own parent."""

import pytest
from django.apps import apps as django_apps
from django.test.utils import isolate_apps

from guitars.introspection import is_mti_child
from guitars.management.enforcement.command import Command
from guitars.models import SetarModel
from tests.testapp.models import Ensemble, Orchestra


class _Config:
    """The minimal ``AppConfig`` face ``_build_operations`` reads, so a test can hand it an
    exact model list rather than the whole registry."""

    label = 'testapp'

    def __init__(self, *models):
        self._models = models

    def get_models(self):
        return list(self._models)


def _withdraw(*names: str) -> None:
    """``AppConfig.models`` *is* ``apps.all_models[label]``, so one pop withdraws both. Left
    registered, a proxy joins every later test's model sweep -- the reason the tenancy suite's
    own proxy fixtures pop theirs too."""
    for name in names:
        django_apps.all_models['testapp'].pop(name, None)
    django_apps.clear_cache()


@pytest.fixture
def plain_proxy():
    """A proxy over a plain soft-deletable model, registered for one test."""

    @isolate_apps('tests.testapp')
    def _build():
        class Plain(SetarModel):
            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        class PlainProxy(Plain):
            class Meta:
                app_label = 'testapp'
                proxy = True

        return Plain, PlainProxy

    plain, proxy = _build()
    yield plain, proxy
    _withdraw('plain', 'plainproxy')


def test_a_proxy_is_not_an_mti_child(plain_proxy):
    """The predicate itself, which three callers ask. ``bool(_meta.parents)`` is true for a
    proxy and ``owns_column`` false, which is the whole trap."""
    _plain, proxy = plain_proxy

    assert proxy._meta.parents  # the trap: Django fills this for a proxy too
    assert not proxy._meta.local_fields
    assert is_mti_child(proxy, '_updated_at') is False
    assert is_mti_child(proxy, '_deleted_at') is False


def test_a_proxy_adds_no_operation_of_its_own(plain_proxy):
    """Two operations, both the concrete model's. Before the fix there were four: the proxy
    earned an MTI trigger and an MTI rule, each naming ``testapp_plain`` as its own parent."""
    plain, proxy = plain_proxy

    headers = [
        line
        for operation in Command()._build_operations(_Config(plain, proxy))
        for line in operation.splitlines()
        if line.startswith('#')
    ]

    assert len(headers) == 2
    assert not [header for header in headers if header.startswith('# MTI')]


def test_a_proxy_alone_does_not_call_for_the_parent_trigger_function(plain_proxy):
    """The third symptom, and the one the loop through ``_build_operations`` cannot reach:
    ``needs_parent_function`` walks its own model list, so a proxy would have forced the MTI
    parent trigger-function migration into a project with no MTI at all."""
    _plain, proxy = plain_proxy

    assert is_mti_child(proxy, '_updated_at') is False


@pytest.fixture
def mti_child_proxy():
    """A proxy over a *real* MTI child, the shape where the concrete model legitimately does
    earn MTI operations and the proxy must add none."""

    @isolate_apps('tests.testapp')
    def _build():
        class OrchestraProxy(Orchestra):
            class Meta:
                app_label = 'testapp'
                proxy = True

        return OrchestraProxy

    proxy = _build()
    yield proxy
    _withdraw('orchestraproxy')


def test_a_proxy_of_an_mti_child_adds_nothing_and_leaves_the_child_alone(mti_child_proxy):
    """The child keeps every operation its own inheritance earns; the proxy contributes none.
    Both would otherwise key on ``testapp_orchestra`` and collide rather than add."""
    def _emit(*models):
        # Recorded coverage cleared, or the corpus's own MTI keys make this emit nothing and
        # the comparison would hold for the wrong reason.
        command = Command()
        command.existing.mti_triggers.clear()
        command.existing.mti_soft_deletes.clear()
        return command._build_operations(_Config(*models))

    with_proxy = _emit(Ensemble, Orchestra, mti_child_proxy)
    without = _emit(Ensemble, Orchestra)

    assert with_proxy == without
    assert [line for operation in without for line in operation.splitlines() if '# MTI' in line]


# --- The note for a migration written before the fix -----------------------------------------


def test_a_recorded_mti_key_nothing_requires_is_named():
    """Nothing can be retired -- the migration never applied, so no database holds the object
    -- but the file stays broken, and the generator cannot repair a file. So it says so."""
    command = Command()
    # ``testapp_band`` is a plain model's table: hosted, and no model reaches a column through
    # an ancestor there. That is exactly the shape a proxy left behind.
    command.existing.mti_triggers['testapp_band'] = 'abc'

    (note,) = command._orphaned_mti_notes()

    assert "MTI Updated at Trigger on 'testapp_band' is recorded" in note
    assert 'a proxy model earned it before 2.9.1' in note
    assert 'Delete the operation from the migration' in note


def test_a_recorded_mti_key_on_an_unmapped_table_stays_silent():
    """Positive evidence only. A table mapping to no local model is a deleted model on one
    reading and a scoped run on another, and following the wrong one is how a live object gets
    dropped -- so that case is left alone, as the cascade and autofill families leave it."""
    command = Command()
    command.existing.mti_triggers['shop_gone'] = 'abc'

    assert command._orphaned_mti_notes() == []


def test_the_real_corpus_produces_no_note():
    """Every MTI key the committed migrations record is still required by a concrete child, so
    a project with no proxy sees nothing."""
    assert Command()._orphaned_mti_notes() == []
