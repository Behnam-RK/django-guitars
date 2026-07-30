# 0004 — `base_manager_name` is deliberately left unset

- **Status:** accepted
- **Date:** 2026-07-30
- **Affects:** `guitars.models.GuitarModel.Meta`, `guitars.models.SoftDeletableModel.Meta`

## Context

`Model._meta.base_manager` is what Django uses to fetch related objects — so
`select_related`, a forward FK access, and `refresh_from_db` all go through it,
and none of them consults `objects`.

With `base_manager_name` unset, Django creates a plain unfiltered `Manager` for
the purpose. So on a tenanted model, `_base_manager` applies **no tenant filter**.

Pointing it at a tenant-scoped manager is the obvious move: it would make
`select_related` raise `TenantScopeError` instead of returning another tenant's
row, which is the safe direction.

## Decision

Leave it unset. `_base_manager` stays Django's plain manager on every rung.

## Why

**1. Django's own rule is that a base manager must not filter rows.** A forward FK
pointing at a row the filter hides raises `RelatedObjectDoesNotExist` — which names
the wrong problem entirely. This is not merely a tenancy concern: it is also why
`SoftDeletableModel` does not point `base_manager_name` at `objects`, since a FK to
an archived row would appear not to exist.

**2. It is on the `save()` path, where the deny-list does not reach.**
`Model._save_table` calls `cls._base_manager` for both `_do_insert` and
`_do_update`. And `QuerySet._insert` / `_update` are declared
`queryset_only = False`, which means they are *not* on the deny-list that a missing
scope raises from. So a scoped base manager would produce:

| operation | with a scoped base manager |
| --- | --- |
| `refresh_from_db()` unscoped | raises |
| related fetch unscoped | raises |
| `instance.save()` unscoped | **passes** |

Writes through, reads denied — enforcement that is partial in a way nobody could
predict from the outside. Partial enforcement in a security feature is worse than
none, because it invites the wrong mental model.

**3. It is the canonical path the database layer exists to cover, and does.**
Verified, not argued:

```python
with tenant(label=a):
    assert Release._base_manager.count() == 1     # of 2 rows in the table
with tenancy_bypassed():
    assert Release._base_manager.count() == 2
```

(`test_the_base_manager_is_scoped_by_the_policy`.) The policy does not care which
manager asked. Half-covering the path in Python would add a failure mode without
closing a hole.

## Consequences

- **The Python layer has a known gap**, and it is the right one: a developer doing
  ad-hoc related access gets no loud error. They also get no leak, because the
  policy filters the statement.
- **This is a real argument for `GUITARS_TENANT_POLICIES = True`.** A project that
  turns the database layer off is choosing a configuration where `_base_manager`
  genuinely is unscoped. That is what the setting's documentation says, and it is
  why the default is `True`.
- **If Django ever put `_insert`/`_update` behind the deny-list**, or offered a
  base-manager hook that is not on the save path, this decision would be worth
  revisiting. The reasoning is recorded in `GuitarModel.Meta` next to the absent
  setting, so the next reader finds it where they would look.

## Related

- [ADR 0001 — the swappable tenant FK](0001-swappable-tenant-model.md)
- [docs/tenancy.md](../tenancy.md) · [docs/soft-deletion.md](../soft-deletion.md)
