"""Which physical table holds a column, given Django's two inheritance styles (``docs/mti.md``):
``hasattr`` says yes for an MTI child whose column is on an ancestor's, where a rule is invalid
SQL. Home too of the rule-carrying sweeps: ``hard_delete()`` must not destroy what one spared."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast


if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db import models

    from guitars.tenancy.discovery import _PolicyDimensionMemo


__all__ = [
    'OwnerArm',
    'column_owner',
    'has_column',
    'is_mti_child',
    'mti_root',
    'owned_tenancy_refusals',
    'owner_arms',
    'owns_column',
    'rule_update_cycle_edges',
]


def has_column(model: type[models.Model], colname: str) -> bool:
    """Whether *colname* is reachable on *model* at all -- own table or inherited."""
    return hasattr(model, colname)


def owns_column(model: type[models.Model], colname: str) -> bool:
    """Whether *colname* is a column on *model*'s OWN table -- true for a declared or
    abstract-copied field, false for one inherited through MTI."""
    return any(field.name == colname for field in model._meta.local_fields)


def column_owner(model: type[models.Model], colname: str) -> type[models.Model]:
    """The concrete model whose physical table declares *colname* -- resolves the
    **owner**, not the immediate parent, since a chain three deep may need two hops up."""
    return model._meta.get_field(colname).model


def is_mti_child(model: type[models.Model], colname: str) -> bool:
    """Whether *model* reaches *colname* through an MTI ancestor's table."""
    return (
        bool(model._meta.parents)
        and has_column(model, colname)
        and not owns_column(model, colname)
    )


def mti_root(model: type[models.Model]) -> type[models.Model]:
    """The top of *model*'s MTI chain -- *model* itself if it has none. Every table shares
    one primary-key value, so a walk over the *whole* chain has to start at the root."""
    root = model
    while root._meta.parents:
        root = next(iter(root._meta.parents))
    return root


def _rule_update_edges(candidates: Iterable[type[models.Model]]) -> set[tuple[str, str]]:
    """``(fires_on_table, updates_table)`` for every ON UPDATE soft-delete rule *candidates* call
    for -- cascade and owned alike, each read off the model declaring the foreign key, so a partial
    *candidates* can only miss edges, never invent one. Imports deferred: see below."""
    # Deferred: every other name in this module comes from ``_meta`` alone, while
    # ``guitars.models.fields`` reaches ``guitars.models.__init__`` and the tenancy runtime
    # behind it -- a cost only a caller asking about rules should pay.
    from django.db.models import CASCADE, ForeignKey  # noqa: PLC0415 - see the comment above

    from guitars.models.fields import (  # noqa: PLC0415 - see the comment above
        OwningForeignKey,
        _targets_primary_key,
    )

    edges: set[tuple[str, str]] = set()
    for model in candidates:
        # Both rule kinds live on the table whose ``_deleted_at`` actually flips, so a model
        # that inherits the column declares no rule of its own -- its ancestor does.
        if not owns_column(model, '_deleted_at'):
            continue
        table = model._meta.db_table
        for field in model._meta.local_fields:
            if not isinstance(field, ForeignKey) or not has_column(
                field.related_model, '_deleted_at'
            ):
                continue
            target_table = column_owner(field.related_model, '_deleted_at')._meta.db_table
            # ``_targets_primary_key`` too: a redirected ``to_field`` gets no rule from either
            # side, and an invented edge is worse than a missing one -- it closes a cycle that
            # cannot form and takes the legitimate rule pointing back down with it.
            if isinstance(field, OwningForeignKey) and _targets_primary_key(field):
                edges.add((table, target_table))  # owned: fires here, updates the target
            # Not ``elif``: one field reaches both generators. ``CASCADE`` on an
            # OwningForeignKey is ``guitars.E001``, but ``--skip-checks`` still reaches the
            # generator, and the two rules are each other's cycle -- both edges detect it.
            if field.remote_field.on_delete is CASCADE and not getattr(
                field.remote_field, 'parent_link', False
            ):
                edges.add((target_table, table))  # cascade: fires on the target, updates here
    return edges


