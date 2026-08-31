"""Startup validation for model *shapes* the enforcement layer cannot express, as
``guitars.tenancy.checks`` does for the tenancy settings. One shape so far, and it is the
one that destroys a row rather than sparing it."""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.checks import Error, register
from django.db import models

from guitars.introspection import column_owner, has_column, owns_column


__all__ = [
    'ORPHAN_ANCESTOR_ID',
    'check_soft_deletable_mti_children_have_a_soft_deletable_ancestor',
    'refuses_soft_delete_rule',
    'register_checks',
]

#: Namespaced to match the field's own ``guitars.E001``/``E002``.
ORPHAN_ANCESTOR_ID = 'guitars.E003'


def _candidate_models(app_configs) -> list[type[models.Model]]:
    """The models a check should consider -- reporting outside the requested apps would
    make a scoped ``manage.py check <app>`` run answer a question it wasn't asked."""
    if app_configs is None:
        return list(django_apps.get_models())
    return [model for config in app_configs for model in config.get_models()]


def orphaned_soft_delete_ancestors(
    candidates: list[type[models.Model]],
) -> list[tuple[type[models.Model], type[models.Model]]]:
    """``(child, ancestor)`` for every concrete MTI child declaring ``_deleted_at`` on its own
    table under an ancestor that has none -- the *declaring* model alone, one finding per root
    cause; :func:`refuses_soft_delete_rule` is what covers its descendants."""
    found = []
    for model in candidates:
        if not model._meta.parents or not owns_column(model, '_deleted_at'):
            continue
        # No parent of such a model can carry ``_deleted_at`` itself: Django rejects a local
        # field clashing with a base's, at any depth, so owning it locally already means every
        # ancestor lacks it. Filtering per parent would be a branch nothing can take.
        found.extend((model, parent) for parent in model._meta.parents)
    return found


def refuses_soft_delete_rule(
    model: type[models.Model],
) -> list[tuple[type[models.Model], type[models.Model]]]:
    """``(owner, ancestor)`` for every reason *model* must get no soft-delete rule of any kind
    -- the generator's own question, re-asked rather than trusted from ``guitars.E003``, which
    ``--skip-checks`` walks straight past and ``hard_delete()`` never runs at all."""
    # Asked of the column's **owner**, not of *model*: a concrete child of a refused model
    # inherits ``_deleted_at``, so it declares nothing of its own and
    # ``orphaned_soft_delete_ancestors`` passes it over.

    # It would then fall through to the MTI redirect rule -- ``DO INSTEAD``, keeping exactly
    # the row the refusal exists to let go, its parent-link dangling at ``COMMIT`` all the
    # same, one table further down.
    if not has_column(model, '_deleted_at'):
        return []
    return orphaned_soft_delete_ancestors([column_owner(model, '_deleted_at')])


def check_soft_deletable_mti_children_have_a_soft_deletable_ancestor(
    app_configs, **kwargs
) -> list[Error]:
    """Django's ``Collector`` issues one ``DELETE`` per table in an MTI chain. A child
    declaring ``_deleted_at`` keeps its row through its own rule, while the ancestor, having no
    column to stamp, gets no rule and is really deleted."""
    # An error rather than a warning: the row does not merely go unstamped, the statement
    # aborts at COMMIT on the child's own parent-link constraint. Nothing is recoverable
    # from it at runtime, and no code path in the kit can spare it.
    return [
        Error(
            f"'{child._meta.label}' declares _deleted_at on its own table while its "
            f"multi-table-inheritance ancestor '{parent._meta.label}' declares none, so "
            f"deleting one aborts at COMMIT: the child's soft-delete rule keeps its row "
            f"while the ancestor's DELETE, which no rule guards, removes the row that "
            f'row points at.',
            hint=(
                f"Make '{parent._meta.label}' soft-deletable too (SetarModel or the "
                f'SoftDeletableModel mixin), so _deleted_at lives on the ancestor and the '
                f'child gets the MTI redirect rule instead of a rule of its own.'
            ),
            obj=child,
            id=ORPHAN_ANCESTOR_ID,
        )
        for child, parent in orphaned_soft_delete_ancestors(_candidate_models(app_configs))
    ]


def register_checks() -> None:
    """Register the checks -- idempotent, Django's registry is a set keyed by function."""
    register(check_soft_deletable_mti_children_have_a_soft_deletable_ancestor)
