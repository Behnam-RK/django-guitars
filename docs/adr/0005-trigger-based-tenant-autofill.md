# 0005 — Tenant autofill belongs in a trigger; the signal stays for diagnostics

- **Status:** accepted — implemented in 2.1.0
- **Date:** 2026-07-30 (accepted 2026-08-14, implemented 2026-08-16)
- **Affects:** `guitars.sql.triggers`, `makeguitarmigrations`, `guitars.tenancy.enforcement`, `GUITARS_TENANT_AUTOFILL`

## Context

This kit's premise is that object metadata belongs in PostgreSQL rules and triggers rather than Python `save()`/signal overrides, because the point is correctness under `bulk_update`, `queryset.delete()`, and raw SQL — code paths that never touch `.save()`. `_created_at`, `_updated_at` and `_deleted_at` all follow it. The tenant column does not: `install_write_guards()` connects a `pre_save` receiver that **validates** a write does not cross tenants and **autofills** the tenant column from the active scope. Only validation has a database counterpart (`tenant_scope`'s `WITH CHECK`); autofill has none, so it is the reason the receiver exists at all — `GuitarModel`'s tenant FK is `editable=False` and `NOT NULL`, so without autofill every call site would have to name a framework-owned field.

Being on a signal has a measured cost: the kit's own `DisableSignals` (reachable through `instance.update(..., _disable_signals=True)`) switches the guard off entirely — an unscoped write that normally fires the guard does not, and autofill that normally sets `org_id = 4242` leaves it `None`. Nothing leaks (`WITH CHECK` still refuses an actual cross-tenant write, and a missing autofill surfaces as a `NOT NULL` violation), but a security guard a sibling feature can switch off by accident is the same shape as the `_queryset_class` bug 1.0.0 fixed — one that reads as installed and silently does nothing. `bulk_create` shows the other half: it sends no `pre_save`, so `querysets.py` overrides it on the queryset and re-invokes the guard by hand, while raw `INSERT`, `INSERT … SELECT`, and anything outside the ORM get nothing at all.

## Decision

**Add a `BEFORE INSERT … FOR EACH ROW` trigger that fills the tenant column from `current_setting('tenant.<dimension>')` when it is `NULL`.** Generate it from `makeguitarmigrations` for every table whose coverage has a local tenant column and whose manager autofills.

**Keep the `pre_save` receiver, demoted to diagnostics.** Correctness moves to the trigger and `WITH CHECK`; the receiver's job becomes producing the good error message and honouring `GUITARS_TENANT_ENFORCE = 'audit'`. Losing it to `DisableSignals` then costs a nice message, not a guarantee.

**Generate one trigger function per distinct (column, GUC) pair**, hosted in `TRIGGER_FUNCTION_APP` beside the existing singleton functions — not one generic function for all tables, and not one per table.

## Why

The trigger covers what the signal cannot — verified against PostgreSQL 18, with the scope opened through `tenant()` so the real execute wrapper publishes it: `instance.save()`/`create()` and `bulk_create()` already work via `pre_save` or the queryset override, but multi-row `INSERT`, `INSERT … SELECT`, and raw SQL outside the ORM reach none of that and only the trigger covers them.

The ordering works, and it had to be checked (measured on PostgreSQL 18; `tests/test_tenancy_rls.py` asserts it directly, and CI runs that on 14 as well): `WITH CHECK` and `NOT NULL` are both evaluated on the row the `BEFORE` trigger returns, not the one the statement supplied — an `INSERT` omitting the tenant column succeeds, and the stored value is the scoped tenant. Had it been the other way round the design would be dead on arrival, since the pre-trigger row's `NULL` satisfies neither. It does not weaken `WITH CHECK`: the trigger only fills a `NULL`; an explicit cross-tenant value is still refused (measured — scope active + column omitted: filled; scope active + explicit cross-tenant value: refused; no scope: refused, stays `NULL`; scope names several tenants: refused, declines to guess; `tenancy_bypassed()` + column omitted: `NOT NULL` violation; `tenancy_bypassed()` + explicit value: allowed, the deliberate cross-tenant path).

**One function per (column, GUC) pair, because the generic form is measurably slower.** PL/pgSQL cannot write a dynamically-named column on `NEW`; reaching one needs a `to_jsonb`/`jsonb_populate_record` round trip. Server-side, one 20,000-row `INSERT` isolated from round-trip time: no trigger 3.76 µs/row, static `NEW.org_id` 3.87 µs/row (+3%), generic `TG_ARGV`+jsonb round trip 6.05 µs/row (**+61%**). An earlier measurement through `executemany` saw only a 3% gap, dominated by network round-trip and hiding the real ratio — the isolated numbers are the ones to trust.

