"""Startup validation for model *shapes* the enforcement layer cannot express, as
``guitars.tenancy.checks`` does for the tenancy settings. One shape so far, and it is the
one that destroys a row rather than sparing it."""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.checks import Error, register
from django.db import models

from guitars.introspection import column_owner, has_column, mti_root, owns_column


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
    """``(child, ancestor)`` for every concrete MTI child carrying ``_deleted_at`` under a direct
    parent that has none -- the model where the column first *meets* such a parent, one finding
    per root cause; :func:`refuses_soft_delete_rule` is what covers its descendants."""
    # ``has_column``, not ``owns_column``: a child of two concrete parents inherits the column
    # from one and still sits over the other, which ``Collector`` deletes unguarded all the same.

    # The declaring shape is the special case where every parent lacks it -- Django refuses a
    # clash with a direct base at class creation and a deeper one as ``models.E006``.
    found = []
    for model in candidates:
        if not model._meta.parents or not has_column(model, '_deleted_at'):
            continue
        found.extend(
            (model, parent)
            for parent in model._meta.parents
            if not has_column(parent, '_deleted_at')
        )
    return found


def refuses_soft_delete_rule(
    model: type[models.Model],
) -> list[tuple[type[models.Model], type[models.Model]]]:
    """``(child, ancestor)`` for every reason *model* must get no soft-delete rule of any kind
    -- the generator's own question, re-asked rather than trusted from ``guitars.E003``, which
    ``--skip-checks`` walks straight past and ``hard_delete()`` never runs at all."""
    # Asked of the **whole chain** above *model*, not of *model* alone: a concrete child of a
    # refused model carries the column over a parent that has it, so
    # ``orphaned_soft_delete_ancestors`` passes it over.

    # It would then fall through to the MTI redirect rule -- ``DO INSTEAD``, keeping exactly
    # the row the refusal exists to let go, its parent-link dangling at ``COMMIT`` all the
    # same, one table further down.
    if not has_column(model, '_deleted_at'):
        return []
    return orphaned_soft_delete_ancestors([model, *model._meta.get_parent_list()])


def _hint(child: type[models.Model], parent: type[models.Model]) -> str:
    """Name the move that resolves the chain, not the next hop of it: making the immediate
    parent soft-deletable under a plain grandparent only moves the orphan up one table."""
    root = mti_root(parent)
    if owns_column(child, '_deleted_at'):
        return (
            f"Make '{root._meta.label}' soft-deletable (SetarModel or the SoftDeletableModel "
            f"mixin) and drop '{child._meta.label}'s own declaration, so _deleted_at lives on "
            f'the root and every table below it gets the MTI redirect rule.'
        )
    # Inherited from another parent: a second base cannot carry it too, since Django refuses a
    # field reaching a model from two bases, so this chain has to be restructured.
    owner = column_owner(child, '_deleted_at')
    return (
        f"'{child._meta.label}' inherits _deleted_at from '{owner._meta.label}', and "
        f"'{root._meta.label}' cannot be made soft-deletable as well, a field reaching a model "
        f"from two bases being a clash. Make '{root._meta.label}' abstract, or drop one parent."
    )


def check_soft_deletable_mti_children_have_a_soft_deletable_ancestor(
    app_configs, **kwargs
) -> list[Error]:
    """Django's ``Collector`` issues one ``DELETE`` per table in an MTI chain. A child
    carrying ``_deleted_at`` keeps its row through its rule, while the ancestor, having no
    column to stamp, gets no rule and is really deleted."""
    # An error rather than a warning: the row does not merely go unstamped, the statement
    # aborts at COMMIT on the child's own parent-link constraint. Nothing is recoverable
    # from it at runtime, and no code path in the kit can spare it.
    return [
        Error(
            f"'{child._meta.label}' carries _deleted_at while its multi-table-inheritance "
            f"ancestor '{parent._meta.label}' declares none, so deleting one aborts at COMMIT: "
            f"the child's soft-delete rule keeps its row while the ancestor's DELETE, which no "
            f'rule guards, removes the row that row points at.',
            hint=_hint(child, parent),
            obj=child,
            id=ORPHAN_ANCESTOR_ID,
        )
        for child, parent in orphaned_soft_delete_ancestors(_candidate_models(app_configs))
    ]


def register_checks() -> None:
    """Register the checks -- idempotent, Django's registry is a set keyed by function."""
    register(check_soft_deletable_mti_children_have_a_soft_deletable_ancestor)
