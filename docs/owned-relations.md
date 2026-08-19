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

Soft-deleting the album soft-deletes its press kit. `on_delete=CASCADE` is **refused**
(`guitars.E001`): it means deleting the press kit deletes the album, the opposite of
ownership, and would emit the cascade rule backwards. A NULL key matches nothing, so a
nullable owned relation needs no guard of its own.

## The last-owner guard

A target another live row still points at **survives**; it is stamped when the last owner
goes. This is unconditional, not derived from whether a `UniqueConstraint` proves single
ownership — dropping such a constraint is an ordinary schema migration that changes no
field, so the rule's `[SQL:…]` identity would not move and `--check` would stay green
while the database kept an unguarded rule. See
[ADR 0011](adr/0011-owner-side-soft-delete-ownership.md).

`hard_delete()` applies the same test in Python, so both paths spare the same rows, and
removes an owned row *after* the batch that owned it — the reverse of the child-first
`CASCADE` order, since the owner still references it. Queryset-level `hard_delete()` walks
neither reverse-FK children nor owned relations.

## Rule names

A rule name is the only thing PostgreSQL dedupes on, not what it references.

- **Inbound.** A child with more than one `CASCADE` FK to the same parent gets its second
  rule suffixed with the FK column (`soft_delete_related_<child>_<fk>`); the first keeps
  the bare name for backward compatibility.
- **Owned.** Every rule is suffixed (`soft_delete_owned_<target>_<fk>`) — nothing predates
  2.3.0 to stay compatible with. The distinct prefix is what keeps the two families from
  ever meeting: a collision would silently replace the other rule rather than fail.

## MTI

Ownership *into* an MTI child works: the key holds the primary-key value every table in
the chain shares, so the rule correlates against the ancestor owning `_deleted_at`.
Ownership declared *on* an MTI child whose `_deleted_at` lives farther up is refused with
a warning — the rule fires on the ancestor's table, where `old."<column>"` cannot name a
column the child holds. See [MTI](mti.md).

## Related

- [Soft deletion](soft-deletion.md) · [Migrations](migrations.md) · [MTI](mti.md)
- [ADR 0011](adr/0011-owner-side-soft-delete-ownership.md) — why a field subclass, and why
  the guard is unconditional
