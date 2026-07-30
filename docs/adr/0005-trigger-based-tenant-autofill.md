# 0005 — Tenant autofill belongs in a trigger; the signal stays for diagnostics

- **Status:** proposed
- **Date:** 2026-07-30
- **Affects:** `guitars.sql.triggers`, `makeguitarmigrations`, `guitars.tenancy.manager`,
  `GUITARS_TENANT_AUTOFILL`

## Context

This kit's premise is that object metadata belongs in PostgreSQL rules and triggers
rather than Python `save()`/signal overrides, "because the point is correctness under
`bulk_update`, `queryset.delete()`, and raw SQL — code paths that never touch
`.save()`". `_created_at`, `_updated_at` and `_deleted_at` all follow it.

The tenant column does not. `install_write_guards()` connects a `pre_save` receiver
which does two things: **validates** that a write does not cross tenants, and
**autofills** the tenant column from the active scope. Only the first has a database
counterpart — the `tenant_scope` policy's `WITH CHECK`. Autofill has none, so it is
the reason the receiver exists at all: `GuitarModel`'s tenant FK is `editable=False`
and `NOT NULL`, so without autofill every call site would have to name a
framework-owned field.

Being on a signal has a measured cost. The kit's own `DisableSignals` — reachable
through the supported `instance.update(..., _disable_signals=True)` — switches the
guard off:

```
pre_save receivers, normally:              1
pre_save receivers, inside DisableSignals: 0

unscoped write:  normally → guard fires    with DisableSignals() → does not fire
autofill:        normally → org_id = 4242  with DisableSignals() → org_id = None
```

Nothing leaks: `WITH CHECK` still refuses an actual cross-tenant write, and a missing
autofill surfaces as a `NOT NULL` violation. But a security guard that a sibling
feature can switch off by accident is the same shape as the `_queryset_class` bug
1.0.0 fixed — one that reads as installed and silently does nothing.

`bulk_create` shows the other half of the problem. It sends no `pre_save`, so
`manager.py` has to override it on the queryset *and* re-invoke the guard by hand.
Raw `INSERT`, `INSERT … SELECT` and anything outside the ORM get nothing.

## Decision

**Add a `BEFORE INSERT … FOR EACH ROW` trigger that fills the tenant column from
`current_setting('tenant.<dimension>')` when it is `NULL`.** Generate it from
`makeguitarmigrations` for every table whose coverage has a local tenant column and
whose manager autofills.

**Keep the `pre_save` receiver, demoted to diagnostics.** Correctness moves to the
trigger and `WITH CHECK`; the receiver's job becomes producing the good error message
and honouring `GUITARS_TENANT_ENFORCE = 'audit'`. Losing it to `DisableSignals` then
costs a nice message, not a guarantee.

**Generate one trigger function per distinct (column, GUC) pair**, hosted in
`TRIGGER_FUNCTION_APP` beside the existing singleton functions — not one generic
function for all tables, and not one per table.

## Why

**The trigger covers what the signal cannot.** Verified against PostgreSQL 18, with
the scope opened through `tenant()` so the real execute wrapper publishes it:

| Path | `pre_save` | `BEFORE INSERT` trigger |
| --- | --- | --- |
| `instance.save()` / `create()` | ✅ | ✅ |
| `bulk_create()` | only via the queryset override | ✅ |
| multi-row `INSERT` | ❌ | ✅ |
| `INSERT … SELECT` | ❌ | ✅ |
| raw SQL outside the ORM | ❌ | ✅ |

**The ordering works, and it had to be checked.** `WITH CHECK` and `NOT NULL` are both
evaluated on the row the `BEFORE` trigger returns, not the one the statement supplied
— an `INSERT` omitting the tenant column succeeds, and the stored value is the scoped
tenant. Had it been the other way round the design would be dead on arrival, since the
pre-trigger row's `NULL` satisfies neither.

**It does not weaken `WITH CHECK`.** The trigger only fills a `NULL`; an explicit
cross-tenant value is still refused. Measured behaviour:

| Situation | Outcome |
| --- | --- |
| scope active, column omitted | filled from the scope |
| scope active, explicit cross-tenant value | refused by the policy |
| no scope at all | refused by the policy (column stays `NULL`) |
| scope names several tenants | refused by the policy (declines to guess) |
| `tenancy_bypassed()`, column omitted | `NOT NULL` violation |
| `tenancy_bypassed()`, explicit value | allowed — the deliberate cross-tenant path |

