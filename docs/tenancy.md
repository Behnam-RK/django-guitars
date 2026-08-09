# Multi-tenancy

Two cooperating layers, neither redundant.

- **Python** fails **loudly**. A model with a `TenantedManager` refuses to read
  without an active scope, and refuses a write that would land in another
  tenant. This is the layer that tells a developer they got it wrong.
- **PostgreSQL** is the layer that is actually **complete**. The same scope is
  published as session settings, and row-level-security policies enforce it on
  every statement — joins, cascades, `_base_manager`, `instance.save()` and raw
  SQL, none of which ever consult a Django manager.

Without the database there are holes. Without Python a missing scope is silent.

## Setup

```python
# settings.py
INSTALLED_APPS = ["guitars", ...]        # ahead of anything else defining `migrate`
LOCAL_APPS = ["accounts", "billing"]     # apps makeguitarmigrations scans

GUITARS_TENANT_MODEL = "accounts.Organization"
GUITARS_TENANT_FIELD = "org"             # optional, defaults to "tenant"
```

```python
from django.db import models

from guitars.models import GuitarModel


class Invoice(GuitarModel):
    amount = models.IntegerField()
```

`Invoice` now has a non-null `org` FK (`CASCADE`, `editable=False`), three
tenant-scoped managers, and — after `makemigrations` and `migrate` — a
`tenant_scope` policy on its table.

Not every model has to move. `SetarModel` is `GuitarModel` without tenancy, and
the two coexist: an untenanted model never starts demanding a scope.

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

`tenancy_bypassed()` is the *only* way across tenants — there is no unscoped
manager and no `across_tenants()` shortcut, so every cross-tenant access in a
codebase is found by grepping for that one name. It bypasses **both** layers.

Scopes nest, and an inner `tenant(...)` re-enforces inside a bypass:

```python
with tenancy_bypassed():
    with tenant(org=acme):
        Invoice.objects.count()          # acme only
```

A scope may name several tenants, which reads as "either of these":

```python
with tenant(org=[acme, initech]):
    Invoice.objects.count()
```

`None` means **absent**, not "match everything" — `tenant(org=None)` denies. A
deliberate unfiltered read has to say so with `tenancy_bypassed()`.

> **On naming.** With the default field name the call reads `tenant(tenant=acme)`,
> which is awkward. Set `GUITARS_TENANT_FIELD` to whatever your tenant actually is
> — `org`, `shop`, `workspace` — and it reads as prose. There is deliberately **no**
> `scope()` alias: two names for one thing would cost the property that makes
> `tenancy_bypassed()` useful, which is that one grep finds every use.

### Entrypoints

Queue handlers, webhook receivers and gRPC servicers usually resolve the tenant
from their payload. `@tenanted` opens the scope from an argument instead of
repeating the `with` in every body:

```python
from guitars.tenancy import tenanted


@tenanted                                    # reads the `tenant` parameter
def handle_uninstalled(tenant, payload): ...


@tenanted(arg="org")                         # parameter and dimension agree
async def refresh_token(org): ...


@tenanted(arg="target_org", dimension="org")  # they do not
def migrate_to(target_org): ...
```

`arg` is which *parameter* to read; `dimension` is which *scope dimension* to
open, defaulting to `arg`. They are separable because the dimension has to match
what the model's manager was declared with, while the parameter name belongs to
the function's own signature. A tenant bound to `None` raises before the body
runs. Generator functions are rejected at decoration time — their body runs at
iteration, after the scope would have closed.

## Exceptions

Every failure this package raises deliberately is a `guitars.GuitarsError`
subclass, from `guitars.tenancy`:

```
GuitarsError
├── TenantScopeError            # base for every tenant-scope failure
│   ├── TenantScopeMissing      # no scope satisfies the operation
│   └── TenantScopeViolation    # an active scope's write disagrees with it
└── TenantValueError            # a value cannot be safely published at all
```

| Class | When | Typical handling |
| --- | --- | --- |
| `TenantScopeMissing` | A read or write needed `tenant(...)` active — wholly absent, or missing the one dimension this operation requires — and found none. | A 403 in application code: the caller forgot to open a scope. |
| `TenantScopeViolation` | A scope *is* active, but the write disagrees with it — an explicit value that does not match, an ambiguous multi-value scope with nothing to autofill, or PostgreSQL's own row-level-security policy rejecting the statement outright. | An alerting signal: something in the application computed the wrong tenant. |
| `TenantValueError` | A dimension's value (its pk, typically) contains the GUC separator (`,`) and cannot be safely published. | A data-modeling bug — fix the value, this is not routine. |

