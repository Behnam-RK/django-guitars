# Owned relations

[Cascades](soft-deletion.md#cascades) run *inbound*: the foreign key is on the child,
pointing at the row being deleted. When a row **owns** what it points at, the key is on
the owner and the predicate is reversed.

`on_delete` cannot say this — it describes what happens to *this* row when the target
goes, the opposite direction — so ownership has its own declaration:

```python
from django.db.models import SET_NULL
from guitars.models import OwningForeignKey, SetarModel

class Album(SetarModel):
    press_kit = OwningForeignKey(PressKit, SET_NULL, null=True, related_name='albums')
```

Soft-deleting the album soft-deletes its press kit. Note which way `SET_NULL` points:
soft-deleting the *press kit* runs Django's `Collector`, which clears `press_kit_id` on
every album **before** the rule turns the `DELETE` into an `UPDATE` — the archived kit is
then unreachable from its former owners, and `hard_delete()` can no longer collect it. Use
`DO_NOTHING` (or `PROTECT`/`RESTRICT`) where the pointer must survive the target's archival.

`on_delete=CASCADE` is **refused** (`guitars.E001`): it means deleting the press kit deletes
the album, the opposite of ownership, and would emit the cascade rule backwards. A NULL key
matches nothing, so a nullable owned relation needs no guard of its own. `to_field` is refused
too (`guitars.E002`): the rule correlates the key against the target's *primary* key, which is
also what makes [MTI](#mti) work.

Three more shapes are refused by the generator rather than by a check, warned about the way
the [MTI](#mti) limitation below is, since each depends on the *other* model:

- **A target with no `_deleted_at`.** Nothing for the rule to stamp, and unlike a plain
  `ForeignKey` an `OwningForeignKey` has no other purpose, so this is reported not passed over.
- **An owner with no `_deleted_at`.** The rule fires on the owner's `_deleted_at` transition,
  so a model never soft-deleted would never fire it. Reported for the same reason.
- **A relation closing a cycle of `ON UPDATE` rules** — owning yourself
  (`OwningForeignKey('self', …)`), owning an MTI descendant of yourself, or a longer loop
  back through another model's owned or `CASCADE` rules. A rule's action expands *before*
  the original statement, so a cycle is rewritten into itself and PostgreSQL rejects *every*
  `UPDATE` to *every* table in it — a plain `save()` included — with `infinite recursion
  detected in rules for relation`. Every edge on the cycle is refused rather than one
  chosen edge, which would depend on iteration order. `hard_delete()` refuses these too:
  no rule means nothing was stamped, so nothing may be removed.

## The last-owner guard

A target another live row still points at **survives**; it is stamped when the last owner
goes. Unconditional, not derived from whether a `UniqueConstraint` proves single ownership:
dropping such a constraint is an ordinary migration that changes no field, so the rule's
`[SQL:…]` identity would not move and `--check` would stay green while the database kept an
unguarded rule. See [ADR 0011](adr/0011-owner-side-soft-delete-ownership.md).

`hard_delete()` applies the same test in Python, removing an owned row *after* the batch that
owned it — the reverse of the child-first `CASCADE` order, since the owner still references
it. Three deliberate narrowings, because it *removes* the row where the rule only
stamps a column: the whole batch is spared rather than one row; an **archived** referrer
still counts, its key being on disk; and **any** surviving foreign key holds the row back,
not only the owning column. All three exist because dropping a still-referenced row fails
the deferred constraint at `COMMIT`. Collection runs to a fixpoint, so a row spared by a
reference that is *itself* collected later is picked up on a later pass. A row that stays
spared stays archived. Queryset-level `hard_delete()` walks neither reverse-FK children nor
owned relations.

Two limits the guard does not cover, both by construction:

- **Per column, not per target.** The `NOT EXISTS` looks only at the rule's own foreign-key
  column. A second `OwningForeignKey` on the same table pointing at the same row does not
  spare it. (`hard_delete()` is the exception — see the narrowings above.)
- **Per statement.** PostgreSQL runs an `ON UPDATE` rule's action *before* the original
  update, so every owner soft-deleted by one statement still reads as live to the others'
  guards. `Album.objects.filter(press_kit=kit).delete()` therefore leaves `kit` alive even
  though it deleted every owner; deleting them one at a time stamps it as expected.

## Rule names, and removing one

A rule name is the only thing PostgreSQL dedupes on, not what it references. An inbound
child with two `CASCADE` FKs to one parent gets its second rule suffixed with the FK column
(`soft_delete_related_<child>_<fk>`), the first keeping the bare name for compatibility.
Every *owned* rule is suffixed (`soft_delete_owned_<target>_<fk>`) — nothing predates 2.3.0
to stay compatible with — and the distinct prefix is what keeps the two families from ever
meeting, a collision being a silent replacement rather than an error.

No enforcement command retires a rule, cascade rules included. Dropping an
`OwningForeignKey` therefore fails at `migrate` (the rule depends on the column), and
converting one back to a plain `ForeignKey` silently leaves it live. Add an explicit
`DROP RULE "soft_delete_owned_<target>_<fk>" ON "<owner_table>"` to that migration.

## MTI

Ownership *into* an MTI child works: the key holds the primary-key value every table in
the chain shares, so the rule correlates against the ancestor owning `_deleted_at`.
Ownership declared *on* an MTI child whose `_deleted_at` lives farther up is refused with
a warning — the rule fires on the ancestor's table, where `old."<column>"` cannot name a
column the child holds. See [MTI](mti.md).

## Related

- [Soft deletion](soft-deletion.md) · [Migrations](migrations.md) · [MTI](mti.md)
- [ADR 0011](adr/0011-owner-side-soft-delete-ownership.md) — the two design decisions
