"""Tests for `guitars.checks` -- the model *shapes* the enforcement layer cannot express.
A shape it emits a rule for anyway is worse than one it refuses: the rule keeps the child's
row while the ancestor's unguarded DELETE removes what that row points at."""

from django.core.checks import registry
from django.test.utils import isolate_apps

from guitars.checks import (
    ORPHAN_ANCESTOR_ID,
    check_soft_deletable_mti_children_have_a_soft_deletable_ancestor as _check,
)
from guitars.checks import orphaned_soft_delete_ancestors
from guitars.management.enforcement.command import Command
from guitars.models import DutarModel, SetarModel, SoftDeletableModel
from tests.testapp.models import Arena, Placard, SpotlitPlacard, Venue


class _Config:
    """The surface ``_candidate_models`` and ``_build_operations`` read off an app config.
    ``isolate_apps`` builds a registry of its own, so the real one cannot reach these."""

    def __init__(self, *models, label='testapp'):
        self._models = models
        self.label = label

    def get_models(self):
        return list(self._models)


def test_a_soft_deletable_child_under_a_plain_ancestor_is_an_error():
    """Django's Collector issues one DELETE per table in the chain: the child's is rewritten
    to an UPDATE and its row survives, the ancestor's is unguarded and its row goes, so the
    parent-link constraint fails at COMMIT. No runtime path in the kit can spare it."""

    @isolate_apps('tests.testapp')
    def _build():
        class Marquee(DutarModel):
            class Meta:
                app_label = 'testapp'

        class NeonMarquee(Marquee, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        # Through an app-config stub: ``isolate_apps`` gives the models their own registry,
        # so the check's default ``django_apps.get_models()`` never sees them.
        errors = _check([_Config(NeonMarquee)])
        return [(e.id, e.obj is NeonMarquee, Marquee._meta.label in e.msg) for e in errors]

    assert _build() == [(ORPHAN_ANCESTOR_ID, True, True)]


def test_the_error_names_the_ancestor_to_make_soft_deletable():
    """The operator has to know which ancestor to change; naming only the child leaves them
    guessing at a chain that may be three deep."""

    @isolate_apps('tests.testapp')
    def _build():
        class Hoarding(DutarModel):
            class Meta:
                app_label = 'testapp'

        class LitHoarding(Hoarding, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        (error,) = _check([_Config(LitHoarding)])
        return Hoarding._meta.label in error.hint, 'SetarModel' in error.hint

    assert _build() == (True, True)


def test_the_ordinary_mti_shape_is_not_reported():
    """``_deleted_at`` on the ancestor is the supported shape -- the child gets the MTI
    redirect rule, and the ancestor's own rule guards its DELETE."""
    assert orphaned_soft_delete_ancestors([Arena, SpotlitPlacard]) == []
    assert orphaned_soft_delete_ancestors([Venue, Placard]) == []


def test_a_non_mti_soft_deletable_model_is_not_reported():
    """The check is about an ancestor that cannot be stamped, not about declaring the column:
    a model with no parents declares ``_deleted_at`` on its own table and always has."""

    @isolate_apps('tests.testapp')
    def _build():
        class Lonely(SetarModel):
            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        return orphaned_soft_delete_ancestors([Lonely])

    assert _build() == []


def test_the_check_reports_only_the_apps_it_was_asked_about():
    """A scoped ``manage.py check <app>`` run must not answer a question it wasn't asked."""

    class _EmptyConfig:
        @staticmethod
        def get_models():
            return []

    @isolate_apps('tests.testapp')
    def _build():
        class Sandwich(DutarModel):
            class Meta:
                app_label = 'testapp'

        class BoardSandwich(Sandwich, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        return _check([_EmptyConfig]), len(_check([_Config(BoardSandwich)]))

    scoped, unscoped = _build()
    assert scoped == []
    assert unscoped == 1


def test_the_shape_check_is_registered():
    """Registered where the tenancy checks are, so ``manage.py check`` runs it without
    ``guitars`` being in INSTALLED_APPS -- it is a library first."""
    registered = {check.__name__ for check in registry.registry.get_checks()}

    assert 'check_soft_deletable_mti_children_have_a_soft_deletable_ancestor' in registered


def test_the_generator_refuses_the_rule_rather_than_trusting_the_check():
    """``--skip-checks`` reaches the generator and ``hard_delete()`` runs no checks at all, so
    the generator re-asks. Emitting the rule is what makes the shape abort at COMMIT."""
    command = Command()
    command._skipped_rule_notes.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class Gantry(DutarModel):
            class Meta:
                app_label = 'testapp'

        class LitGantry(Gantry, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        rendered = ' '.join(command._build_operations(_Config(LitGantry)))
        # The header, not the table: an MTI ``_updated_at`` trigger legitimately names it.
        return 'Soft Delete Rule on "testapp_litgantry"' in rendered

    emits_a_rule = _build()

    assert not emits_a_rule
    assert any('testapp_litgantry' in note for note in command._skipped_rule_notes)
    assert any('aborting at COMMIT' in note for note in command._skipped_rule_notes)
