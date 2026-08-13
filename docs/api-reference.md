# API reference

A flat enumeration of the public surface: base models, managers, settings,
commands, and the frozen `guitars.sql` names. For the *why* behind any of
this, see [`docs/adr/`](adr/README.md); for task-oriented guides, see
[`soft-deletion.md`](soft-deletion.md), [`tenancy.md`](tenancy.md),
[`mti.md`](mti.md), and [`migrations.md`](migrations.md).

## Model bases (`guitars.models`)

The instrument ladder, each rung adding capability:

| Base | Adds |
| --- | --- |
| `TarModel` | `.update()` / `.aupdate()`, cached-property invalidation on `refresh_from_db()`. No columns, no DB behaviour. |
| `DutarModel` | DB-managed `_created_at` / `_updated_at`, `app_label()` / `model_name()` / `class_name()` helpers, field-listing `__repr__`. |
| `SetarModel` | PostgreSQL-enforced soft deletion. The default rung for a non-tenanted model. |
| `GuitarModel` | Multi-tenancy: a tenant FK, tenant-scoped managers, an RLS policy. The full kit. |

Each capability is also a standalone mixin: `UpdatableModel`,
`HasCachedPropertyModel`, `DatedModel`, `SoftDeletableModel`.

## Managers and querysets (`guitars.models`)

| Name | Purpose |
| --- | --- |
| `LiveManager` | Default manager on a `SoftDeletableModel` — live rows only. |
| `ArchiveManager` | `_archives` — soft-deleted rows only. |
| `AllObjectsManager` | `_all_objects` — every row regardless of `_deleted_at`. |
| `LiveQuerySet` | Queryset backing `LiveManager`. |
| `HardDeletableQuerySet` | `LiveQuerySet` subclass adding `.hard_delete()` in bulk; backs `ArchiveManager` / `AllObjectsManager`. |

## Tenancy public API (`guitars.tenancy`)

`guitars.tenancy.__all__` (16 names) is the application-facing surface only —
GUC constants live in `guitars.gucs`, generator-facing spec helpers in
`guitars.tenancy.spec`, and test-only install/uninstall hooks in
`guitars.tenancy.testing`.

