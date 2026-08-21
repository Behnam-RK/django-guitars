# Owned relations

[Cascades](soft-deletion.md#cascades) run *inbound*: the foreign key is on the child, pointing at
the row being deleted. When a row **owns** what it points at, the key is on the owner and the
predicate is reversed. `on_delete` cannot say this — it describes what happens to *this* row when
the target goes, the opposite direction — so ownership has its own declaration:

```python
from django.db.models import DO_NOTHING
from guitars.models import OwningForeignKey, SetarModel

class Album(SetarModel):
    press_kit = OwningForeignKey(PressKit, DO_NOTHING, null=True, related_name='albums')
```

Soft-deleting the album soft-deletes its press kit. An `on_delete` that *clears* the key —
`SET_NULL`, `SET_DEFAULT`, `SET(…)` — warns (`guitars.W001`): deleting the *press kit* runs Django's
`Collector`, which clears `press_kit_id` on every album **before** the rule turns the `DELETE` into
an `UPDATE`, leaving the archived kit uncollectable by `hard_delete()`. Legal, occasionally wanted,
silent otherwise. `DO_NOTHING`/`PROTECT`/`RESTRICT` keep the key — and make `kit.hard_delete()` fail
rather than orphan it: go through the owner.

`on_delete=CASCADE` is **refused** (`guitars.E001`): it means deleting the press kit deletes the
album, the opposite of ownership, and would emit the cascade rule backwards. A NULL key matches
nothing, so a nullable owned relation needs no guard. `to_field` is refused too (`guitars.E002`): the
rule correlates the key against the target's *primary* key, which also makes [MTI](#mti) work —
re-asked by the generator and `hard_delete()`, not trusted from the check, since `--skip-checks`
reaches one, the other runs no checks at all, and a redirected key stamps then *removes* a wrong row.

Three more shapes are refused by the generator rather than by a check, warned about like the
[MTI](#mti) limitation below since each depends on the *other* model:

- **A target with no `_deleted_at`.** Nothing for the rule to stamp, and unlike a plain
  `ForeignKey` an `OwningForeignKey` has no other purpose, so this is reported not passed over.
- **An owner with no `_deleted_at`.** The rule fires on the owner's `_deleted_at` transition,
  so a model never soft-deleted would never fire it. Reported for the same reason.
- **A relation closing a cycle of `ON UPDATE` rules** — owning yourself
  (`OwningForeignKey('self', …)`), owning an MTI descendant, or a longer loop back through another
  model's owned or `CASCADE` rules. A rule's action expands *before* the original statement, so a
  cycle is rewritten into itself and PostgreSQL rejects *every* `UPDATE` to *every* table in it — a
  plain `save()` included — as `infinite recursion detected in rules for relation`. Every edge on
  the cycle is refused, not one chosen by iteration order. `hard_delete()` refuses these too.

## The last-owner guard

A target another live row still points at **survives**; it is stamped when the last owner goes.
Unconditional, not derived from whether a `UniqueConstraint` proves single ownership: dropping one
changes no field, so the rule's `[SQL:…]` identity would not move and `--check` would stay green
over an unguarded rule. See [ADR 0011](adr/0011-owner-side-soft-delete-ownership.md).

`hard_delete()` applies the same test in Python, removing an owned row *after* the batch that owned
it — the reverse of child-first `CASCADE` order, since the owner still references it. Three
deliberate narrowings, because it *removes* the row where the rule only stamps a column: the whole
batch is spared rather than one row; an **archived** referrer still counts, its key being on disk;
and **any** surviving foreign key holds the row back — not only the owning column, and at *any*
level of an MTI chain, no row of which is collected alone. All three because a still-referenced row
fails the deferred constraint at `COMMIT`. Collection runs to a fixpoint, so a row spared by a
reference *itself* collected later is picked up later; one that stays spared stays archived. A
`CASCADE` referrer never counts, going *with* the row it points at — discounted by **row**, not
relation, since one model can hold a `CASCADE` key *and* a plain one to the same target, and at any
*depth*, a grandchild going along too. Sound only because collection reads keys as the guard does,
`to_field` column and *base* manager alike. Queryset `hard_delete()` walks neither reverse-FK
children nor owned relations. Narrower **per row**, not absolutely: the whole batch being gone by
construction, a target the per-statement limit below left *live* is still removed — never archived,
straight to gone. The intended end state, but the one direction the Python side goes further.

One thing collection does **not** reach: a `GenericRelation` on an owned target. It carries no
foreign-key constraint, so it cannot fail at `COMMIT` and rightly does not hold the row back — but
nothing removes it either. Only Phase 1's `Collector` walks `_meta.private_fields`, and an owned row
is soft-deleted by a *rule*, never collected, so `hard_delete()` on its **owner** leaves those rows
pointing at a primary key that no longer exists. Deleting through the target does clean them up,
that path running the `Collector`. Delete through the target, or clear them by hand.

Three limits the guard does not cover, all by construction:

- **Per column, not per target.** The `NOT EXISTS` reads only the rule's own foreign-key column, so
  a second `OwningForeignKey` pointing at the same row does not spare it — `hard_delete()` excepted.
- **Per statement.** PostgreSQL runs an `ON UPDATE` rule's action *before* the original update, so
  every owner soft-deleted by one statement still reads as live to the others' guards:
  `Album.objects.filter(press_kit=kit).delete()` leaves `kit` alive. One at a time stamps it.
- **Per visible row.** The `NOT EXISTS` is an ordinary `SELECT`, so a [tenant policy](tenancy.md) on
  the *owner's* table filters it: a live sibling owner in another tenant is invisible, the guard reads
  "last owner", and a still-owned row is stamped — the one place the kit's guards do not fail safe.
  `hard_delete()` reads through the same policy and then *removes* the row, which the foreign-key
  check does not (integrity is exempt from RLS), so the transaction aborts at `COMMIT`. It takes a
  tenanted owner pointing at an **untenanted** target: keep an owned target inside its owner's
  tenant dimension, or leave both untenanted.

## MTI

Ownership *into* an MTI child works: the key holds the primary-key value every table in the chain
shares, so the rule correlates against the ancestor owning `_deleted_at`. Declared *on* an MTI
child whose `_deleted_at` lives farther up it is refused with a warning — the rule fires on the
ancestor's table, where `old."<column>"` cannot name a column the child holds; see [MTI](mti.md).

## Related

- [Migrations](migrations.md#rule-names) — rule names, which family's spelling is frozen, and why
  relaxing an `OwningForeignKey` leaves its rule live and wants a hand-written `DROP RULE`.
- [Soft deletion](soft-deletion.md) · [MTI](mti.md) · [ADR 0011](adr/0011-owner-side-soft-delete-ownership.md) — the guard's shape.
