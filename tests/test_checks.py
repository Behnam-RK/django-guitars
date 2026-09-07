"""Tests for `guitars.checks` -- the model *shapes* the enforcement layer cannot express.
A shape it emits a rule for anyway is worse than one it refuses: the rule keeps the child's
row while the ancestor's unguarded DELETE removes what that row points at."""

from django.core.checks import registry
from django.db.models import AutoField, Model
from django.test.utils import isolate_apps

from guitars.checks import (
    ORPHAN_ANCESTOR_ID,
    check_soft_deletable_mti_children_have_a_soft_deletable_ancestor as _check,
)
from guitars.checks import orphaned_soft_delete_ancestors, refuses_soft_delete_rule
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
        return [
            # The message states the 2.7.0 direction, not the pre-2.7.0 one: refused means no
            # rule, and no rule destroys, where the rule it once got only aborted.
            (
                e.id,
                e.obj is NeonMarquee,
                Marquee._meta.label in e.msg,
                'destroys the chain' in e.msg,
            )
            for e in errors
        ]

    assert _build() == [(ORPHAN_ANCESTOR_ID, True, True, True)]


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
    # The --skip-checks operator is the one reader for whom the live outcome is destruction.
    assert any('destroys the chain' in note for note in command._skipped_rule_notes)


