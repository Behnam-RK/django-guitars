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
    # Every plain root above *child*, not only above this *parent* and not ``mti_root``'s first:
    # two plain roots -- a diamond, or two direct parents -- can give only one of them the column,
    # the other then meeting it as the join shape below. Ordered and deduplicated by ``dict``.
    plain = [p for p in child._meta.parents if not has_column(p, '_deleted_at')]
    roots = list(
        dict.fromkeys(
            m for p in plain for m in (p, *p._meta.get_parent_list()) if not m._meta.parents
        )
    )
    named = ', '.join(f"'{root._meta.label}'" for root in roots)
    if owns_column(child, '_deleted_at') and len(roots) == 1:
        return (
            f'Make {named} soft-deletable (SetarModel or the SoftDeletableModel mixin) and drop '
            f"'{child._meta.label}'s own declaration, so _deleted_at lives on the root and every "
            f'table below it gets the MTI redirect rule.'
        )
    # A second base cannot carry the column too, Django refusing a field that reaches a model
    # from two bases, so this chain has to be restructured rather than extended.
    if owns_column(child, '_deleted_at'):
        carried = f"'{child._meta.label}' sits over {len(roots)} plain roots ({named})"
    else:
        owner = column_owner(child, '_deleted_at')
        carried = (
            f"'{child._meta.label}' inherits _deleted_at from '{owner._meta.label}', and {named} "
            f'cannot be made soft-deletable as well'
        )
    return (
        f'{carried}, a field reaching a model from two bases being a clash. Make the plain '
        f'side abstract, or drop one parent.'
    )


def check_soft_deletable_mti_children_have_a_soft_deletable_ancestor(
    app_configs, **kwargs
) -> list[Error]:
    """Django's ``Collector`` issues one ``DELETE`` per table in an MTI chain. A child
    carrying ``_deleted_at`` keeps its row through its rule, while the ancestor, having no
    column to stamp, gets no rule and is really deleted."""
    # An error rather than a warning: with a rule the statement aborts at COMMIT on the child's
    # own parent-link constraint, and without one -- which is what the refusal leaves -- the
    # chain is destroyed. Nothing at runtime spares it either way, so it must not start.
    return [
        Error(
            f"'{child._meta.label}' carries _deleted_at while its multi-table-inheritance "
            f"ancestor '{parent._meta.label}' declares none, so it gets no soft-delete rule: "
            f"with one, a delete aborted at COMMIT (the rule kept the child's row while the "
            f"ancestor's unguarded DELETE removed what it points at); without one, .delete() "
            f'destroys the chain.',
            hint=_hint(child, parent),
            obj=child,
            id=ORPHAN_ANCESTOR_ID,
        )
        for child, parent in orphaned_soft_delete_ancestors(_candidate_models(app_configs))
    ]


def register_checks() -> None:
    """Register the checks -- idempotent, Django's registry is a set keyed by function."""
    register(check_soft_deletable_mti_children_have_a_soft_deletable_ancestor)
