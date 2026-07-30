# Multi-table inheritance

A concrete model subclassing another concrete `SetarModel` / `GuitarModel` /
`DatedModel` / `SoftDeletableModel` is fully supported.

```python
class Ensemble(SetarModel):
    name = models.CharField(max_length=100)


class Orchestra(Ensemble):          # its own table
    conductor = models.CharField(max_length=100)

    class Meta:                      # required — see below
        pass


class ChamberOrchestra(Orchestra):   # and another
    seats = models.IntegerField(default=0)

    class Meta:
        pass
```

## The one piece of boilerplate

> ⚠️ **An MTI child of a soft-deletable base must declare its own `Meta`.** An
> empty `class Meta: pass` is enough.

Otherwise Django re-declares the parent's partial `_deleted_at` index against the
child's table, which has no such column, and raises `models.E016`. Managers are
still inherited either way.

## Why it needs special handling at all

`_updated_at` and `_deleted_at` physically live on the **ancestor that declares
them**. The child's table has neither column. So:

- A rule or trigger referencing `_deleted_at` on the child's table is invalid SQL.
- `hasattr(Child, "_deleted_at")` is `True` and therefore useless — it answers a
  question about Python attributes, not about columns.

Everything below follows from resolving the **owner** — the concrete model whose
physical table declares the column — via `model._meta.get_field(name).model`. That
lives once in `guitars.introspection`, shared by the migration generator and by
tenancy policy discovery.

Note *owner*, not *parent*. In a chain three deep the column may live two tables
up, and predicating against the immediate parent would reference a table that has
no such column either.

## The shared-PK invariant

Every table in an MTI chain shares one primary-key **value**: the child's PK is a
`OneToOneField(parent_link=True)` holding the ancestor's id. That single fact is
what makes all four mechanisms below sound, and it is worth stating plainly because
each of them is a correlated `WHERE owner_pk = child_pk`.

## What each child table gets

### Soft deletion — a redirect rule

```sql
CREATE RULE soft_delete AS ON DELETE TO <child>
    DO INSTEAD (UPDATE <owner> SET _deleted_at = NOW()
                WHERE <owner_pk> = old.<child_pk> AND _deleted_at IS NULL);
```

`DO INSTEAD` preserves the child row and stamps the **owner**. Django deletes
child-before-parent, so the parent's own rule then no-ops via its
`_deleted_at IS NULL` guard — cascades fire exactly once, in both delete
directions and at any depth.

A consequence worth knowing: because the statement is rewritten, the reported row
count describes the *substituted* `UPDATE`, not the `DELETE` you issued. Assert on
effects, not on `cursor.rowcount`.

### `_updated_at` — a parent-propagating trigger

A child-only `QuerySet.update()` touches only the child table, so the owner's
`_updated_at` would go stale. Each MTI child therefore gets:

```sql
CREATE TRIGGER updated_at_trigger AFTER UPDATE ON <child>
    REFERENCING NEW TABLE AS new_table FOR EACH STATEMENT
    WHEN (pg_trigger_depth() = 0)
    EXECUTE FUNCTION set_parent_updated_at(…);
```

`FOR EACH STATEMENT` (not per row) and `pg_trigger_depth() = 0` (so the write it
performs does not re-enter).

### Tenancy — an owner-join policy

The child gets its **own** row-level-security policy, correlated to the owner:

```sql
EXISTS (SELECT 1 FROM <owner> AS o
        WHERE o.<owner_pk> = <child>.<child_pk>
          AND o.<tenant_col>::text = ANY(…))
```

It does not rely on the ancestor's policy. "Every query joins the parent" is
false, and this kit already knew it — that is precisely why
`set_parent_updated_at` exists, reached from the other direction. A child-only
statement never touches the ancestor, so an ancestor-only policy never applies to
it. See [ADR 0003](adr/0003-mti-owner-join-policy.md).

### Cascades *into* an MTI child

A `CASCADE` FK whose target is an MTI child attaches its cascade rule to the
target's **owner** table — the FK column holds the shared PK, so matching it
against the owner's PK still works.

The MTI parent-link itself (a `CASCADE` `OneToOne`) is skipped: it is structural,
already handled by the redirect rule, not a user cascade FK.

An FK reached *through* MTI is likewise not a second FK — it is the same physical
column on the ancestor's table, and the ancestor's own rule already archives the
whole chain through the shared `_deleted_at`.

## Hard deletion across the chain

- **Instance-level** starts its DFS from the MTI **root**, because the parent-link
  reverse relation is itself a `CASCADE` relation. Every table in the chain, and
  any `CASCADE` child of any ancestor, is collected into one child-first order and
  each table is deleted via the own-table primitive.
- **Queryset-level** deletes the whole table chain leaf-to-root by shared PK, so no
  orphaned ancestor row is left, regardless of which level the queryset was on.

## Known limitation

Cascading into an MTI child through an FK declared on the child's **own** table
while its `_deleted_at` lives on a farther ancestor is not supported: the flat
`UPDATE <child> SET _deleted_at` form would reference a column that table does not
have, and the join form is not emitted yet.

`makeguitarmigrations` skips it with a warning naming the models rather than
emitting broken SQL.

## Where to look

`tests/testapp/models.py` carries two chains — `Ensemble → Orchestra →
ChamberOrchestra` (untenanted, plus `Section` cascading into `Orchestra`) and
`Tour → WorldTour → StadiumTour` (tenanted, so the owner-join resolves two tables
up). `tests/test_mti.py` and `tests/test_tenancy_models.py` exercise them.

## Related

- [Soft deletion](soft-deletion.md) · [Migrations](migrations.md) · [Tenancy](tenancy.md)