`TenantScopeError` is still raised nowhere directly — every raise site picks
one of the two subclasses — but it stays the shared base so `except
TenantScopeError` keeps working for any scope failure, missing or violated
alike. `TenantValueError` is deliberately **not** a `TenantScopeError`: it is
not about scope at all, so a handler written for scope failures should not
silently swallow it too.

```python
from guitars.tenancy import TenantScopeMissing, TenantScopeViolation

try:
    with tenant(org=acme):
        Invoice.objects.create(amount=100, org=initech)
except TenantScopeViolation:
    ...  # alert: the application computed the wrong tenant
except TenantScopeMissing:
    ...  # 403: the caller forgot to open a scope
```

> **Migrating from 1.x.** Every 1.x raise site used the single `TenantScopeError`
> class. A consumer catching it by name for a specific failure needs to catch the
> right subclass instead — see the mapping in the 2.0.0 changelog entry. Catching
> the base class still works for every case except the separator one, which was
> already the odd one out and is now `TenantValueError`.

## Managers

All three managers are scoped, `_archives` and `_all_objects` included: they
exist to see rows `objects` hides, so an unscoped one would be the widest leak
in the kit.

```python
Invoice.objects        # live rows, this tenant
Invoice._archives      # soft-deleted rows, this tenant
Invoice._all_objects   # everything, this tenant
```

Without a scope, every one of them raises `TenantScopeMissing` — on reads, on
set-wide writes (`update`, `delete`, `bulk_update`), and on `hard_delete()`.

### Scoping a model without the GuitarModel rung

`TenantedManager` composes onto any manager:

```python
from guitars.models import LiveManager, SetarModel
from guitars.tenancy import TenantedManager


class Booking(SetarModel):
    org = models.ForeignKey("accounts.Organization", on_delete=models.CASCADE)

    objects = TenantedManager(_manager_class=LiveManager, org="org")
```

Declaring the manager *is* the opt-in — there is no registry to keep in step, and
`makeguitarmigrations` will emit a policy for it like any other.

Two things to know if you do this by hand:

- Only the managers you wrap are scoped. `Booking._all_objects` above is not, so
  it answers unscoped in Python. (The policy still applies, so what comes back is
  still only what the caller may see — but the Python layer will not tell you.)
  Avoiding that asymmetry is most of what the `GuitarModel` rung is for.
- Autofill follows `GUITARS_TENANT_AUTOFILL` (default `False`), so an unscoped
  write is refused rather than guessed at. `GuitarModel` passes `autofill=True`
  for the field it owns, because that field is framework-owned and
  `editable=False` — naming it at every call site would be ceremony.

Multi-hop dimensions work in Python:

```python
objects = TenantedManager(_manager_class=LiveManager, org="release__org")
```

…but **cannot** be covered by a policy: there is no column on this table to
predicate on. `makeguitarmigrations` and `audittenancy` both say so on every run.
Raw SQL against such a table is not scoped.

## What the database enforces

`makemigrations` writes a `tenant_scope` policy per tenanted table. The
predicate is:

```sql
(SELECT current_setting('tenant.bypass', true)) = 'on'
OR (<column>::text = ANY(string_to_array(
      (SELECT current_setting('tenant.org', true)), ',')))
```

Three things about that shape are deliberate:

- **Every NULL path denies.** An unset session setting yields NULL, and
  `ANY(NULL)` is NULL, which is not true. That is what makes the database half
  fail closed.
- **Membership, not equality.** The value is always encoded as a separated list,
  even a single one, so one policy form serves a scalar scope and a collection
  scope alike.
- **`current_setting` sits in a scalar subquery**, so the planner hoists it to an
  InitPlan evaluated once per statement rather than once per candidate row.

`USING` covers reads and `WITH CHECK` covers writes, so an `UPDATE` cannot move a
row into another tenant even though no Python guard sees `queryset.update()`.

### The three silent bypasses

RLS has three, and each returns rows unfiltered with no error and no log line:

1. **`SUPERUSER`** bypasses policies unconditionally, even `FORCE`d ones.
2. **The `BYPASSRLS` role attribute** does the same, and is granted separately —
   ruling out one does not rule out the other.
3. **A table's owner** bypasses RLS unless the table is `FORCE`d. Your
   application role owns its tables, because it runs the migrations.

The third is why `GUITARS_RLS_FORCE` defaults to `True`. Do not run your
application as a superuser or with `BYPASSRLS`.

