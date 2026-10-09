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

Soft-deleting a row also soft-deletes rows related by `on_delete=CASCADE`, via an arm
of the parent table's statement-level trigger that keys off the `_deleted_at` transition
(not `.delete()`), so it fires for bulk deletes and raw SQL too -- a second `ON UPDATE` rule
until 2.19.0, which the rewriter planned on every `UPDATE` ([ADR 0039](adr/0039-cascade-arms-in-the-owner-trigger.md)). Non-`CASCADE`
relations get no arm, and the cascade only reaches models that are themselves
soft-deletable — a plain `Model` is deleted for real. A cascaded child is stamped with its **parent's own** `_deleted_at`, not a fresh `NOW()`, so one archive reads as one instant however deep it goes; and a child already archived is left alone, keeping when *it* went. Through 2.11.0 it was re-stamped, silently losing that ([ADR 0023](adr/0023-cascade-stamps-its-parents-timestamp.md)). A **self-referential** `CASCADE` FK (a tree) whose table owns `_deleted_at` gets no rule but a statement-level `AFTER UPDATE` trigger instead, since 2.8.0: a rule updating the table it fires on is rewritten into itself and PostgreSQL then rejects *every* `UPDATE` there, an ordinary `save()` included, while a trigger takes no part in rewriting. It re-fires itself for each level down, stopping where a level archives nothing, and each level's own `UPDATE` fires the table's other cascade arms — so a tree's ordinary children go with it. A cycle through **two or more** tables is still refused a rule, with a warning, and still wants that step in Python: the same trigger would unbrick it, but *which* of its edges to convert has no answer that holds as models are added. One statement class is **refused** outright: archiving a row whose primary key the same statement rewrites, where a live child holds either key — the trigger correlates on that key, so it cannot tell which row was archived and would leak the subtree. Rewrite the key and archive in separate statements. See [ADR 0018](adr/0018-self-referential-cascade-trigger.md). A key through **`to_field`** matches on that column, not the owner's primary key (2.17.0, [ADR 0035](adr/0035-cascade-keys-through-to-field.md)): its archive and revive arms and the self-cascade trigger read the column off the row, while pairing a row across a statement stays on the pk. A column declared below the table holding `_deleted_at` is refused, and named on stderr.

Clearing a parent's `_deleted_at` **revives the children that archive took**, via a statement-level trigger since 2.11.0: one per cascade rule until 2.16.0, since then one per owner table carrying every key as an arm (the archive arms too, since 2.19.0), which leaves at its first test unless the statement flipped a row ([ADR 0033](adr/0033-one-revive-trigger-per-owner.md)). Paired, not universal: a child archived independently beforehand keeps its own timestamp and is left alone, which is what the parent's-timestamp stamping above is for. The owned family and the self-referential trigger stay archive-only by design — a last-owner guard and a subtree walk have no unambiguous inverse. Two exposures remain, both narrower than the stranding they replace: a child archived in the *same transaction* as its parent is revived with it, and re-stamping an already-archived parent leaves its children unreachable to any later revive. See [ADR 0024](adr/0024-inverse-cascade-revive-rules.md).

The reverse case, and how both kinds of rule are named: [Owned relations](owned-relations.md).

## Hard deletion

```python
article.hard_delete()                            # this row, CASCADE children, owned rows
Article._all_objects.filter(...).hard_delete()   # in bulk
```

`hard_delete()` opts out by setting a transaction-local session variable every rule tests: `SELECT set_config('rules.hard_deletion', 'on', TRUE)`. An instance walk sets it **once** for every table (a table without `_all_objects` is deleted through the collector with it switched off), and a failing walk is rolled back with it; a plain self-referential `CASCADE` key is read as one `WITH RECURSIVE` query however deep the tree (an MTI chain, a `to_field` key or a pk the ORM converts or that is itself a key keeps the level walk, and Phase 1's collector still reads a level at a time), and the owned-row fixpoint reads each owner once. Every `DELETE` the walk issues on a soft-deletable table must remove exactly the rows it collected for that table: one hidden by a tenant scope or row policy, removed first by another transaction, or already gone (a table with no soft-delete rule yet, an owned key pointing at no row) raises `HardDeleteIncompleteError` and rolls the whole walk back rather than commit part of the tree (2.15.0, [ADR 0032](adr/0032-hard-delete-removes-everything-it-collected.md)).

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
It refuses a sliced, combined, `distinct(*fields)` or `values()` queryset, as `delete()` does. A filter on a window or an aggregate, which no `DELETE` can hold, is read as keys first; the MTI form and that path are held to the same row count.

## Managers and the base manager

`objects` filters `_deleted_at IS NULL`, `_archives` filters `IS NOT NULL`, `_all_objects` filters neither. `Meta.default_manager_name` is `objects`. `base_manager_name` is deliberately **not** set, so `_base_manager` stays Django's plain unfiltered manager: a soft-delete filter there would make a FK pointing at an archived row raise `RelatedObjectDoesNotExist`. See [ADR 0004](adr/0004-unscoped-base-manager.md). That filter constrains a `WHERE` as well as a `SELECT`, so `objects.update()` and `objects.bulk_update()` against archived rows match nothing, change nothing and raise nothing — **every revive path goes through `_all_objects`**.

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
- [`soft_delete()` and the fast path](soft-delete-api.md) — archiving without the collector
- [Tenancy](tenancy.md) — soft deletion under RLS
