# 0017 — E003 names the declaring model; the generator refuses its descendants

- **Status:** accepted — implemented in 2.7.0
- **Affects:** `guitars.checks`, `makeguitarmigrations`, [ADR 0015](0015-refuse-soft-deletable-mti-orphans.md)
- **Date:** 2026-09-01

## Context

[ADR 0015](0015-refuse-soft-deletable-mti-orphans.md) refuses a model declaring `_deleted_at` on
its own table under a concrete MTI ancestor that has none. As first written, both halves of the
refusal asked the same question — `orphaned_soft_delete_ancestors`, which gates on
`owns_column(model, '_deleted_at')`.

That gate names only the model that *declares* the column. A concrete child of such a model
inherits `_deleted_at`, so `owns_column` is false, the predicate passes it over, and it fell
through to the MTI **redirect** rule — `DO INSTEAD`, keeping exactly the row the refusal exists
to let go. Confirmed against a live registry for `Plain → Lit → Neon`: `Lit` was refused and
`Neon` got `MTI Soft Delete Rule on "testapp_neon"`. Deleting a `Neon` then aborts at `COMMIT` on
its own parent-link, one table further down than the shape 0015 describes.

## Decision

**Split the two questions.** The generator asks `checks.refuses_soft_delete_rule(model)`, which
re-asks `orphaned_soft_delete_ancestors` of `column_owner(model, '_deleted_at')` rather than of
the model in front of it, so a descendant is refused with its declaring ancestor. `guitars.E003`
keeps asking the narrow question and reports the **declaring model alone**.

## Why

The two halves want different answers because they answer to different readers.

The generator emits SQL per model, so it has to decide about `Neon` specifically — and its only
safe answer is "no rule", since every rule shape available to `Neon` keeps a row the ancestor's
unguarded `DELETE` removes.

The check talks to an operator, and there is one thing for them to do: make the ancestor
soft-deletable. That fixes every descendant at once. Reporting `Neon` as well would print n
findings for a single root cause and invite fixing them one at a time — which for a descendant
means declaring `_deleted_at` on *it*, creating a second orphan rather than resolving the first.

The generator's stderr note names the *owner*, not the model whose rule was skipped, so an
operator who reaches the generator without the check still gets pointed at the same fix.

## Consequences

**Accepted costs.** A descendant loses its rule while `manage.py check` never mentions it by
name. Someone debugging why `Neon` stopped soft-deleting has to follow the generator's note or
ADR 0015 to find out that `Lit` is the reason. The alternative — a finding per affected model —
was judged worse, but this is the cost of that judgment.

The two predicates must stay in step. `refuses_soft_delete_rule` is defined *in terms of*
`orphaned_soft_delete_ancestors` rather than duplicating its logic, so they cannot drift apart;
`tests/test_checks.py::test_the_refusal_reaches_a_descendant_of_the_refused_model` pins the split
by asserting both answers for one model.

**Reversibility.** High. Both halves are pure Python read off `_meta`, and no generated SQL
records which question was asked. A later release could report descendants too without touching
a migrated database.

## Related

- [ADR 0015](0015-refuse-soft-deletable-mti-orphans.md) — the refusal itself, and why it is an
  error · [ADR 0011](0011-owner-side-soft-delete-ownership.md) — why the generator re-asks a
  check rather than trusting it · [`docs/mti.md`](../mti.md) — the supported shapes