def test_the_refusal_reaches_a_descendant_of_the_refused_model():
    """A concrete child of a refused model declares nothing itself, so the check passes it over
    -- and it would fall through to the MTI redirect rule, ``DO INSTEAD``, keeping exactly the
    row the refusal lets go and dangling at COMMIT one table further down."""
    command = Command()
    command._skipped_rule_notes.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class Pylon(DutarModel):
            class Meta:
                app_label = 'testapp'

        class LitPylon(Pylon, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        class NeonPylon(LitPylon):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        rendered = ' '.join(command._build_operations(_Config(Pylon, LitPylon, NeonPylon)))
        return (
            'MTI Soft Delete Rule on "testapp_neonpylon"' in rendered,
            orphaned_soft_delete_ancestors([NeonPylon]),
            [owner.__name__ for owner, _ in refuses_soft_delete_rule(NeonPylon)],
        )

    emits_a_redirect_rule, declares_the_column, refused_for = _build()

    assert not emits_a_redirect_rule
    # The check names the *declaring* model alone -- one finding per root cause, and making
    # that ancestor soft-deletable fixes the descendant with it. The generator asks the wider
    # question, since ``--skip-checks`` is the path it exists to cover.
    assert declares_the_column == []
    assert refused_for == ['LitPylon']
    assert any(
        'testapp_neonpylon' in note and 'LitPylon' in note for note in command._skipped_rule_notes
    )


def test_a_model_with_no_deleted_at_is_refused_nothing():
    """``refuses_soft_delete_rule`` is asked of every model the generator walks, most of which
    have no ``_deleted_at`` to resolve an owner for."""

    @isolate_apps('tests.testapp')
    def _build():
        class Turnstile(DutarModel):
            class Meta:
                app_label = 'testapp'

        return refuses_soft_delete_rule(Turnstile)

    assert _build() == []


def test_a_child_joining_a_soft_deletable_parent_to_a_plain_one_is_an_error():
    """Two concrete parents: the child *inherits* ``_deleted_at`` from one and still sits over
    the other, which ``Collector`` deletes unguarded all the same. Owning the column is the
    special case; carrying it over a plain parent is the shape."""

    @isolate_apps('tests.testapp')
    def _build():
        # Two concrete parents need distinct primary keys and no shared metadata columns,
        # which is why the plain side is a bare ``Model`` here rather than a ``DutarModel``.
        class Plinth(Model):
            plinth_id = AutoField(primary_key=True)

            class Meta:
                app_label = 'testapp'

        class Statue(SetarModel):
            statue_id = AutoField(primary_key=True)

            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        class MountedStatue(Plinth, Statue):
            class Meta:  # not SetarModel.Meta: its index names a column not local here
                app_label = 'testapp'

        assert MountedStatue.check() == []  # Django itself takes the shape
        errors = _check([_Config(MountedStatue)])
        return (
            [(e.id, e.obj is MountedStatue, Plinth._meta.label in e.msg) for e in errors],
            any(Statue._meta.label in e.msg for e in errors),  # the soft-deletable side is fine
            [
                (child.__name__, parent.__name__)
                for child, parent in refuses_soft_delete_rule(MountedStatue)
            ],
            # Guarded, so the unfixed predicate reports an empty list rather than an IndexError.
            bool(errors)
            and 'inherits _deleted_at' in errors[0].hint
            and 'abstract' in errors[0].hint,
        )

    assert _build() == (
        [(ORPHAN_ANCESTOR_ID, True, True)],
        False,
        [('MountedStatue', 'Plinth')],
        True,
    )


def test_the_generator_refuses_the_joining_shape_and_its_descendant():
    """Both halves passed this shape over: the check gated on *owning* the column, and the
    generator asked only the column's owner, which has no plain parent. The redirect rule it
    then emitted keeps the child's row while the plain parent's DELETE removes what it points at."""
    command = Command()
    command._skipped_rule_notes.clear()

    @isolate_apps('tests.testapp')
    def _build():
        class Pedestal(Model):
            pedestal_id = AutoField(primary_key=True)

            class Meta:
                app_label = 'testapp'

        class Bust(SetarModel):
            bust_id = AutoField(primary_key=True)

            class Meta(SetarModel.Meta):
                app_label = 'testapp'

        class MountedBust(Pedestal, Bust):
            class Meta:  # not SetarModel.Meta: its index names a column not local here
                app_label = 'testapp'

        class GildedBust(MountedBust):
            class Meta:  # not SetarModel.Meta: its index names a column not local here
                app_label = 'testapp'

        rendered = ' '.join(
            command._build_operations(_Config(Pedestal, Bust, MountedBust, GildedBust))
        )
        return (
            'MTI Soft Delete Rule on "testapp_mountedbust"' in rendered,
            'MTI Soft Delete Rule on "testapp_gildedbust"' in rendered,
            'Soft Delete Rule on "testapp_bust"' in rendered,  # the healthy parent keeps its own
        )

    assert _build() == (False, False, True)
    assert any(
        'testapp_gildedbust' in note and 'MountedBust' in note
        for note in command._skipped_rule_notes
    )


def test_the_hint_names_the_root_of_the_chain_rather_than_the_next_hop():
    """Making the immediate parent soft-deletable under a plain grandparent moves the orphan up
    one table and earns a second E003; the hint has to name the move that ends it."""

    @isolate_apps('tests.testapp')
    def _build():
        class Footing(DutarModel):
            class Meta:
                app_label = 'testapp'

        class Column(Footing):
            class Meta:
                app_label = 'testapp'

        class LitColumn(Column, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        (error,) = _check([_Config(LitColumn)])
        return (
            Column._meta.label in error.msg,  # the parent it meets is still what the message names
            Footing._meta.label in error.hint,
            f"'{Column._meta.label}' soft-deletable" in error.hint,
            LitColumn._meta.label in error.hint,  # and the declaration to drop
        )

    assert _build() == (True, True, False, True)


def test_the_hint_over_a_diamond_of_plain_roots_says_restructure_rather_than_naming_one():
    """``mti_root`` follows one parent. A declaring child over a parent with *two* plain roots
    cannot give both the column -- a field reaching a model from two bases is a clash -- so
    naming one root sends the operator straight into the join shape this check also refuses."""

    @isolate_apps('tests.testapp')
    def _build():
        class Beam(Model):
            beam_id = AutoField(primary_key=True)

            class Meta:
                app_label = 'testapp'

        class Post(Model):
            post_id = AutoField(primary_key=True)

            class Meta:
                app_label = 'testapp'

        class Frame(Beam, Post):
            class Meta:
                app_label = 'testapp'

        class LitFrame(Frame, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        (error,) = _check([_Config(LitFrame)])
        return (
            Beam._meta.label in error.hint and Post._meta.label in error.hint,
            'Make the plain side abstract' in error.hint,
            "Make 'testapp.Beam' soft-deletable" in error.hint,  # the label keeps its case
        )

    assert _build() == (True, True, False)


def test_the_hint_over_two_direct_plain_parents_names_both_and_says_restructure():
    """The V one hop lower than the diamond: the roots are the child's own parents. Read off the
    one parent in each ``(child, parent)`` finding, the hint named a single root per finding and
    following either walked into the join shape."""

    @isolate_apps('tests.testapp')
    def _build():
        class Rail(Model):
            rail_id = AutoField(primary_key=True)

            class Meta:
                app_label = 'testapp'

        class Stile(Model):
            stile_id = AutoField(primary_key=True)

            class Meta:
                app_label = 'testapp'

        class LitGate(Rail, Stile, SoftDeletableModel):
            class Meta(SoftDeletableModel.Meta):
                app_label = 'testapp'

        errors = _check([_Config(LitGate)])
        return len(errors), [
            (
                Rail._meta.label in e.hint and Stile._meta.label in e.hint,
                'Make the plain side abstract' in e.hint,
                'soft-deletable (SetarModel' in e.hint,
            )
            for e in errors
        ]

    count, hints = _build()
    assert count == 2  # one finding per plain parent it sits over
    assert hints == [(True, True, False)] * 2
