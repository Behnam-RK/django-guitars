# Soft deletion

`.delete()` never reaches Python. A PostgreSQL `ON DELETE … DO INSTEAD` rule
rewrites it into an `UPDATE` that stamps `_deleted_at`. That is the whole
point: a `save()` override or `pre_delete` receiver is skipped by
`queryset.delete()`, a cascade, or raw SQL — a rule is not.

## Using it

Inherit `SetarModel` (or the `SoftDeletableModel` mixin alone):

```python
from guitars.models import SetarModel

class Article(SetarModel):
    title = models.CharField(max_length=200)

article.delete()              # sets _deleted_at; the row stays
article.is_deleted            # True
article.is_alive              # False
Article.objects.all()         # live rows only (the default manager)
Article._archives.all()       # soft-deleted rows only
Article._all_objects.all()    # everything
```

> ⚠️ **The rule lives in a migration.** Until `makemigrations` has generated it
> and you have run `migrate`, `.delete()` **permanently deletes the row** — see
> [Migrations](migrations.md).

## Cascades

Soft-deleting a row also soft-deletes rows related by `on_delete=CASCADE`, via a
second rule on the parent's table that keys off the `_deleted_at` transition
(not `.delete()`), so it fires for bulk deletes and raw SQL too. Non-`CASCADE`
relations get no rule, and the cascade only reaches models that are themselves
soft-deletable — a plain `Model` is deleted for real. A `CASCADE` FK that would close a **cycle** of `ON UPDATE` rules — self-referential, or a loop through another model's cascade or [owned](owned-relations.md) rules — gets no rule either, with a warning: PostgreSQL rewrites such a cycle into itself and rejects *every* `UPDATE` to *every* table in it. Cascade that step in Python.

The reverse case, and how both kinds of rule are named: [Owned relations](owned-relations.md).

## Hard deletion

```python
article.hard_delete()                            # this row, CASCADE children, owned rows
Article._all_objects.filter(...).hard_delete()   # in bulk
```

`hard_delete()` opts out by setting a transaction-local session variable every rule tests: `SELECT set_config('rules.hard_deletion', 'on', TRUE)`.

**Every rule guard is written `<> 'on'`, never `= 'off'`.** A session variable
never set reads as `NULL`, but one set transaction-locally and then *rolled
back* reads as the **empty string** — a placeholder Postgres leaves rather than
removing, so `= 'off'` would match neither and silently stop the rule. The blast
radius is the *connection*: with any pool, one rolled-back `hard_delete()` turns
every later `.delete()` there into a real delete.

> **If your database was migrated before 1.0.0** it still carries the old guard.
> Regenerate via `makeguitarmigrations`/`makemigrations` then `migrate`;
> `--check` fails until you do. See [Migrations](migrations.md). **Do not** fix
> this by reversing the enforcement migration: `reverse_sql` *drops* the rules,
> and `migrate <app> <previous>` unapplies later ones too.

**Instance-level `hard_delete()` is two-phase:** soft-delete first (so cascade
rules fire), then DFS-collect `CASCADE` children through `_all_objects` and
hard-delete child-first — Django's `CASCADE` is Python-level (`Collector`), not
`ON DELETE CASCADE`, so a raw parent `DELETE` would fail the FK check. An owned
row goes the other way — *after* its owner, which still references it.
`GenericRelation` children come from `_meta.private_fields` (2.7.0), holding
nothing back: no key column, so no constraint to fail.

Queryset-level `hard_delete()` is blunter: it deletes matched rows (and, for
MTI, the whole chain) but walks no reverse-FK children and no owned relations.

## Managers and the base manager

`objects` filters `_deleted_at IS NULL`, `_archives` filters `IS NOT NULL`,
`_all_objects` filters neither. `Meta.default_manager_name` is `objects`.
`base_manager_name` is deliberately **not** set, so `_base_manager` stays
Django's plain unfiltered manager: a soft-delete filter there would make a FK
pointing at an archived row raise `RelatedObjectDoesNotExist`. See
[ADR 0004](adr/0004-unscoped-base-manager.md).

## The partial index

`SoftDeletableModel.Meta` declares:

```python
Index(fields=["_deleted_at"], condition=Q(_deleted_at__isnull=True),
      name="%(class)s_deleted_at")
```

Partial, since the overwhelmingly common query is "live rows". `%(class)s` is
what lets one abstract declaration produce a unique index name per concrete
model — and why an MTI child must declare its own `Meta`; see [MTI](mti.md).

## Related

- [Owned relations](owned-relations.md) — soft deletion in the other direction
- [Migrations](migrations.md) — how the rules get into the database
- [MTI](mti.md) — soft deletion across an inheritance chain
- [Tenancy](tenancy.md) — soft deletion under RLS