def rule_update_cycle_edges(candidates: Iterable[type[models.Model]]) -> set[tuple[str, str]]:
    """The edges of ``_rule_update_edges`` lying on a cycle, which may never be written: a
    rule's action expands *before* the original statement, so a cycle is rewritten into itself
    and PostgreSQL refuses **every** ``UPDATE`` to every table in it, guard unread."""
    edges = _rule_update_edges(candidates)
    adjacency: dict[str, set[str]] = {}
    for source, target in edges:
        adjacency.setdefault(source, set()).add(target)

    def _reaches(start: str, goal: str) -> bool:
        stack = list(adjacency.get(start, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == goal:  # a self-loop falls out of this: goal is its own target
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(adjacency.get(node, ()))
        return False

    # *Every* edge on the cycle, not one chosen edge: which one got refused would otherwise
    # depend on iteration order, and an order-dependent refusal cannot stay stable run to
    # run -- the generator's `--check` would flap and `hard_delete()` would disagree with it.
    return {(source, target) for source, target in edges if _reaches(target, source)}


def _distinct(candidates: Iterable[type[models.Model]]) -> list[type[models.Model]]:
    """*candidates* with repeats dropped. A sweep is built as ``[*named, *get_models()]``, whose
    named models are usually registered too, so one arrives twice. :func:`rule_update_cycle_edges`
    absorbs that in a ``set``; the two below accumulate into lists and would count it twice."""
    return list(dict.fromkeys(candidates))


class OwnerArm(NamedTuple):
    """One owning column pointing at a dependent, as a last-owner guard needs to read it.
    Carries the models as well as the tables: the tenancy question is answerable only from a
    model, and no table-keyed equivalent exists. See ADR 0012."""

    owner_table: str
    fk_column: str
    owner_model: type[models.Model]
    #: Set only where the owner keeps ``_deleted_at`` on an MTI ancestor: that ancestor's table,
    #: model and primary key, and this table's parent-link primary key. The arm joins the two,
    #: the foreign key being on one table and liveness on the other. ``None`` for the plain form.
    root_table: str | None = None
    root_pk: str | None = None
    child_pk: str | None = None
    root_model: type[models.Model] | None = None

    def reads(self) -> tuple[tuple[str, type[models.Model]], ...]:
        """``(table, model)`` for every table this arm's ``SELECT`` touches -- two for a joined
        arm. A tenant policy on *either* filters the read, so both have to be asked about."""
        own = (self.owner_table, self.owner_model)
        if self.root_model is None:
            return (own,)
        return (own, (cast('str', self.root_table), self.root_model))

    def liveness_table(self) -> str:
        """The table whose ``_deleted_at`` this arm reads -- the ancestor's where it joins.
        Which row the arm must not count as an owner is decided against this one, the arm
        matching a row per *liveness* row, not per row holding the key."""
        return self.owner_table if self.root_table is None else self.root_table


def owner_arms(candidates: Iterable[type[models.Model]]) -> dict[str, list[OwnerArm]]:
    """``dependent_table -> every owning column pointing at it``. Swept over the whole registry
    for the reason :func:`rule_update_cycle_edges` is: which relations carry a rule must be one
    answer, or the generator and ``hard_delete()`` disagree about what a guard spared."""
    # Deferred for the reason ``_rule_update_edges`` gives: ``guitars.models.fields`` reaches
    # the tenancy runtime behind ``guitars.models.__init__``, a cost only a caller asking
    # about rules should pay.
    from guitars.models.fields import (  # noqa: PLC0415 - see the comment above
        OwningForeignKey,
        _targets_primary_key,
    )

    arms: dict[str, list[OwnerArm]] = {}
    for model in _distinct(candidates):
        for field in model._meta.local_fields:
            # Only the tests deciding whether an arm can be *expressed*. The ones about whether
            # this owner's own rule can be *written* -- self-update, cycle, tenancy -- say
            # nothing about whether its rows still own the target.
            if (
                not isinstance(field, OwningForeignKey)
                # Reached through MTI is the same physical column on the ancestor's table,
                # covered by that ancestor's own pass.
                or field.model is not model
                or not has_column(model, '_deleted_at')
                or not has_column(field.related_model, '_deleted_at')
                or not _targets_primary_key(field)
            ):
                continue
            dependent_table = column_owner(field.related_model, '_deleted_at')._meta.db_table
            arms.setdefault(dependent_table, []).append(_owner_arm(model, field))
    # Sorted, so a rendered guard -- and therefore the ``[SQL:...]`` identity that decides
    # whether a migration is emitted -- does not move with registry order.
    return {
        table: sorted(found, key=lambda arm: (arm.owner_table, arm.fk_column))
        for table, found in arms.items()
    }


def _owner_arm(model: type[models.Model], field: models.ForeignKey) -> OwnerArm:
    """One arm, in whichever of the two forms the owner's own shape calls for. An owner that
    inherits ``_deleted_at`` is refused a rule of its *own* -- that would fire on a table its
    key is not on -- but its rows own the target all the same."""
    table = model._meta.db_table
    if owns_column(model, '_deleted_at'):
        return OwnerArm(table, field.column, model)
    root = column_owner(model, '_deleted_at')
    # Joined on the primary key, which every table in an MTI chain shares one *value* of --
    # the same soundness the owned rule's own correlation rests on.
    return OwnerArm(
        table,
        field.column,
        model,
        root_table=root._meta.db_table,
        root_pk=cast('str', root._meta.pk.column),
        child_pk=cast('str', model._meta.pk.column),
        root_model=root,
    )


def owned_tenancy_refusals(
    candidates: Iterable[type[models.Model]],
) -> dict[tuple[str, str, str], list[str]]:
    """``(dependent_table, owner_table, fk_column) -> the tables a tenant policy filters on a
    dimension the dependent's does not``. An arm's ``NOT EXISTS`` is an ordinary ``SELECT``, so
    such a policy hides a live owner: the rule is refused, and not followed in Python either."""
    # Deferred for the reason ``owner_arms`` gives, and because ``guitars.tenancy`` imports this
    # module -- at module level the two would close a cycle.
    from guitars.models.fields import (  # noqa: PLC0415 - see the comment above
        OwningForeignKey,
        _targets_primary_key,
    )
    from guitars.tenancy.discovery import (  # noqa: PLC0415 - see the comment above
        assumed_policy_dimensions,
        policy_dimensions,
        tenant_policies_enabled,
    )

    if not tenant_policies_enabled():
        return {}
    swept = _distinct(candidates)
    arms = owner_arms(swept)
    memo: _PolicyDimensionMemo = {}
    refused: dict[tuple[str, str, str], list[str]] = {}
    for model in swept:
        # An owner that does not own the column is refused a rule anyway, on the MTI ground;
        # asking here would name a table the rule could never fire on.
        if not owns_column(model, '_deleted_at'):
            continue
        owner_table = model._meta.db_table
        for field in model._meta.local_fields:
            # The earlier refusals that cost nothing to re-ask, so a key here is one tenancy
            # alone would refuse -- a caller reading the dict as *the* reason a relation carries
            # no rule would otherwise get the wrong remediation. One exception, below.
            if (
                not isinstance(field, OwningForeignKey)
                or field.model is not model
                or not has_column(field.related_model, '_deleted_at')
                or not _targets_primary_key(field)
            ):
                continue
            dependent = column_owner(field.related_model, '_deleted_at')
            dependent_table = dependent._meta.db_table
            # A rule updating the table it fires on is refused for infinite rule recursion. The
            # exception: its multi-table form stays the caller's, which builds that graph on its
            # own account -- so a key here may be cycle-refused too, and callers read both.
            if dependent_table == owner_table:
                continue
            # Per *dimension*, and per what a policy predicates rather than what a manager
            # declares: reaching the dependent's row put the session inside the dimensions its
            # own policy filters on, so only one a co-owner's has and it does not can hide a row.
            dimensions = policy_dimensions(dependent, memo)
            # Opposite defaults, deliberately: for a model the kit writes no policy for, the
            # dependent's read is assumed unfiltered and the arm's filtered -- both refuse, and
            # one function for the two emits exactly the guard that cannot see a live owner.
            offending = sorted(
                {
                    table
                    for arm in arms.get(dependent_table, ())
                    if (arm.owner_table, arm.fk_column) != (owner_table, field.column)
                    # Every table the arm reads, not just the one holding the key: a joined arm
                    # takes liveness from an MTI ancestor, and a policy there hides it as well.
                    for table, arm_model in arm.reads()
                    if assumed_policy_dimensions(arm_model, memo) - dimensions
                }
            )
            if offending:
                refused[dependent_table, owner_table, field.column] = offending
    return refused
