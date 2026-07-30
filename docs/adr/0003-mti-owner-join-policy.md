# 0003 — An owner-join policy for MTI children

- **Status:** accepted
- **Date:** 2026-07-30
- **Affects:** `guitars.sql.policy`, `guitars.tenancy.discovery`

## Context

With the tenant FK inherited from `GuitarModel`, the tenant column lives on the
MTI **root**. Every child table in the chain has no such column.

The tempting shortcut — and the one the implementation this was extracted from
took — is to skip MTI children entirely, reasoning that "every query joins the
parent, so the parent's policy covers them."

That reasoning is false, and this kit already knew it. `set_parent_updated_at()`
exists precisely because a child-only statement does not touch the ancestor:

- `queryset.update()` on child-local fields
- a `DELETE` against the child table
- `.values()` of child-only columns

None of these join the root, so a root-only policy never applies. Unprotected, and
by *default* — with the FK inherited, every MTI child lands in this case.

## Decision

Each MTI child table gets its own `tenant_scope` policy, predicated through a
correlated subquery against the column's **owner**:

```sql
EXISTS (SELECT 1 FROM <owner> AS _guitars_owner
        WHERE _guitars_owner.<owner_pk> = <child>.<child_pk>
          AND _guitars_owner.<col>::text = ANY(string_to_array(
                (SELECT current_setting('tenant.<dim>', true)), ',')))
```

Correlated by the shared-PK invariant: every table in an MTI chain shares one
primary-key value.

Dimensions spread across **two** different ancestors are reported rather than
covered.

## Why

**The alternative is a documented hole in the default configuration.** "MTI
children are unprotected unless you hand-write a policy" is not a limitation a
security feature can carry when MTI children are what the ladder produces.

**The owner, not the parent.** In a chain three deep the column may live two
tables up, so predicating against the immediate parent would reference a table
that has no such column either. Ownership is resolved via
`model._meta.get_field(name).model`, shared with the migration generator through
`guitars.introspection`.

**Two ancestors is refused rather than approximated.** One correlated subquery
reaches one ancestor. Picking one would make the policy's strength depend on field
declaration order — a policy whose strength varies with that is worse than a named
gap. A model with own-table dimensions as well still gets a policy for those, and
the note says which dimensions it enforces and which it dropped, because "skipped"
alone reads as "no protection here" on a table that has some.

## Risks, and what was done about them

This was flagged up front as novel SQL: a correlated `EXISTS` per candidate row,
against a table that itself has RLS. Two things had to be proven rather than
assumed, and both were, against real PostgreSQL:

1. **The ancestor's own policy applies inside the subquery.** It does — and that is
   correct rather than merely tolerable, because it compares the same session
   setting, so it is satisfied for the same tenant and denies for any other. The
   two layers agree instead of one quietly widening the other.
   (`test_the_ancestors_policy_inside_the_subquery_does_not_over_deny`)
2. **The leak it closes is real.** A negative control drops the child policy and
   demonstrates the unfiltered child-only read.
   (`test_an_unpolicied_child_leaks_every_tenant`)

A third thing was learned while building that control: **RLS enabled with no
policies is default-DENY**, not default-allow. The first version of the control
returned 0 rows rather than the expected leak, because dropping the policy while
leaving RLS enabled denies everything. Pinned as
`test_rls_enabled_with_no_policy_denies_everything`.

Two naming hazards are handled in the SQL: the ancestor is aliased
(`_guitars_owner`) so an unqualified column inside the subquery cannot silently
resolve to it, and the child's key is written table-qualified so it cannot be
shadowed by a same-named column on the ancestor.

## Consequences

- **A correlated subquery per candidate row on child tables.** Accepted. The join
  is on the primary key of both tables, and `current_setting` sits in a scalar
  subquery so the planner hoists it to an InitPlan evaluated once per statement.
- **The fallback, had the nesting not worked**, was to ship without the MTI policy
  and document the limitation. It worked, so that was not needed — but the tests
  above are what would have caught it, and they remain the reason to trust it.

## Related

- [docs/mti.md](../mti.md) · [docs/tenancy.md](../tenancy.md)