**One function per (column, GUC) pair, because the generic form is measurably
slower.** PL/pgSQL cannot write a dynamically-named column on `NEW`; reaching one
needs a `to_jsonb` / `jsonb_populate_record` round trip. Server-side cost, one
20 000-row `INSERT`, isolated from round-trip time:

| | µs/row | vs baseline |
| --- | --- | --- |
| no trigger (column supplied) | 3.76 | — |
| static `NEW.org_id` | 3.87 | +3% |
| generic, `TG_ARGV` + jsonb round trip | 6.05 | **+61%** |

A first pass measured these through `executemany` and saw 35 vs 36 µs/row — a
difference of 3%, which would have argued for the generic form. That measurement was
dominated by network round-trip and hid the real ratio. The numbers above are the
ones to trust.

The static form needs the column name baked in, which sounds like one function per
table until you notice `GUITARS_TENANT_FIELD` is a single project-wide setting: every
`GuitarModel` subclass in a project shares the same column name. So "one per distinct
(column, GUC) pair" is **one function** for a typical project, and mirrors how
`set_updated_at` is a single shared function rather than a per-table one. Only
hand-rolled `TenantedManager` models on other columns add more.

**`COPY FROM` needs no consideration.** PostgreSQL refuses it outright on any table
with row-level security — `FeatureNotSupported: COPY FROM not supported with
row-level security`. Bulk loading into a tenanted table is already impossible, so it
is not a gap this trigger has to close.

## Why the signal is not deleted

The earlier framing of this work was that the trigger would let the `pre_save`
receiver be removed. That is wrong, for two reasons found while scoping it.

**Audit mode has no database analogue.** `GUITARS_TENANT_ENFORCE = 'audit'` reports a
write violation and *proceeds*, so a populated deployment can be told where its
offending call sites are without 500-ing them. There is no "report and continue" in a
trigger or a policy — SQL can refuse or permit, and [`docs/tenancy.md`](../tenancy.md)
already documents that audit mode does not soften the database. Deleting the receiver
would delete audit mode for writes, which is the feature that makes a rollout onto a
populated database survivable.

**The database's error messages are worse, and in one case misleading.** Today's guard
names the model, the dimension and the fix — *"`Release` write is missing `'label'`.
Pass it explicitly, or enable `GUITARS_TENANT_AUTOFILL`"*. After the change, the
same mistakes surface as `null value in column "org_id" violates not-null constraint`
or the generic policy rejection. The multi-tenant-scope case is the worst: the row is
refused with *"the row does not belong to the active tenant, or no tenant scope is
active"*, when in fact a scope **is** active and the real problem is that it names
several tenants so autofill cannot pick one. The Python guard says exactly that.

So the receiver earns its place as a diagnostic layer. What changes is that it stops
being the thing correctness depends on — which is the whole point.

## Consequences

- **`DisableSignals` stops being able to disable tenancy enforcement.** It will still
  suppress the friendly error; the trigger and `WITH CHECK` do not notice it. This is
  the finding that motivated the ADR.
- **`bulk_create`'s queryset override becomes belt-and-braces**, kept for the error
  message rather than for correctness. The `_guarded()` call in
  `_untenanted_queryset_class.bulk_create` can keep its current comment but loses its
  load-bearing status.
- **New frozen SQL names**, so `sql/__init__.py` re-exports them and
  `FROZEN_SQL_NAMES` records them — per the rule in CLAUDE.md, a name that ships must
  keep resolving.
- **A migration per tenanted table**, plus one function migration. Existing databases
  need `makemigrations` + `migrate`; nothing is rewritten.
- **`autofill=False` becomes visible in the schema.** A model that opts out (an
  append-only archive) simply gets no trigger, so the opt-out is auditable in
  `pg_trigger` instead of living only in a manager argument.
- **`audittenancy` should learn to check it.** A tenanted table whose manager
  autofills but whose trigger is missing is exactly the "looks fine, is not" state the
  command exists to catch, and it is invisible to every current check.
- **Multi-hop dimensions still cannot be autofilled**, by the same reasoning that
  excludes them from policies: there is no column on this table to write. Unchanged
  from today, where `TenantedManager` rejects `autofill` with a multi-hop lookup.

## Related

- [ADR 0002 — FORCE ROW LEVEL SECURITY by default](0002-force-rls-by-default.md)
- [ADR 0004 — `base_manager_name` is deliberately left unset](0004-unscoped-base-manager.md)
  — the same "which layer should own this?" question, answered the other way
- [docs/tenancy.md](../tenancy.md), [docs/migrations.md](../migrations.md)