The static form needs the column name baked in, which sounds like one function per table until you notice `GUITARS_TENANT_FIELD` is a single project-wide setting: every `GuitarModel` subclass shares the same column name, so "one per distinct (column, GUC) pair" is **one function** for a typical project, mirroring how `set_updated_at` is a single shared function rather than a per-table one. Only hand-rolled `tenanted_manager()` models on other columns add more.

**`COPY FROM` needs no consideration.** PostgreSQL refuses it outright on any table with row-level security (`FeatureNotSupported: COPY FROM not supported with row-level security`), so bulk loading into a tenanted table is already impossible — not a gap this trigger has to close.

## Why the signal is not deleted

The earlier framing was that the trigger would let the `pre_save` receiver be removed. That is wrong, for two reasons found while scoping it.

**Audit mode has no database analogue.** `GUITARS_TENANT_ENFORCE = 'audit'` reports a write violation and *proceeds*, so a populated deployment can be told where its offending call sites are without 500-ing them. There is no "report and continue" in a trigger or a policy — SQL can only refuse or permit, and [`docs/tenancy.md`](../tenancy.md) already documents that audit mode does not soften the database. Deleting the receiver would delete audit mode for writes, the feature that makes a rollout onto a populated database survivable.

**The database's error messages are worse, and in one case misleading.** Today's guard names the model, dimension, and fix — *"`Release` write is missing `'label'`. Pass it explicitly, or enable `GUITARS_TENANT_AUTOFILL`"*. After the change, the same mistakes surface as `null value in column "org_id" violates not-null constraint` or the generic policy rejection. The multi-tenant-scope case is the worst: the row is refused with *"the row does not belong to the active tenant, or no tenant scope is active"*, when in fact a scope **is** active and the real problem is that it names several tenants so autofill cannot pick one. The Python guard says exactly that.

So the receiver earns its place as a diagnostic layer. What changes is that it stops being the thing correctness depends on — which is the whole point.

## Consequences

- **`DisableSignals` stops being able to disable tenancy enforcement.** It will still suppress the friendly error; the trigger and `WITH CHECK` do not notice it. This is the finding that motivated the ADR. Amended in 2.1.1: this held only where a trigger was actually emitted — an ancestor-owned column got none until [ADR 0009](0009-relocated-owner-table-autofill.md) relocated it there.
- **`bulk_create`'s queryset override becomes belt-and-braces**, kept for the error message rather than correctness — `_untenanted_queryset_class.bulk_create`'s `_guarded()` call keeps its comment but loses its load-bearing status.
- **New SQL templates, private and *not* frozen** (`_CREATE_TENANT_AUTOFILL_*`). Amended during implementation: this ADR originally said to freeze them. The frozen-name rule exists for migrations generated *before* 1.1.0, which resolve `guitars.sql` names at `migrate` time — none can reference a name that did not exist when they were written, so freezing a new one buys nothing and bans a rename forever. 2.0.0 had already set the precedent with `_CREATE_PARENT_UPDATED_AT_TRIGGER`. Private → public stays available later; the reverse does not.
- **A migration per tenanted table**, plus one function migration. Existing databases need `makemigrations` + `migrate`; nothing is rewritten.
- **`autofill=False` becomes visible in the schema.** A model that opts out (an append-only archive) gets no trigger, so the opt-out is auditable in `pg_trigger` instead of living only in a manager argument. Amended in 2.1.1: true only once a trigger could be *removed* — through 2.1.0 flipping the flag emitted nothing and the database kept filling. See [issue #27](https://github.com/Behnam-RK/django-guitars/issues/27) and `docs/migrations.md`'s "Retirement".
- **`audittenancy` should learn to check it.** A tenanted table whose manager autofills but whose trigger is missing is exactly the "looks fine, is not" state the command exists to catch, invisible to every current check. Shipped in 2.1.0; 2.1.1 added the other direction, a trigger the models no longer expect.
- **Multi-hop dimensions still cannot be autofilled**, by the same reasoning that excludes them from policies — no column on this table to write. Unchanged from today, where `tenanted_manager()` rejects `autofill` with a multi-hop lookup.

## Related

- [Issue #24](https://github.com/Behnam-RK/django-guitars/issues/24) — the implementation, shipped in 2.1.0.
- [ADR 0002](0002-force-rls-by-default.md) (FORCE RLS by default) · [ADR 0004](0004-unscoped-base-manager.md) (`base_manager_name` left unset — the same "which layer owns this?" question, answered the other way)
- [docs/tenancy.md](../tenancy.md), [docs/migrations.md](../migrations.md)
