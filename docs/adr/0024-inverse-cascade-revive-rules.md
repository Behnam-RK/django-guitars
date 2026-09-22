# 0024 — an inverse trigger revives what a cascade archived

- **Status:** accepted
- **Date:** 2026-09-22
- **Affects:** `guitars.sql.soft_delete._CREATE_SOFT_DELETE_REVIVE`, `guitars.management.enforcement`

## Context

Issue #51. Every cascade family was gated on the archive transition alone, so clearing a parent's `_deleted_at` left its children archived. The parent read healthy, its children were invisible to every live-manager query, and nothing raised or logged. Re-ingest is the motivating path — a webhook meets a soft-deleted row on the same natural key and must revive it, and inserting beside it is impossible because the archived row still holds the unique key.

The obvious fix, a predicate flip, is unsafe: `UPDATE child SET _deleted_at = NULL WHERE fk = old.pk` also resurrects a child the caller archived *before* the parent went. That fails toward **exposing** data, which is worse than the silent stranding it replaces, and it is the direction this kit's guards are written never to fail in. It needed archive provenance, which [ADR 0023](0023-cascade-stamps-its-parents-timestamp.md) supplied by stamping a cascaded child with its parent's own `_deleted_at`.

Two other options were on the table: a Python `revive()`/`undelete()` on the queryset, walking the generator's own arm predicates; and documenting the asymmetry loudly and leaving revive to callers.

## Decision

A new private family, `soft_delete_revive_*`, emitted from inside `_cascade_operations`' own loop against the same key and after the same refusals — so which relations carry a revive **is** which carry a cascade. It is a **statement-level `AFTER UPDATE` trigger**, not a second rule: it reads the transition tables for parents whose `_deleted_at` went from set to `NULL`, and clears `_deleted_at` on children whose value still equals the parent's.

Plain and VIA only. The owned family and the self-referential cascade trigger stay archive-only. A retirement now drops **both** rules per key, each arm testing its own recorded map, and each is ordered against the migration that created it.

## Why

Timestamp equality is the provenance test, and it needs no second mechanism: it chains level by level because each level's own `UPDATE` fires the level below's trigger, and a multi-row statement stays correct because the transition tables are joined on the primary key rather than one value being broadcast. Both were verified against PostgreSQL before any of the wiring was written.

**Statement-level, and this is the decision that changed.** The family shipped first as a second `ON UPDATE` rule beside the cascade one, which is wrong for a reason a review round measured: PostgreSQL's rewriter expands *both* rules for any `UPDATE`, then rewrites each arm against the child's two rules, so a cascade chain of depth N costs 2^(N+1)−1 query trees. The revive arm inside a cascade expansion is provably dead — `new._deleted_at IS NULL` is constant-false there — but the rewriter builds it and the planner plans it. Measured at depth 6: **127 query trees and 93 ms** to plan a plain `UPDATE … SET label = 'x'`, against 7 and 2 ms with the cascade rules alone; a statement trigger restores 7 and ~6 ms. It doubles per level and is paid on every write, `save()` included. That is the cost [ADR 0018](0018-self-referential-cascade-trigger.md) converted the self-referential cascade to avoid, and the cost CLAUDE.md cites as the whole reason for the rule-cycle refusal, arriving by expense rather than by error.

No **refusal** on a key rewrite, unlike the owned sweep. That sweep raises where a statement archives an owner and moves its primary key, because its join cannot then say whether the row was archived and the dependent would leak. Here the same shape leaves the children archived — the state they were already in — so it fails toward hiding and needs no exception.

A separate operation rather than more SQL inside the cascade's own, though bundling looks cheaper. Bundled, a retirement would have to drop a revive for keys whose project never created one — every project upgrading to this release — and the escapes are `IF EXISTS`, a knowledge claim reserved for `--adopt`, or comparing a recorded digest against the current template's bytes, which breaks the next time either template changes. It is also ADR 0014's own finding: a recorded rule must not read as a recorded second object, or upgrading projects never receive one.

Against a Python `revive()`: the kit's premise is that behaviour is enforced by PostgreSQL because the paths that matter — `queryset.delete()`, `bulk_update`, raw SQL — never reach `.save()`. A Python method is bypassed by a raw `UPDATE ... SET _deleted_at = NULL`, by another service, and by this repository's own test suite, which writes exactly that. The counter-argument is real and worth stating: unlike deletion, which Django issues on the caller's behalf through rows they never named, un-archiving is always a deliberate act at a known call site, so a Python API would be reachable at the point of intent. That makes the Python branch viable, not equivalent — it narrows the hole rather than closing it.

Against documenting alone: the failure is silent, which is what makes it a bug rather than a limitation.

Owned stays archive-only because its archive is conditional on a last-owner `NOT EXISTS`, so the inverse is not "did this owner archive it" but a question about rows the reviving statement is simultaneously changing — ADR 0014's problem again, needing its own sweep. The self-referential trigger stays archive-only because it recurses a subtree and its transition tables carry no record of which rows *that* archive took; it also still writes `NOW()`, so its descendants never carry the root's value and the provenance test could not match them even if a rule existed.

## Consequences

**Accepted costs.** Two exposures, both narrower than the behaviour they replace and neither closed by this design. A child archived independently **in the same transaction** as its parent, at a bit-identical value, is revived with it — and the ordinary ORM path relies on that same coincidence, since `Collector` archives children before parents inside one `atomic()` and one `transaction_timestamp()` stamps both. And re-stamping an **already-archived** parent fires neither rule, since `old` and `new` are both `IS NOT NULL`, so its children keep the old value and a later revive matches none of them; that one fails toward hiding. Both are pinned by tests.

Under tenancy the family is emitted, not refused, as the cascade family is: a child a policy hides is not archived by the cascade and is not revived by this rule, so it stays exactly as hidden as before — no new exposure, and no reason to copy the owned family's refusal. A caller on a `GuitarModel` must revive inside a `tenant()` scope, the unscoped manager denying the write.

One shape is left as accepted rather than guarded: a row that is both an `OwningForeignKey` dependent and an ordinary `CASCADE` child of the *same* parent is revived by this rule regardless of its other owners, the owned family's last-owner reasoning not reaching it. `tests/testapp` has no such shape.

The name-clash report now covers a third family. The revive name sizes every segment it emits *and* carries a literal `via` on the keyed form, because all three of its variable parts are optional and sizing alone let `('myapp.x', None)` meet `('myapp', 'x')`. It spells the **owner** table too, for `_owned_sweep_name`'s reason: a trigger is namespaced per table but a function per schema, so two owners sharing a `(related, key)` pair would otherwise overwrite each other's body. It is claimed on the name alone, as that sweep is, rather than per table.

**Reversibility.** The family is private and additive: dropping its templates, headers, scanner and emitter would leave the cascade family untouched, and the retirement path already drops the trigger and its function. What would not come back is the provenance ADR 0023 paid for. Relaxing a key rather than removing it still wants a hand-written `DROP TRIGGER` and `DROP FUNCTION`, as it does for the owned sweep — and `RetireEnforcement`'s whole-table form takes them, while its column-scoped form cannot, a trigger depending on no column.

## Related

- [#51](https://github.com/Behnam-RK/django-guitars/issues/51) · [ADR 0023](0023-cascade-stamps-its-parents-timestamp.md) — the provenance this rests on.
- [ADR 0014](0014-statement-level-owned-sweep.md) · [ADR 0018](0018-self-referential-cascade-trigger.md) — the two families left archive-only.
- [`docs/soft-deletion.md`](../soft-deletion.md)'s "Cascades" · [`docs/migrations.md`](../migrations.md)'s "Retirement"
