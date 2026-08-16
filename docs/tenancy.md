# Multi-tenancy

Two cooperating layers, neither redundant. **Python** fails **loudly**: a `tenanted_manager()`-built manager refuses to read without an active scope, and refuses a write that would land in another tenant. **PostgreSQL** is **complete**: the same scope is published as session settings, and RLS enforces it on every statement — joins, cascades, `_base_manager`, `instance.save()`, raw SQL — none of which consult a Django manager. Without the database there are holes; without Python a missing scope is silent.

## Setup

```python
# settings.py
INSTALLED_APPS = ["guitars", ...]        # ahead of anything else defining `migrate`
LOCAL_APPS = ["accounts", "billing"]     # apps makeguitarmigrations scans
GUITARS_TENANT_MODEL = "accounts.Organization"
GUITARS_TENANT_FIELD = "org"             # optional, defaults to "tenant"

# models.py
class Invoice(GuitarModel):
    amount = models.IntegerField()
```

`Invoice` now has a non-null `org` FK (`CASCADE`, `editable=False`), three tenant-scoped managers, and — after `makemigrations` and `migrate` — a `tenant_scope` policy on its table. Not every model has to move: `SetarModel` is `GuitarModel` without tenancy, and the two coexist — an untenanted model never starts demanding a scope.

## Using it

```python
from guitars.tenancy import tenancy_bypassed, tenant

with tenant(org=acme):
    Invoice.objects.all()                # acme's invoices
    Invoice.objects.create(amount=100)   # org filled in from the scope

Invoice.objects.all()                    # TenantScopeMissing

with tenancy_bypassed():                 # the one explicit cross-tenant path
    Invoice.objects.count()              # every tenant
```

`tenancy_bypassed()` is the *only* way across tenants — no unscoped manager, no `across_tenants()` shortcut, so every cross-tenant access is found by grepping for that one name. It bypasses **both** layers. Scopes nest, and an inner `tenant(...)` re-enforces inside a bypass. A scope may name several tenants (`tenant(org=[acme, initech])`, "either of these"). `None` means **absent**, not "match everything" — `tenant(org=None)` denies; an unfiltered read must say so with `tenancy_bypassed()`. There is deliberately **no** `scope()` alias for `tenant()` under a friendlier name: two names for one thing would cost the grep-ability that makes `tenancy_bypassed()` useful.

**Entrypoints.** Queue handlers, webhook receivers and gRPC servicers usually resolve the tenant from their payload. `@tenanted` opens the scope from an argument instead of repeating the `with` in every body — `@tenanted` reads the `tenant` parameter directly; `@tenanted(arg="org")` reads `org`; `@tenanted(arg="target_org", dimension="org")` reads `target_org` but opens the `org` dimension, for when the two disagree. `arg` is which *parameter* to read; `dimension` is which *scope dimension* to open, defaulting to `arg` — separable because the dimension must match the model's manager, while the parameter belongs to the function's own signature. A tenant bound to `None` raises before the body runs; generator functions are rejected at decoration time, since their body runs at iteration, after the scope would have closed.

## Exceptions