Every catalog-based check is blind to the first two — `pg_class` and `pg_policy`
report the same "enforced" whoever is asking — so `audittenancy` reads the
connecting role's attributes as well, and says plainly that its other findings
prove nothing when that role bypasses. It warns rather than fails, because this
describes *who connected*, not the database: a pipeline may legitimately audit
as an administrative role while the application does not.

### Multi-table inheritance

An MTI child gets its **own** policy, correlated to the ancestor that holds the
tenant column by the shared primary key:

```sql
EXISTS (SELECT 1 FROM parent AS o
        WHERE o.id = child.parent_ptr_id
          AND o.org_id::text = ANY(...))
```

It does *not* rely on the ancestor's policy. "Every query joins the parent" is
false, and the kit already knew that — it is why `set_parent_updated_at` exists.
A child-only statement — `queryset.update()` on child-local fields, a `DELETE`
against the child table, `.values()` of child-only columns — never touches the
ancestor, so an ancestor-only policy never applies to it. See
[ADR 0003](adr/0003-mti-owner-join-policy.md).

Dimensions spread across **two** different ancestors are reported rather than
covered: one correlated subquery reaches one ancestor, and which one it reached
would come down to field declaration order.

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `GUITARS_TENANT_MODEL` | *(required for `GuitarModel`)* | Tenant model, `"app.Model"`. Missing it is a `guitars.tenancy.E003` check error. |
| `GUITARS_TENANT_FIELD` | `"tenant"` | Name of the FK, and of the scope dimension. |
| `GUITARS_TENANT_ENFORCE` | `"strict"` | `"audit"` reports a write violation and proceeds. **Read the rollout section below.** |
| `GUITARS_TENANT_AUTOFILL` | `False` | Fill a missing tenant from the active scope. `GuitarModel` passes `True` for its own FK regardless. |
| `GUITARS_TENANT_POLICIES` | `True` | `False` keeps the Python layer and leaves the database alone. |
| `GUITARS_RLS_FORCE` | `True` | `False` ships policies inert, for a staged retrofit. |
| `GUITARS_RLS_EXEMPT_ROLES` | `[]` | Roles granted a `SELECT`-only exemption policy, guarded on the role existing. |

## Rolling it out onto a populated database

The order matters, and one step is easy to get wrong.

**1. Adopt the Python layer, in audit mode, with the policies off.**

```python
GUITARS_TENANT_ENFORCE = "audit"
GUITARS_TENANT_POLICIES = False
```

Every unscoped read still raises — a read has rows to return, and returning every
tenant's would be the leak itself. But a *write* that crosses tenants is reported
once per distinct finding and allowed through, so you get a list of offending call
sites instead of a wall of 500s. Point it at your error tracker:

```python
from guitars.tenancy import set_reporter

set_reporter(lambda message, **context: sentry_sdk.capture_message(message, extras=context))
```

**2. Fix the call sites. Then switch to `strict`.**

> ⚠️ **Audit mode does not soften the database.** There is no session variable
> that makes a policy lenient, so once the policies bind, a cross-tenant write is
> reported *and* rejected. A team that leaves `audit` on expecting no 500s gets
> them from one layer lower. `audittenancy` warns when it sees this combination.

**3. Ship the policies.** If the database is large or you want a soak, ship them
inert first:

```python
GUITARS_TENANT_POLICIES = True
GUITARS_RLS_FORCE = False       # policies exist but the owning role bypasses them
```

Then, once you are satisfied, land `FORCE` in its own migration:

```bash
python manage.py makeguitarmigrations --force-rls
```

That stage only touches tables whose policies actually shipped inert, so running
it on a fully-forced database correctly does nothing.

**4. Gate on it.** In your deploy pipeline:

```bash
python manage.py audittenancy --require-force --require-match
```

## Auditing

`makeguitarmigrations --check` is a *build* gate: it proves the migrations exist.
It cannot prove they ran, that nobody dropped a policy by hand, or that
enforcement binds. `audittenancy` asks the database:

```bash
python manage.py audittenancy                    # warn on findings
python manage.py audittenancy --require-force    # fail on a table the owner bypasses
python manage.py audittenancy --require-match    # fail on a policy the models disagree with
python manage.py audittenancy billing            # scope to an app
```

It catches, in descending order of danger:

- **A connecting role that bypasses RLS outright** — `SUPERUSER` or `BYPASSRLS`.
  Warned, never fatal: it describes the connection rather than the database.
