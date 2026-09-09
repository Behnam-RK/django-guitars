# Multi-table inheritance

A concrete model subclassing another concrete `SetarModel` / `GuitarModel` / `DatedModel` /
`SoftDeletableModel` is fully supported.

```python
class Ensemble(SetarModel):
    name = models.CharField(max_length=100)

class Orchestra(Ensemble):          # its own table
    conductor = models.CharField(max_length=100)
    class Meta: pass                 # required — see below
```

## The one piece of boilerplate

> ⚠️ **An MTI child of a soft-deletable base must declare its own `Meta`** (`class Meta: pass`
> is enough), or Django re-declares the parent's partial `_deleted_at` index against a table
> with no such column and raises `models.E016`. Managers are still inherited either way.

## Why it needs special handling at all

`_updated_at` and `_deleted_at` physically live on the **ancestor that declares them** — the
child's table has neither column, so a rule/trigger referencing them there is invalid SQL, and
`hasattr(Child, "_deleted_at")` is `True` and useless: Python attributes, not columns.

Everything below resolves the **owner** — the concrete model whose physical table declares the
column — via `model._meta.get_field(name).model` (not *parent*: the column may live two tables
up). Every table in a chain shares one primary-key **value** via
`OneToOneField(parent_link=True)`, a correlated `WHERE owner_pk = child_pk`.

A **proxy is not an MTI child** though it reads as one: Django fills its `_meta.parents` and it
declares no field, so operations it earned named its own table as parent. Answered `False` since
2.9.1, and relations keyed on `_meta.concrete_model` — `related_model` at a proxy is the proxy.

## What each child table gets

**Soft deletion — a redirect rule** preserves the child row and stamps the **owner**:

```sql
CREATE RULE soft_delete AS ON DELETE TO <child>
    DO INSTEAD (UPDATE <owner> SET _deleted_at = NOW()
                WHERE <owner_pk> = old.<child_pk> AND _deleted_at IS NULL);
```

Django deletes child-before-parent, so the parent's own rule no-ops via its `_deleted_at IS
NULL` guard — cascades fire once, at any depth. `cursor.rowcount` describes the *substituted*
`UPDATE`.

The **inverse** shape — a child carrying `_deleted_at`, declared or inherited from a second
parent, over a concrete parent that has none — is **refused** ([`guitars.E003`](adr/0015-refuse-soft-deletable-mti-orphans.md),
2.7.0): the child's rule keeps its row while the ancestor's unguarded `DELETE` removes what it
points at. No rule means `.delete()` destroys the chain rather than aborting — hence an
`Error`, which `--skip-checks` walks past. A concrete descendant is refused with it.

**`_updated_at` — a parent-propagating trigger.** A child-only `QuerySet.update()` touches
only the child table, so the owner's `_updated_at` would go stale without one:

```sql
CREATE TRIGGER updated_at_trigger AFTER UPDATE ON <child>
    REFERENCING NEW TABLE AS new_table FOR EACH STATEMENT
    WHEN (pg_trigger_depth() = 0) EXECUTE FUNCTION set_parent_updated_at(…);
```

`FOR EACH STATEMENT` (not per row) and `pg_trigger_depth() = 0` (no re-entry). Schema-qualified
`db_table` is supported: the function takes the parent's schema and table as two arguments
(`%I` can't render a two-part name) and still understands the older three-argument form, frozen
per-trigger at `CREATE TRIGGER` time — see `tests/test_schema_qualified.py`. The own-table
trigger has the same constraint via `search_path`.

**Tenancy — an owner-join policy**, correlated to the owner rather than relying on the
ancestor's policy:

```sql
EXISTS (SELECT 1 FROM <owner> AS o
        WHERE o.<owner_pk> = <child>.<child_pk>
          AND o.<tenant_col>::text = ANY(…))
```

"Every query joins the parent" is false, which is why `set_parent_updated_at` exists, reached the
other way — [ADR 0003](adr/0003-mti-owner-join-policy.md).

## Cascades and hard deletion

- **Cascades *into* an MTI child** attach to the target's **owner** table (the FK column holds
  the shared PK); the parent-link is skipped as structural, already handled by the redirect
  rule — an FK reached *through* MTI is the same physical column.
- **`hard_delete()`** DFS from the MTI **root** at the instance level (the parent-link reverse
  relation is itself `CASCADE`), collecting every table in the chain child-first; at the
  queryset level it deletes the whole chain leaf-to-root by shared PK.
- **Known limitation:** a *cascade* FK on a child's own table while `_deleted_at` lives farther
  up warns instead of emitting broken SQL; an [`OwningForeignKey`](owned-relations.md#mti) too.

`tests/testapp/models.py` carries `Ensemble → Orchestra → ChamberOrchestra` (untenanted) and
`Tour → WorldTour → StadiumTour` (tenanted, owner-join two tables up); `tests/test_mti.py` and
`tests/test_tenancy_models.py` exercise them, `tests/test_proxy_models.py` the proxy shape.

## Related

- [Soft deletion](soft-deletion.md) · [Owned relations](owned-relations.md) · [Migrations](migrations.md) · [Tenancy](tenancy.md)
