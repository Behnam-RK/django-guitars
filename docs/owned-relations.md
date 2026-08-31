# Owned relations

[Cascades](soft-deletion.md#cascades) run *inbound*: the foreign key is on the child, pointing at the row
being deleted. When a row **owns** what it points at, the key is on the owner and the predicate is reversed.
`on_delete` cannot say this — it describes what happens to *this* row when the target goes — so ownership
has its own declaration:

```python
from django.db.models import DO_NOTHING
from guitars.models import OwningForeignKey, SetarModel

class Album(SetarModel):
    press_kit = OwningForeignKey(PressKit, DO_NOTHING, null=True, related_name='albums')
```

Soft-deleting the album soft-deletes its press kit. An `on_delete` that *clears* the key —
`SET_NULL`, `SET_DEFAULT`, `SET(…)` — warns (`guitars.W001`): deleting the *press kit* runs
Django's `Collector`, which clears `press_kit_id` on every album **before** the rule turns the
`DELETE` into an `UPDATE`, leaving the archived kit uncollectable by `hard_delete()`. Legal,
occasionally wanted, silent otherwise. `DO_NOTHING`/`PROTECT`/`RESTRICT` keep the key — and make
`kit.hard_delete()` fail rather than orphan it: go through the owner. `on_delete=CASCADE` is
**refused** (`guitars.E001`): it means deleting the press kit deletes the album, the opposite of
ownership, and would emit the cascade rule backwards. `to_field` is refused too (`guitars.E002`):
the rule correlates the key against the target's *primary* key, which also makes [MTI](#mti) work —
re-asked by the generator and `hard_delete()`, not trusted from the check, since `--skip-checks`
reaches one, the other runs no checks at all, and a redirected key stamps then *removes* a wrong row.

More shapes the generator refuses rather than a check, warned about since each depends on the *other*
model — plus the [MTI](#mti) one below and the tenancy one under the guard:

- **A target, or an owner, with no `_deleted_at`.** Nothing to stamp, or nothing whose transition fires
  it — an `OwningForeignKey` has no other purpose, so generating nothing is a bug.
- **A relation closing a cycle of `ON UPDATE` rules** — owning yourself (`OwningForeignKey('self', …)`), owning
  an MTI descendant, or a longer loop back through another model's owned or `CASCADE` rules. A rule's action
  expands *before* the original statement, so the cycle is rewritten into itself and PostgreSQL rejects *every*
  `UPDATE` to *every* table in it, a plain `save()` included. Every edge is refused; `hard_delete()` too.

## The last-owner guard

A target another live row still points at **survives**; it is stamped when the last owner goes.
Unconditional, not derived from whether a `UniqueConstraint` proves single ownership: dropping one
changes no field, so the rule's `[SQL:…]` identity would not move and `--check` would stay green
over an unguarded rule. A NULL key matches nothing, so a nullable owned relation needs no guard.
"Last owner" is the last across **every** `OwningForeignKey` into the target, not the last through
the rule's own column: one `NOT EXISTS` arm per owning column, read over the whole registry rather
than `LOCAL_APPS`. An arm excludes, on the table it reads liveness from, the row going away or the
target itself — so a row owning through two of its own columns, and a target owning itself, are not
their own last owners. **Index every owning column.** See
[ADR 0011](adr/0011-owner-side-soft-delete-ownership.md) and
[ADR 0012](adr/0012-cross-owner-last-owner-guard.md).

`hard_delete()` applies the same test in Python, removing an owned row *after* the batch that owned
it — the reverse of child-first `CASCADE` order, since the owner still references it. Three
narrowings, because it *removes* the row where the rule only stamps a column: the whole batch is
spared rather than one row; an **archived** referrer still counts, its key being on disk; and **any**
surviving foreign key holds it back, at *any* level of an MTI chain — all three because a
still-referenced row fails the deferred constraint at `COMMIT`. Collection runs to a fixpoint, so a
row spared by a reference itself collected later is picked up later; a `CASCADE` referrer never
counts, going *with* the row, discounted by **row** rather than relation and at any depth. Queryset
`hard_delete()` walks neither reverse-FK children nor owned relations. Narrower **per row**, not
absolutely: the batch being gone by construction, it removes a target the rule merely *archived*.

A `GenericRelation` is the one referring shape with no key column. It cannot fail at `COMMIT`, so it rightly never
holds a row back — and before 2.7.0 nothing removed it either: only Phase 1's `Collector` walked
`_meta.private_fields`, leaving the child archived and pointing at a gone primary key. Phase 2 walks them too now.

Two limits the guard does not cover on its own:

- **Per statement — closed in 2.6.0.** A rule's action expands *before* the original update, so owners archived by
  one statement read as live to each other's guards and nothing ever stamped the target. Each rule now carries an
  additive sweep, on the same key and refusals; `sweepowned` repairs older databases, and `--repair` runs to a
  fixpoint since 2.7.0 — over the apps it was given, never the database, a scoped walk skipping the next hop of a
  chain that leaves them. Rewriting a live owner's **own pk** to strand its target raises `feature_not_supported`;
  permuting pks among owners is out of scope. See [ADR 0014](adr/0014-statement-level-owned-sweep.md).
- **Per visible row.** Every arm's `NOT EXISTS` is an ordinary `SELECT`, so a
  [tenant policy](tenancy.md) on the table it reads filters it: a live sibling owner in another
  tenant is invisible, the guard reads "last owner", and a still-owned row is stamped — the one place
  the kit's guards do not fail safe. `hard_delete()` reads through that policy and then *removes* the
  row, which the foreign-key check does not (integrity is exempt from RLS), so that aborts at
  `COMMIT` — the *declaring* owner's own tenancy, not re-examined: keep an owned target inside its
  owners' tenant dimension. The *co-owner* case is **refused** instead since 2.4.0, where a policy
  on either table its arm reads — the co-owner's, or the MTI ancestor it takes liveness from —
  filters on a dimension the dependent's does not, and since 2.4.1 `hard_delete()` reads that same
  verdict. Refusing emits nothing, so one over a recorded rule fails `--check`: drop it by hand.

## MTI

Ownership *into* an MTI child works: the key holds the primary-key value every table in the chain
shares, so the rule correlates against the ancestor owning `_deleted_at`. Declared *on* a child whose
`_deleted_at` lives farther up, it gets no rule — `old."<column>"` cannot name a column off that
table — but every *other* rule's guard carries a **joined** arm for it, matching its key on one table
against liveness on the ancestor's. It spares what it owns, never stamping. See [MTI](mti.md).

## Related

- [Migrations](migrations.md#rule-names) — rule names, which family's spelling is frozen, and why
  relaxing an `OwningForeignKey` leaves its rule live and wants a hand-written `DROP RULE`.
- [Soft deletion](soft-deletion.md) · [MTI](mti.md) — where hard deletion and MTI are described in
  full · [ADR 0011](adr/0011-owner-side-soft-delete-ownership.md) +
  [ADR 0012](adr/0012-cross-owner-last-owner-guard.md) — the guard's shape.