- **`ENABLE` without `FORCE`** — the table looks protected in `pg_policies` and
  constrains nothing.
- **A missing policy or missing `ENABLE`** — a migration that never ran, or drift.
- **A policy that no longer says what the models say** — the table has a healthy
  `tenant_scope` policy that scopes on the *wrong* dimensions, on a renamed
  column, or on nothing at all in its `WITH CHECK` half. Every existence check
  passes while each statement is filtered by a weaker predicate than the Python
  layer believes. The usual cause is a replacement migration that was generated
  but never applied. Fatal only under `--require-match`, since a run that
  precedes the deploy's own `migrate` is legitimately in this state.
- **Unexpected coverage** — a policy on a table the models no longer consider
  tenanted. Harmless to reads, but the database and the models disagree.

The third is compared by the facts a *stored* policy preserves, not by its text:
PostgreSQL rewrites a policy expression when it saves it (casts made explicit,
columns parenthesised), so the text it hands back never equals what was emitted,
however correct the policy is. What survives intact is the set of `tenant.*`
settings the predicate reads, and — from `pg_depend`, which records a real
dependency per column a policy touches — the set of columns it references.

The settings are read from **both halves of the policy separately**. `USING`
governs reads and `WITH CHECK` governs writes, they are independently editable,
and a policy left as `USING (<tenant match>) WITH CHECK (true)` reads as fully
scoped while accepting every cross-tenant write. Nothing else notices it: the
`USING` half's settings are exactly right, and `true` records no `pg_depend`
rows, so the column set is exactly right too. A `WITH CHECK` that PostgreSQL
stores as `NULL` — what a `FOR ALL` policy written without one gets — is read as
the `USING` expression, which is PostgreSQL's own rule.

Both commands share one definition of what coverage *should* be
(`guitars.tenancy.discovery`), so the build gate and the live audit cannot
quietly disagree.

## Migrations run bypassed

`migrate` is overridden to run inside `tenancy_bypassed()`. Without it a
`RunPython` backfill matches no rows and **silently does nothing**, then gets
marked applied — which surfaces much later as missing data with a green migration
history pointing away from the cause.

This is why `guitars` must appear in `INSTALLED_APPS` **before** anything else
that defines a `migrate` command: Django's `get_commands()` walks
`reversed(get_app_configs())` and lets each app overwrite the previous entry, so
the app appearing earliest wins.

That ordering is checked rather than merely documented. `guitars.tenancy.W001`
resolves the `migrate` that would actually run and warns if it is not this one —
a warning, not an error, because a project with no data migrations is unaffected,
and by `isinstance` rather than by app name, so subclassing the override to add
your own behaviour is silent. It stays quiet when nothing is tenanted or when
`GUITARS_TENANT_POLICIES = False` leaves the database layer switched off.

## Performance notes

- **The tenant FK is `CASCADE`**, so if your tenant model is itself
  soft-deletable, `makeguitarmigrations` writes one cascade soft-delete rule onto
  the tenant table *per tenanted model*. Soft-deleting one tenant then fires N
  `UPDATE`s in a single statement. Correct, and cheap next to orphaned rows — but
  it scales with the number of tenanted models.
- **Deleting a tenant needs a scope.** `Label.objects` is not tenanted, so
  `tenant.delete()` runs unscoped — and the cascade `UPDATE` into the tenanted
  tables is then filtered to nothing by their policies. The tenant is archived and
  its rows are not. Fail-closed rather than wrong, but do it as
  `with tenant(org=doomed): doomed.delete()`.
- **Publishing is lazy.** The scope reaches the database in a
  `connection.execute_wrappers` entry, immediately before a statement runs, and
  only when the desired state differs from what that connection was last told. A
  `tenant()` block that never queries costs nothing; nested blocks cost nothing;
  a tenant switch costs one extra round trip.
- **Index the tenant column.** `GuitarModel`'s FK is indexed by Django's default,
  which is what keeps the policy predicate cheap.

## Related

- [ADR 0001 — a swappable tenant model on the GuitarModel rung](adr/0001-swappable-tenant-model.md)
- [ADR 0002 — FORCE ROW LEVEL SECURITY by default](adr/0002-force-rls-by-default.md)
- [ADR 0003 — an owner-join policy for MTI children](adr/0003-mti-owner-join-policy.md)
- [ADR 0004 — leaving base_manager_name unset](adr/0004-unscoped-base-manager.md)
- [Soft deletion](soft-deletion.md) · [Migrations](migrations.md) · [MTI](mti.md)
