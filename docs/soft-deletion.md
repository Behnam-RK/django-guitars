# Soft deletion

`.delete()` never reaches Python. A PostgreSQL `ON DELETE … DO INSTEAD` rule
rewrites it into an `UPDATE` that stamps `_deleted_at`.

That is the whole point. A `save()` override or a `pre_delete` receiver is
skipped by `queryset.delete()`, by a cascade, and by raw SQL. A rule is not.

## Using it

Inherit `SetarModel` (or the `SoftDeletableModel` mixin alone):

```python
from guitars.models import SetarModel


class Article(SetarModel):
    title = models.CharField(max_length=200)
```

```python
article.delete()              # sets _deleted_at; the row stays
article.is_deleted            # True
article.is_alive              # False

Article.objects.all()         # live rows only (the default manager)
Article._archives.all()       # soft-deleted rows only
Article._all_objects.all()    # everything
```

> ⚠️ **The rule lives in a migration.** Until `makemigrations` has generated it
> and you have run `migrate`, `.delete()` **permanently deletes the row** — there
> is nothing intercepting it yet. See [Migrations](migrations.md).

## Cascades

Soft-deleting a row also soft-deletes rows related by `on_delete=CASCADE`, via a
second rule on the parent's table:

```sql
CREATE RULE soft_delete_related_<child> AS ON UPDATE TO <parent>
    WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND …
    DO ALSO (UPDATE <child> SET _deleted_at = NOW() WHERE <fk> = old.<pk>);
```

Because it keys off the `_deleted_at` transition rather than off `.delete()`, it
fires for bulk deletes and raw SQL too. Non-`CASCADE` relations (`SET_NULL`,
`PROTECT`, `DO_NOTHING`) get no rule — Django's own semantics stand.

The cascade only reaches models that are themselves soft-deletable. A plain
`Model` with a `CASCADE` FK is deleted for real by Django's collector, as it
always was.

## Hard deletion

```python
article.hard_delete()                            # this row + CASCADE children
Article._all_objects.filter(...).hard_delete()   # in bulk
```

`hard_delete()` opts out of the rule by setting a transaction-local session
variable that every rule tests:

```sql
SELECT set_config('rules.hard_deletion', 'on', TRUE);
```

Two things about that are load-bearing.

**Every rule guard is written `<> 'on'`, never `= 'off'`.** A custom session
variable that has never been set reads as `NULL`, but one that was set
transaction-locally and then *rolled back* reads as the **empty string** —
PostgreSQL leaves a placeholder behind rather than removing it. Under `= 'off'`
that empty string matched neither branch, the rule stopped firing, and `DELETE`
meant what it says. The blast radius was the *connection*, not the transaction:
with `CONN_MAX_AGE` or any pool, one rolled-back transaction containing a
`hard_delete()` would silently turn every later `.delete()` on that connection
into permanent data loss.

> **If your database was migrated before 1.0.0** it still carries the old guard.
> Re-apply the enforcement migration to replace the rules — `migrate <app>
> <previous>` then `migrate <app>` — since its `reverse_sql` drops them and the
> forward re-creates them from the fixed SQL.

**Instance-level `hard_delete()` is two-phase.** It soft-deletes first (so the
cascade rules fire), then DFS-collects `CASCADE` children through `_all_objects`
and hard-deletes child-first. That order is not decoration: Django's `CASCADE` is
Python-level (`Collector`), so PostgreSQL has no `ON DELETE CASCADE` constraint,
and a raw parent `DELETE` would be rejected by the FK check.

Queryset-level `hard_delete()` is blunter: it deletes the matched rows (and, for
MTI, the whole table chain by shared PK) but does **not** walk reverse-FK
children. Use the instance form when you need that.

## Managers and the base manager

`objects` filters `_deleted_at IS NULL`, `_archives` filters `IS NOT NULL`,
`_all_objects` filters neither. `Meta.default_manager_name` is `objects`.

`base_manager_name` is deliberately **not** set, so `_base_manager` stays
Django's plain unfiltered manager. Django's own rule is that a base manager must
not filter rows: `_base_manager` is what fetches related objects, so a
soft-delete filter there would make a FK pointing at an archived row raise
`RelatedObjectDoesNotExist` — naming the wrong problem entirely. See
[ADR 0004](adr/0004-unscoped-base-manager.md).

## The partial index

`SoftDeletableModel.Meta` declares:

```python
Index(fields=["_deleted_at"], condition=Q(_deleted_at__isnull=True),
      name="%(class)s_deleted_at")
```

Partial, because the overwhelmingly common query is "live rows", and indexing
only those keeps it small. The `%(class)s` template is what lets one abstract
declaration produce a unique index name per concrete model — and it is also why
an MTI child must declare its own `Meta`; see [MTI](mti.md).

## Related

- [Migrations](migrations.md) — how the rules get into the database
- [MTI](mti.md) — soft deletion across an inheritance chain
- [Tenancy](tenancy.md) — soft deletion under row-level security
