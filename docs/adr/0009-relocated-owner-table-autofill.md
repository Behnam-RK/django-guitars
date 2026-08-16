# 0009 — a tenant autofill trigger relocates onto the ancestor owning its column

- **Status:** accepted — implemented in 2.1.1
- **Date:** 2026-08-16
- **Affects:** `guitars.tenancy.discovery`, `makeguitarmigrations`, `audittenancy`, ADR-0005

## Context

[ADR-0005](0005-trigger-based-tenant-autofill.md) put tenant autofill in a `BEFORE INSERT` trigger. `_classify` sourced the trigger's columns from `own` — columns on the model's *own* table — because a trigger there cannot write a column living on an MTI ancestor's table.

That is correct as far as it goes, but it only *relocates* the trigger when the ancestor happens to be tenanted and autofilling itself, which is the `Tour`/`WorldTour` shape the tests happened to cover. For an untenanted ancestor merely holding the column:

```python
class Venue(SetarModel):
    label = models.ForeignKey(Label, ...)

class Arena(Venue):
    objects = tenanted_manager(label='label', autofill=True)
```

`own` is empty, so `autofill_columns` was `None` and **no trigger was emitted anywhere** — silently, unlike the multi-hop and diamond cases which both warn. The RLS policy was still emitted with the owner join, so the table looked covered. ADR-0005's claim that `DisableSignals` "costs the friendly message, not the guarantee" was false for exactly this shape: with the `pre_save` receiver disconnected, nothing filled the column and the insert failed on a bare `NOT NULL`. Raw SQL, multi-row `INSERT` and `INSERT … SELECT` against the ancestor's table were never covered at all.

Three options were on the table: leave it and document the gap; emit a warning note and leave the behaviour; or emit the trigger on the ancestor's table.

## Decision

Emit the trigger on the **owner's** table, attributed to the **owner's app** — the same inversion `_cascade_operations` already uses for `_deleted_at`. `TableCoverage` gains `owner_autofill_columns`, a sibling of `autofill_columns`, kept out of `as_kwargs()` and out of `[POLICY:…]`.

The trigger is one database object shared by every MTI descendant, so the decision to create it is theirs jointly. `_relocatable(owner, column)` refuses, with a note, when:

1. **no claimant autofills** — nothing to create, and silence is correct;
2. **claimants disagree on the dimension** — two GUCs writing one column means two triggers on one table fired in *name* order, an ordering nobody declared;
3. **any claimant sets `autofill=False`** — an MTI insert of that child writes a row into the owner's table, so the trigger would overwrite an opt-out ADR-0005 makes auditable as an *absent* trigger;
4. **the owner already autofills that column itself** — the `Tour` case, already covered by its own `autofill_columns`; relocating too would emit a second `CREATE TRIGGER` on one table and fail `migrate`.

Rule 4 keys on the **column**, not the dimension, so "the owner autofills this column under a different dimension" falls into rule 2 rather than passing as coverage.

## Why

The note-only alternative leaves a correctness hole open in the one layer this library exists to be: the database. ADR-0005's whole argument is that Python-side enforcement is not a guarantee, and accepting a Python-only path for this shape concedes that argument for an arbitrary subset of models.

The strongest objection to relocating is that the ancestor never opted into tenancy. That is real, and it is accepted below rather than argued away. The guard removes the cases where relocation would actively contradict a model's stated intent; what remains is a table gaining a trigger its own author did not ask for, which is the ordinary consequence of MTI — the ancestor's table is *where the column is*, and every insert through a tenanted child already writes to it.

## Consequences

**Accepted costs.** The trigger fires for **direct inserts of the owner**, which is untenanted: `Venue.objects.create(name='x')` inside an active `tenant(label=acme)` scope has `label_id` stamped. This cannot be mitigated — Django writes the ancestor row with its own `INSERT` carrying no marker, so no `WHEN` clause can tell `Venue.objects.create()` from `Arena.objects.create()`, and refusing whenever the owner is directly insertable would kill the feature outright since an MTI root always is. Pass `autofill=False` on a claimant if that is wrong for your model.

A guard that can flip to "refuse" also means adding a sibling with `autofill=False` retires an existing trigger, which is only safe because [issue #27](https://github.com/Behnam-RK/django-guitars/issues/27)'s retirement path landed first.

**Reversibility.** Straightforward. `owner_autofill_columns` is additive and outside every digest, so dropping it churns no `[POLICY:…]` header; the emitted triggers retire themselves through the ordinary `recorded − required` path on the next run.

## Related

- [ADR-0005](0005-trigger-based-tenant-autofill.md) — the trigger this relocates, and the opt-out this preserves.
- [ADR-0003](0003-mti-owner-join-policy.md) — the owner-join policy resolving the same ownership question for predicates.
- [`docs/mti.md`](../mti.md), [`docs/tenancy.md`](../tenancy.md), [`docs/migrations.md`](../migrations.md).
- Issues [#28](https://github.com/Behnam-RK/django-guitars/issues/28) and [#27](https://github.com/Behnam-RK/django-guitars/issues/27).