| Name | Kind | Purpose |
| --- | --- | --- |
| `tenant(**dimensions)` | context manager | Open a tenant scope for the block. |
| `tenancy_bypassed()` | context manager | The one explicit cross-tenant escape hatch. |
| `tenanted` | decorator | Run the wrapped callable inside a scope opened from one of its own arguments (`@tenanted(arg=…, dimension=…)`). Raises `TenantScopeMissing` if that argument is `None`. |
| `tenanted_manager(...)` | factory | Build a tenant-scoped manager (renamed from `TenantedManager` in 2.0 — see `CHANGELOG.md`'s `[2.0.0]` entry). |
| `TenantedManagerBase` | class | Marker base; `isinstance(Model.objects, TenantedManagerBase)` recognises a tenant-scoped manager. |
| `install()` | function | Activate enforcement. Idempotent; called by `GuitarsConfig.ready()` and by `tenanted_manager()`. |
| `get_tenant(dimension)` | function | Read the active scope's value for one dimension. |
| `is_bypassed()` | function | Whether the current context is inside `tenancy_bypassed()`. |
| `set_reporter(reporter)` | function | Install a custom `Reporter` for audit-mode findings. |
| `Reporter` | protocol/class | Receives structured violation reports (`kind`, `model`, `dimension`). |
| `TenantEnforcement` | enum | `STRICT` / `AUDIT` — `GUITARS_TENANT_ENFORCE`'s values. |
| `ViolationKind` | enum | `UNSCOPED`, `MISSING`, `AMBIGUOUS`, `MISMATCH` — passed to `Reporter`. |
| `GuitarsError` (`guitars.GuitarsError`) | exception | Package-level base for everything guitars raises deliberately. |
| `TenantScopeError` | exception | Base for tenant-scope failures; never raised directly. |
| `TenantScopeMissing` | exception | No scope satisfies the operation. |
| `TenantScopeViolation` | exception | An active scope's write disagrees with it, or Postgres RLS rejected the statement. |
| `TenantValueError` | exception | A tenant value cannot be safely published (e.g. contains the GUC separator). Not a `TenantScopeError` subclass. |

Not application-facing, but reachable directly if needed:
`guitars.gucs` (`BYPASS_GUC`, `GUC_PREFIX`, `VALUE_SEPARATOR`, `guc_name`),
`guitars.tenancy.spec` (`tenant_spec`, `local_tenant_fields`),
`guitars.tenancy.testing` (test setup/teardown hooks).

## Settings

All eight `GUITARS_*` settings, plus two related, non-`GUITARS_`-prefixed
settings the enforcement generator reads:

| Setting | Default | Effect |
| --- | --- | --- |
| `GUITARS_TENANT_MODEL` | *(required for `GuitarModel`)* | Tenant model, `"app.Model"`. |
| `GUITARS_TENANT_FIELD` | `"tenant"` | Name of the FK, and of the scope dimension. |
| `GUITARS_TENANT_ENFORCE` | `"strict"` | `"audit"` reports a write violation once per distinct finding and proceeds. |
| `GUITARS_TENANT_AUTOFILL` | `False` | Fill a missing tenant from the active scope. `GuitarModel` passes `True` for its own FK regardless. |
| `GUITARS_TENANT_POLICIES` | `True` | `False` keeps the Python layer and leaves the database alone. |
| `GUITARS_RLS_FORCE` | `True` | `False` ships policies inert, for a staged retrofit; `makeguitarmigrations --force-rls` lands `FORCE` later. |
| `GUITARS_RLS_EXEMPT_ROLES` | `[]` | Roles granted a `SELECT`-only exemption policy, guarded on the role existing. |
| `GUITARS_AUTO_MAKE_MIGRATIONS` | `True` | `False` disables the `makemigrations` override's enforcement step; use the standalone `makeguitarmigrations` instead. |
| `LOCAL_APPS` | *(required)* | Not `GUITARS_`-prefixed. First-party apps the generator scans for `_updated_at` / `_deleted_at`, matched against each `AppConfig.name` — so entries are **dotted module paths** (`"myproject.blog"`), not short labels. A short label for an app whose `name` is dotted matches nothing, and the scan is then silently empty. |
| `TRIGGER_FUNCTION_APP` | `LOCAL_APPS[0]` | Not `GUITARS_`-prefixed. Which app hosts the shared trigger-function migration. |

## Management commands

**`makeguitarmigrations [app_label ...]`**
| Flag | Effect |
| --- | --- |
| `--check` | Exit non-zero if enforcement migrations are missing; writes nothing. |
| `--adopt` | Re-emit every operation in a form correct whether or not the database object already exists (for a database whose objects weren't created by this command). |
| `--force-rls` | Generate `FORCE ROW LEVEL SECURITY` migrations for tables whose policies already exist. Only needed when `GUITARS_RLS_FORCE = False`. |

**`audittenancy [app_label ...]`**
| Flag | Effect |
| --- | --- |
| `--database ALIAS` | Database alias to audit (default `"default"`). |
| `--require-force` | Fail on a table without `FORCE ROW LEVEL SECURITY`. |
| `--require-match` | Fail on a policy whose predicate no longer matches the models. |

**`makemigrations [app_label ...]`** — Django's own command, overridden to also
run `makeguitarmigrations` after the schema migrations (unless
`GUITARS_AUTO_MAKE_MIGRATIONS = False`, or `--empty`/`--dry-run` is passed).
`--check` validates both layers. Positional app labels scope the enforcement
step the same way; `--force-rls` is deliberately **not** forwarded — it's a
staged-retrofit step run by hand.

## Frozen `guitars.sql` names

Migrations generated **before 1.1.0** import these by name (`from guitars
import sql`); migrations generated from 1.1.0 on inline their SQL and
reference nothing (see [ADR 0006](adr/0006-inline-generated-migration-sql.md)).
Either way, these names must never be renamed — `tests/test_sql_interface.py`
guards this list.

Two different obligations share one list. A name that could appear in a
pre-1.1.0 migration is load-bearing for `migrate` on a fresh database in a
consuming project. A name added *after* inlining — the `REPLACE_*` / `ADOPT_*`
forms and everything row-level-security — is referenced by no migration
anywhere; it is recorded here so that a later rename is still caught rather
than shipped silently. Neither may be renamed; only the blast radius differs.

**Constants:**
`CREATE_UPDATED_AT_TRIGGER_FUNCTION`, `DROP_UPDATED_AT_TRIGGER_FUNCTION`,
`CREATE_UPDATED_AT_TRIGGER`, `DROP_UPDATED_AT_TRIGGER`,
`REPLACE_UPDATED_AT_TRIGGER_FUNCTION`, `REPLACE_UPDATED_AT_TRIGGER`,
`ADOPT_UPDATED_AT_TRIGGER`, `SWITCH_ON_HARD_DELETION`,
`SWITCH_OFF_HARD_DELETION`, `CREATE_SOFT_DELETE_RULE`,
`DROP_SOFT_DELETE_RULE`, `CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE`,
`DROP_SOFT_DELETE_RELATED_OBJECTS_RULE`,
`CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA`,
`DROP_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA`,
`CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION`,
`DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION`,
`CREATE_PARENT_UPDATED_AT_TRIGGER`, `DROP_PARENT_UPDATED_AT_TRIGGER`,
`REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION`,
`REPLACE_PARENT_UPDATED_AT_TRIGGER`, `ADOPT_PARENT_UPDATED_AT_TRIGGER`,
`CREATE_MTI_SOFT_DELETE_RULE`, `DROP_MTI_SOFT_DELETE_RULE`,
`TENANT_POLICY`, `EXEMPT_POLICY_PREFIX`.

**Callables:**
`create_tenant_policy`, `drop_tenant_policy`, `create_exempt_policy`,
`drop_exempt_policy`, `drop_all_exempt_policies`, `enable_rls`, `disable_rls`,
`force_rls`, `no_force_rls`, `create_table_rls`, `replace_table_rls`,
`drop_table_rls`.