Every failure this package raises deliberately is a `guitars.GuitarsError` subclass, from `guitars.tenancy`: `TenantScopeError` (base) splits into `TenantScopeMissing` (no scope satisfies the operation — a 403, the caller forgot to open one) and `TenantScopeViolation` (an active scope's write disagrees with it — an alert, something computed the wrong tenant); `TenantValueError` (a dimension's value contains the GUC separator `,` and can't be safely published — a data-modeling bug) is deliberately **not** a `TenantScopeError`, so a handler for scope failures doesn't silently swallow it too. `TenantScopeError` itself is never raised directly, but stays the shared base so `except TenantScopeError` keeps working for either case. Migrating from 1.x: every raise site there used the single `TenantScopeError` class — catch the right subclass instead (2.0.0 changelog has the mapping); the base class still works except for the separator case, now `TenantValueError`.

## Managers

All three managers are scoped, `_archives` and `_all_objects` included — they exist to see rows `objects` hides, so an unscoped one would be the widest leak in the kit. Without a scope, every one raises `TenantScopeMissing`, on reads, set-wide writes, and `hard_delete()`. `tenanted_manager()` composes onto any manager without the `GuitarModel` rung: `objects = tenanted_manager(_manager_class=LiveManager, org="org")` on a `SetarModel` with an `org` FK. Declaring the manager *is* the opt-in — no registry, and `makeguitarmigrations` emits a policy for it like any other. Two things by hand: only the managers you wrap are scoped (an unwrapped `_all_objects` answers unscoped in Python though the policy still applies — avoiding that asymmetry is most of what the `GuitarModel` rung is for); and autofill follows `GUITARS_TENANT_AUTOFILL` (default `False`), while `GuitarModel` passes `autofill=True` for its own framework-owned, `editable=False` field — a manager that autofills gets a `BEFORE INSERT` trigger, so the opt-out is visible in `pg_trigger` rather than only in the manager argument. Multi-hop dimensions (`org="release__org"`) work in Python but **cannot** be covered by a policy — no column on this table to predicate on; `makeguitarmigrations`/`audittenancy` say so on every run, and raw SQL against such a table is not scoped.

## What the database enforces

`makemigrations` writes two things per tenanted table. **Autofill is a `BEFORE INSERT … FOR EACH ROW` trigger** ([ADR 0005](adr/0005-trigger-based-tenant-autofill.md)) filling a `NULL` tenant column from `current_setting('tenant.<dimension>')`, so `bulk_create`, multi-row `INSERT`, `INSERT … SELECT` and raw SQL are covered — none of which reach a `pre_save`. It declines rather than guesses: an explicit value, a bypassed frame, no scope, or a scope naming several tenants all leave the column alone, and `WITH CHECK` or `NOT NULL` then refuses the row. The `pre_save` receiver is kept for **diagnostics** — audit mode has no database analogue, and the guard's message names the model, dimension and fix where the database says only `null value in column "org_id"`. Losing it to `DisableSignals` costs that message, not the guarantee — including for a tenant column owned by an MTI ancestor, whose trigger sits on that ancestor's table ([ADR 0009](adr/0009-relocated-owner-table-autofill.md)); where a shared ancestor's descendants disagree about autofilling it, the generator refuses and says so. Second, a `tenant_scope` policy:
`(SELECT current_setting('tenant.bypass', true)) = 'on' OR (<column>::text = ANY(string_to_array((SELECT current_setting('tenant.org', true)), ',')))`.
Deliberate: every NULL path denies (an unset setting yields NULL, `ANY(NULL)` is NULL — the database half fails closed); membership not equality (the value is always a separated list, so one form serves scalar and collection scopes alike); `current_setting` sits in a scalar subquery so the planner hoists it to an InitPlan evaluated once per statement. `USING` covers reads, `WITH CHECK` covers writes, so an `UPDATE` can't move a row into another tenant even though no Python guard sees `queryset.update()`.

### The three silent bypasses

RLS has three, each returning rows unfiltered with no error or log line: `SUPERUSER` bypasses unconditionally, even `FORCE`d policies; the `BYPASSRLS` role attribute does the same and is granted separately; a table's **owner** bypasses RLS unless `FORCE`d — your application role owns its tables since it runs the migrations, which is why `GUITARS_RLS_FORCE` defaults to `True`. Don't run as superuser or with `BYPASSRLS`. Catalog checks (`pg_class`, `pg_policy`) are blind to the first two, so `audittenancy` reads the connecting role's attributes too, warning (not failing, since this describes *who connected*) that its other findings prove nothing when that role bypasses.

An MTI child gets its **own** owner-join policy rather than relying on the ancestor's — see [MTI](mti.md#what-each-child-table-gets) for the SQL shape and [ADR 0003](adr/0003-mti-owner-join-policy.md) for why. Dimensions spread across **two** different ancestors are reported rather than covered: one correlated subquery reaches one ancestor, and which one it reached would come down to field declaration order.

## Connection pooling

`tenancy/guc.py` publishes the active scope as `tenant.*` PostgreSQL session settings; its cache is proven correct under `CONN_MAX_AGE` and Django's own psycopg pool by `tests/test_concurrency.py`, but has no visibility into an **external, transaction-pooling connection pooler** (e.g. pgbouncer `POOL_MODE: transaction`) in front of that connection. There, a physical backend is handed to a different logical client between transactions without resetting a session-level `SET`, so a leftover `tenant.org = 'acme'` makes the next client's queries run as if they had opened `tenant(org=acme)` themselves — silently, and **fails open**: RLS matches the wrong tenant instead of denying. `TestPgbouncerTransactionPooling` demonstrates both the leak and the fix against a real pgbouncer (opt-in via `docker compose --profile pooling up -d --wait`).

**The fix lives in the pooler's configuration, not in Django or guitars**: set `server_reset_query = DISCARD ALL` in `pgbouncer.ini` — standard guidance independent of guitars, likely already correct for any app pooling through pgbouncer in transaction mode. `guitars.tenancy.W002` (`check_pooling_leaks_tenant_gucs`) warns when `DISABLE_SERVER_SIDE_CURSORS` is on — Django's own signal for this pooling mode — as a nudge, not a verdict: the risk applies to every tenanted deployment behind a transaction-pooling external pooler regardless of that setting.

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `GUITARS_TENANT_MODEL` | *(required)* | Tenant model, `"app.Model"`; missing it is `E003`. |
| `GUITARS_TENANT_FIELD` | `"tenant"` | Name of the FK and scope dimension. |
| `GUITARS_TENANT_ENFORCE` | `"strict"` | `"audit"` reports a violation and proceeds — see rollout below. |
| `GUITARS_TENANT_AUTOFILL` | `False` | Fill a missing tenant from scope. `GuitarModel` passes `True` for its own FK. |
| `GUITARS_TENANT_POLICIES` | `True` | `False` keeps Python-only, database untouched. |
| `GUITARS_RLS_FORCE` | `True` | `False` ships policies inert, for a staged retrofit. |
| `GUITARS_RLS_EXEMPT_ROLES` | `[]` | Roles granted a `SELECT`-only exemption policy. |

## Rolling it out onto a populated database

The order matters, and one step is easy to get wrong. **1.** Adopt the Python layer in audit mode, policies off (`GUITARS_TENANT_ENFORCE = "audit"`, `GUITARS_TENANT_POLICIES = False`) — unscoped reads still raise, but a cross-tenant *write* is reported once per finding via `set_reporter(fn)` (receiving `message, **context` with `context['kind']` a `ViolationKind`) and allowed through, giving a list of offending call sites instead of a wall of 500s. **2.** Fix the call sites, then switch to `strict`. **3.** Ship the policies, inert first if the database is large (`GUITARS_TENANT_POLICIES = True`, `GUITARS_RLS_FORCE = False`), then land `FORCE` in its own migration via `makeguitarmigrations --force-rls` — that stage only touches tables that actually shipped inert. **4.** Gate on `audittenancy --require-force --require-match` in the deploy pipeline.

> ⚠️ **Audit mode does not soften the database.** No session variable makes a policy lenient, so once policies bind, a cross-tenant write is reported *and* rejected — a team that leaves `audit` on expecting no 500s gets them from one layer lower. `audittenancy` warns when it sees this combination.

## Auditing

`makeguitarmigrations --check` is a *build* gate proving migrations exist — it can't prove they ran, that nobody dropped a policy by hand, or that enforcement binds. `audittenancy [app_label]` (`--require-force`, `--require-match`) asks the database instead, catching in descending danger: a connecting role that bypasses RLS outright (warned, never fatal — describes the connection, not the database); `ENABLE` without `FORCE` (looks protected in `pg_policies`, constrains nothing); a missing policy or `ENABLE` (a migration that never ran, or drift); a policy that no longer says what the models say (fatal only under `--require-match`, since a pre-deploy-`migrate` run is legitimately in this state — usually a replacement migration generated but never applied); and unexpected coverage (harmless, but database and models disagree).

The policy-mismatch check compares the facts a *stored* policy preserves, not its text — Postgres rewrites the expression on save, so stored text never equals what was emitted. What survives is the set of `tenant.*` settings the predicate reads and, from `pg_depend`, the columns it references, checked separately for `USING` (reads) and `WITH CHECK` (writes) since they're independently editable — a policy left as `USING (<match>) WITH CHECK (true)` reads as fully scoped while accepting every cross-tenant write, and nothing else notices since `true` records no `pg_depend` rows. Both commands share one definition of expected coverage (`guitars.tenancy.discovery`), so the build gate and the live audit cannot quietly disagree.

Separately, `migrate` itself runs inside `tenancy_bypassed()` — see [Migrations](migrations.md#odds-and-ends) for why, and `guitars.tenancy.W001` for the check that the `INSTALLED_APPS` ordering it depends on actually holds.

## Performance notes

The tenant FK is `CASCADE`, so a soft-deletable tenant model gets one cascade soft-delete rule per tenanted model — one `UPDATE` fan-out per tenant delete, cheap next to orphaned rows but scaling with model count. Deleting a tenant needs a scope (`with tenant(org=doomed): doomed.delete()`) or the cascade `UPDATE` is filtered to nothing by policies, archiving the tenant but not its rows — fail-closed, not wrong. Publishing is lazy: the scope reaches the database immediately before a statement runs, only when it differs from what the connection was last told, so an empty `tenant()` block costs nothing and a switch costs one round trip. The tenant column is indexed by Django's default, keeping the policy predicate cheap.

## Related

- [ADR 0001](adr/0001-swappable-tenant-model.md) (swappable tenant model) · [ADR 0002](adr/0002-force-rls-by-default.md) (FORCE RLS by default)
- [ADR 0003](adr/0003-mti-owner-join-policy.md) (MTI owner-join policy) · [ADR 0004](adr/0004-unscoped-base-manager.md) (unscoped base_manager_name)
- [Soft deletion](soft-deletion.md) · [Migrations](migrations.md) · [MTI](mti.md)
