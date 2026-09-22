# 0023 — a cascade stamps its parent's timestamp, and only a live child

- **Status:** accepted
- **Date:** 2026-09-22
- **Affects:** `guitars.sql.soft_delete._CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE`

## Context

Issue #53, found while planning #51. The reverse-FK cascade rule's action was
`UPDATE {related_table} SET _deleted_at = NOW() WHERE "{foreign_key}" = old."{primary_key}"`, with no guard — alone among the families. The owned rule, the self-cascade trigger, the MTI redirect rule and the own-table `ON DELETE` rule all carry `AND _deleted_at IS NULL`. So archiving a parent overwrote the `_deleted_at` of a child the caller had archived earlier, silently. Reproduced on 2.10.0: a child archived at `2020-01-01` read `2026-09-22` after its parent went, while the same shape through the owned family kept its value.

That is a bug on its own terms — `_deleted_at` is the only record of *when* a row was archived, and `_archives` is ordered and filtered by it. It is also the reason a revive (inverse) cascade could not be written safely: after the overwrite, a child cascaded away by its parent and a child archived deliberately beforehand are indistinguishable, so reviving the parent would resurrect both. That fails toward *exposing* data, which is worse than #51's silent stranding.

Three options were on the table. Add the guard alone, leaving `NOW()`. Add the guard and copy `new._deleted_at`. Or record provenance explicitly, in a column or a side table.

## Decision

Both changes, to the private `_CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE` — the one the generator renders:

```sql
UPDATE {related_table}
SET _deleted_at = new._deleted_at
WHERE "{foreign_key}" = old."{primary_key}"
  AND _deleted_at IS NULL
```

The frozen public `CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE` and `..._VIA` keep their old bodies. The owned sweep and the self-cascade trigger keep `NOW()`.

## Why

The guard is not optional once the behaviour is named: nothing else in the kit re-stamps a column that is already set, and an archive timestamp that moves is not a timestamp.

The copy is what makes the surviving value mean something. `NOW()` is `transaction_timestamp()`, so a parent and its children agree only when one transaction archived both through the kit's own `ON DELETE` rule — and disagree whenever Python wrote the parent's value (`update(_deleted_at=timezone.now())`), when a raw `UPDATE` did, or across transactions. With the copy, a child carries its parent's exact value on every path, and because each level's rule fires on the level above's `UPDATE`, it chains down a whole subtree with no second mechanism. One archive is then one instant, which is what the owned and self families already give. It also survives the case that distinguishes a per-row copy from a single evaluated `NOW()`: a multi-row statement archiving two parents at two values expands the rule as a join, so each child reads the row that archived it.

Against a provenance column or side table: it would need a schema migration on every soft-deletable table in every consuming project, an order of magnitude more than a rule replace — and it would have to be a *set* rather than a column, since a child reachable from two parents can be cascaded by either. It would also need its own retirement, its own tenancy story, and rows outliving hard deletes.

The strongest objection to what was chosen is that two rows archived at the same instant under the same parent remain indistinguishable, so timestamp equality is not true provenance. That is accepted and named below; the alternative that closes it is the one rejected in the paragraph above, and the gap is strictly narrower than the behaviour it replaces, which recorded nothing at all.

The public constants are left alone for the reason the inlining rule exists: a migration generated before 1.1.0 reads them *by name at migrate time*, so editing them would give a consumer the fixed rule on a fresh database and the old one on an incrementally-migrated database, from an identical history. Both converge once the replace migration enters history, so nothing is lost by waiting.

## Consequences

**Accepted costs.** A consumer gets one additional enforcement migration per app, and `makemigrations --check` in their CI is red until they run `makeguitarmigrations` and `migrate`. The moved `[SQL:…]` identity emits `CREATE OR REPLACE RULE` per cascade relation — `operations.py` sets `replace = forward` for this family — so there is no `DROP` and no window in which a table has no rule. Timestamp semantics are now **not uniform across families**: plain and VIA propagate the parent's value, while the owned sweep and the self-cascade trigger still write `NOW()`. Changing either would move another identity for no corresponding benefit, and neither is gaining an inverse. And equality remains an incidental record rather than a designed one: two rows archived in the same transaction at the same value cannot be told apart.

**Reversibility.** Restoring the old body and regenerating would emit another replace, so it is mechanically easy — but it would re-open the data loss and, if #51 has shipped by then, make its revive rule unsafe. In practice this is one-way.

## Related

- [#53](https://github.com/Behnam-RK/django-guitars/issues/53) — the bug, with the reproduction.
- [#51](https://github.com/Behnam-RK/django-guitars/issues/51) — the inverse cascade this makes possible.
- [`docs/soft-deletion.md`](../soft-deletion.md)'s "Cascades" · [ADR 0014](0014-statement-level-owned-sweep.md) · [ADR 0018](0018-self-referential-cascade-trigger.md)
