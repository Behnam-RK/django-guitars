# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.0] - 2026-08-02

M2: new behavioural test families (#9) -- dev/test-only, no production code path changes
behavior in this release. 100% line/branch coverage cannot see behavioural gaps in a
library whose whole premise is PostgreSQL-enforced correctness under conditions Python
doesn't control: concurrency, connection reuse, a database that drifted from what
migrations claim, and consuming projects upgrading across the 1.1.0 SQL-inlining change.

### Added

- `hypothesis`, `syrupy` and `pglast` as dev/test dependencies: property-based fuzzing,
  snapshot testing for generated-migration text, and structural (not textual) parsing of
  generated policy SQL, respectively.
- `tests/test_concurrency.py`: two threads in different tenant scopes, `aupdate()` inside
  an already-running event loop, a connection reused across logical requests via
  `CONN_MAX_AGE`, Django 5.1+'s `OPTIONS={"pool": True}`, and pgbouncer transaction
  pooling -- behind a new opt-in `pooling` compose profile
  (`docker compose --profile pooling up -d --wait`) so default local/CI runs don't pay
  for it.
- `tests/test_drift.py`: a hand-dropped soft-delete rule or `_updated_at` trigger is
  invisible to both `makemigrations --check` (a build-time gate over migration files) and
  `audittenancy` (a runtime gate scoped to tenant RLS only); a hand-dropped tenant policy
  is the one drift `audittenancy` does catch. Also covers `--adopt`'s `DROP ... IF EXISTS`
  form succeeding where the plain generated form fails against an already-present,
  unrecorded object.
- `tests/test_legacy_migrations.py` and `tests/legacy_migrations/`: reproduces an
  already-migrated downstream project on upgrade -- pre-1.1.0-shaped migrations
  (`from guitars import sql`, no `[SQL:...]` identity) that `--check` correctly refuses
  rather than treating as "covered forever" (the bug `2ba86a3` fixed), and the generator's
  replace form applying cleanly against the live, already-populated tables.
- `tests/test_migrate_override.py`: a real `RunPython` migration backfilling across two
  tenants in one statement with no tenant scope open, proving `migrate.py`'s
  `tenancy_bypassed()` wrapper for real rather than only via the `guitars.tenancy.W001`
  system check that it's installed.
- `tests/test_properties.py`: identifier fuzzing pinning that `sql/policy.py`'s `_bare()`
  raises a build-time error while the trigger/rule path
  (`makeguitarmigrations._build_operations`) has no equivalent guard at all
  (`xfail(strict=True)`, flips once M4 fixes it); the 16-way `_save` x
  `_save_all_fields` x `_raise_for_excessive` x `_disable_signals` cross product of
  `update()`/`aupdate()`, exhaustively explored via hypothesis.
- `tests/test_tenancy_rls.py::TestThreeLevelMTIOwnerJoin`: the owner-join policy proven
  against a real three-level MTI chain (`Tour -> WorldTour -> StadiumTour`), not just the
  two-level raw-DDL fixture the rest of the file uses.
- `tests/test_mti_incremental.py` and `tests/mti_incremental/`: an MTI child added to an
  app whose parent's enforcement migration already exists and is already current --
  proving only the new child is reported missing and generated.

### Changed

- `tests/test_makemigrations_override.py` no longer mocks both Django's real
  `makemigrations` and the module's own `call_command` and asserts on call arguments
  (every one of the six tests would have passed if `makeguitarmigrations` became a
  same-named no-op). Replaced with five tests running the real command against a real
  throwaway app, asserting on what actually lands on disk.
- `tests/test_command.py`'s six `test_scoped_cascade_gap_*` tests collapsed into one
  parametrized test; same for the `_ensure_trigger_function_migration` /
  `_ensure_parent_trigger_function_migration` "writes and records the dependency" pair,
  and the two "`--check` reports a missing ... trigger function migration" tests. No
  coverage lost.
- Cursor-based raw-SQL test helpers (`execute`/`scalar`/`rows`), previously redefined
  independently across seven files, now live once in `tests/conftest.py`.
- Hand-written expected migration text in `tests/test_command.py` replaced with a
  `syrupy` snapshot -- the generator's own output is the source of truth.
- `tests/test_management_audittenancy.py` gained `pglast`-parsed structural assertions of
  generated policy SQL alongside the existing `pg_policies` text assertions.

## [1.2.0] - 2026-08-02

M1: fixing the measuring instruments themselves (#8) -- harness and CI
tooling only, no production code path changes behavior in this release.

### Added

- Branch coverage (`branch = true`), gated at 100% same as line coverage; the
  one gap this surfaced (`guc.py`'s transaction-marker replace-in-place loop
  never searching past a non-matching `run_on_commit` entry) is closed with a
  real test, not a lowered threshold.
- A GitHub Actions test matrix: Python 3.10/3.12/3.14 x Django 5.0/5.2/6.0
  (minus the one combination pip itself refuses) x PostgreSQL 14/18 -- 16
  cells, sampling the floor/mid/ceiling of each axis rather than the full
  20-cell grid the classifiers advertise (5 Python versions x 4 Django
  versions). Previously only 1 cell was ever verified by CI.
- `noxfile.py`, mirroring the same matrix for local runs via `uv run nox`.
- `psycopg` as an optional dependency (`pip install django-guitars[psycopg]`),
  installing `psycopg[c]` per psycopg's own production recommendation.
- A dynamic drift check in `tests/test_tenancy_denylist.py`: every public
  member Django's `QuerySet` exposes is now enumerated at runtime and must
  fall into one of four classified buckets, so a future Django release adding
  a new queryset method fails the suite instead of silently going unguarded.
- Coverage uploaded as a CI artifact (HTML report, one canonical matrix cell)
  so a coverage drop is diffable from the PR/Actions UI.

### Changed

- `pytest-xdist` (previously an unused dev dependency) now actually runs the
  suite in parallel (`-n auto`).
- `compose.yaml`'s Postgres image version is now parametrized
  (`POSTGRES_VERSION`, defaulting to 18) so CI can exercise more than one
  PostgreSQL major.

## [1.1.3] - 2026-08-02

Two follow-up bugs found reviewing #15 after it merged, one of them in that
PR's own fix.

### Fixed

- `hard_delete()`'s new `contextlib.suppress(Exception)` around
  `SWITCH_OFF_HARD_DELETION` swallowed a genuine failure of that statement
  itself, not just failures caused by an already-aborted transaction. If the
  DELETE succeeded but the following switch-off statement then failed for its
  own reason, `hard_delete()` returned normally with `rules.hard_deletion`
  left `'on'` for the rest of any enclosing transaction -- silently turning a
  later plain `.delete()` call in that same transaction into a hard delete.
  Verified against a real database before fixing. The suppression now only
  applies on the failure path (where the switch-off's own error would just
  replace the real `DELETE` error); on the success path a switch-off failure
  propagates, so the enclosing `atomic()` rolls the `DELETE` back too instead
  of leaking the switch open.
- `update(_disable_signals=True)` reported a tenant-write-guard-bypass
  finding even when `update_fields` collapsed to an empty set -- a case where
  `self.save()` is a no-op (no SQL, no signals) and so nothing was actually
  bypassed.

## [1.1.2] - 2026-08-02

Five confirmed bugs found by a multi-aspect quality review, each shipped
behind a regression test that was observed failing before the fix.

### Fixed

- `DisableSignals` stashed `signal.receivers` -- process-global mutable
  state -- per instance with no lock. Two overlapping blocks (nested or
  concurrent across threads) could race: the second block's `__exit__`
  restored from a stash taken *after* the first block had already emptied
  the list, overwriting the first block's correct restore with an empty
  one and permanently disconnecting every receiver in the process,
  including the tenant write guard. The stash is now a module-level,
  lock-guarded, reference count keyed by signal: whichever block enters
  first takes the real stash, and only the last one out restores it.
  `DisableSignals.__enter__` also now returns `self`, matching
  `with DisableSignals() as ds:` (it previously returned `None`).
- `update(_disable_signals=True)` disabled all eight `DEFAULT_SIGNALS` --
  including `pre_init`/`post_init`/`pre_delete`/`post_delete`/
  `pre_migrate`/`post_migrate` -- instead of just `pre_save`/`post_save`.
  Even narrowed, suppressing `pre_save` still disables the tenant write
  guard for a `GuitarModel` instance; that interaction is now reported
  once per model class via `guitars.tenancy.reporting`, and documented on
  `update()`.
- `update()` collapsed an empty `updating_fields` set to
  `update_fields=None` by truthiness, so an M2M-only or argument-less call
  rewrote every column instead of none -- the opposite of what the
  docstring promises. `_prepare_update` also applied attributes to the
  instance before validating that `_save=False` combined with M2M
  arguments should raise, so a raising call still left the instance
  mutated in memory. Both fixed: `update_fields` is `None` only when
  `_save_all_fields=True`, and validation now runs before any `setattr`.
- `hard_delete()` (both the multi-table-inheritance path and
  `_hard_delete_own_table`) opened a cursor on the module-global default
  database connection and quoted identifiers via its `ops`, ignoring the
  queryset's own `.db`. On a project with more than one database alias
  this either raised (no matching row on `'default'`) or silently deleted
  the wrong row. Both now resolve `connections[self.db]`, and the
  enclosing transactions are opened with `using=self.db`. The switch-off
  statement that re-enables the soft-delete rule is now also wrapped in
  `try`/`finally`, so it is a guarantee of the function itself rather than
  an effect that happened to follow from the enclosing transaction rolling
  back on error.
- The tenancy deny-list, applied to an unscoped queryset, missed two of
  Django's own database-touching `QuerySet` methods: `_raw_delete`
  compiles a `DeleteQuery` straight off `self.query` and executes it with
  no signals and no per-row guard -- unscoped, an unfiltered `DELETE`
  across every tenant -- and `explain()` executes an `EXPLAIN`, bypassing
  the `_fetch_all` chokepoint entirely. Both are now denied.

## [1.1.1] - 2026-07-31

No package changes -- tooling only. Upgraded pinned GitHub Actions
(`actions/checkout` v4 -> v7, `astral-sh/setup-uv` v5 -> v9.0.0,
`actions/cache` v4 -> v6) across all workflows, clearing the "targets
Node.js 20" deprecation warning the old pins were emitting on every run.

## [1.1.0] - 2026-07-31

### Upgrading

**`makemigrations --check` will fail on your first run, and that is the fix, not a
regression.** Every enforcement migration written before this release carries a
recognised comment header with no `[SQL:…]` identity, which now reads as *stale*
rather than *covered*. Run `makemigrations` (or `makeguitarmigrations`) and
`migrate`; each operation is re-emitted once, in a form that redefines the object
in place. A database already correct gets a refresh that is a no-op in effect.

This is how the 1.0.0 soft-delete guard fix finally reaches existing databases. Until
now it could not: see *Fixed* below.

### Added

- **`makeguitarmigrations --adopt`** — re-emit every enforcement operation for the
  apps in scope, in a form that is correct whether or not the database object already
  exists. For a database whose triggers, rules or policies were created outside this
  command: by hand, or by another generator whose comment headers this one cannot
  read.

  There was previously no supported way in. `create_tenant_policy` is a bare
  `CREATE POLICY` — PostgreSQL has no `CREATE POLICY IF NOT EXISTS` — so a table
  whose policy existed but carried no `[POLICY:…]` header took the "not covered"
  branch, emitted the `CREATE` form, and failed `migrate` with *policy
  "tenant_scope" already exists*. Editing comments in committed migrations by hand
  was the only route through.

  Cannot be combined with `--force-rls`, which acts only on tables whose policies
  this command already recorded — the very thing `--adopt` exists because you lack.
  Run `--adopt` first.

- `sql.REPLACE_UPDATED_AT_TRIGGER_FUNCTION`, `sql.REPLACE_UPDATED_AT_TRIGGER`,
  `sql.ADOPT_UPDATED_AT_TRIGGER` and their `*_PARENT_*` counterparts — the refresh
  and adoption forms. `IF EXISTS` appears on the adopt form and nowhere else: it is a
  claim about knowledge, and on a path where the answer is known it turns "your
  database has diverged from its migration history" into silence.

### Fixed

- **A change to any enforcement SQL constant shipped no migration.** 1.0.0 rewrote
  every soft-delete rule guard to `<> 'on'` — the fix for a rolled-back
  `hard_delete()` turning every later `.delete()` on that connection into a permanent
  delete — and then told you, in this file, to hand-write a migration to actually get
  it. That instruction is withdrawn; it is generated now.

  Two causes, and the second is the one that mattered. Generated migrations did
  `from guitars import sql` and named the constant, so the `[DIGEST:…]` marker covered
  a source that never contained the SQL. But the digest was never reached anyway: the
  per-table `_RE_*` header scan short-circuits first, so any table with a recognised
  header was treated as covered *forever*. Each operation header now carries a
  `[SQL:<digest>]` identity of its own SQL, and a stale or absent one re-emits.

- **Generated migrations no longer import from `guitars`.** They carry their SQL
  literally. Django freezes model state into migration files so that replaying history
  reproduces the same database; naming a library constant un-freezes exactly that — a
  fresh `migrate` on 1.0.0 built `<> 'on'` rules at migration `0003` while a database
  that ran `0003` on 0.7 had `= 'off'`, from an identical history.

  Migrations already committed on the old form keep working, so `guitars.sql`'s public
  names stay frozen forever. This stops the obligation growing rather than discharging
  it.

- **A changed trigger-function body shipped nothing.** Both singleton function
  migrations returned early on "a migration mentioning this function exists
  somewhere". They now compare the recorded digest too, and emit
  `CREATE OR REPLACE FUNCTION` — forced rather than defensive, since `DROP FUNCTION`
  refuses while any trigger depends on it and `CASCADE` would take every table's
  trigger with it.

- **A tenant policy whose SQL text changed was not replaced.** `[POLICY:…]` covers
  what the policy *says*, with `force` deliberately excluded so that flipping
  `GUITARS_RLS_FORCE` cannot defeat the staged `--force-rls` retrofit. That left a
  change to the policy SQL itself invisible to it. Both identities are now checked.

- **The comment headers were never actually guarded.** `CLAUDE.md` and
  `docs/migrations.md` both pointed at `tests/test_sql_interface.py`, which only ever
  checked `guitars.sql`'s exported names. The emit templates and the scan regexes are
  two hand-written copies in one module with nothing deriving one from the other, and
  a silent drift between them makes the next run in a consuming project duplicate every
  operation it already has. `tests/test_enforcement_identity.py` now asserts the round
  trip, and both documents are corrected.

### Changed

- The two singleton trigger-function scanners match their comment header rather than
  the `sql.CREATE_*_TRIGGER_FUNCTION` reference, which inlining removed. Both forms of
  migration carry the header, so migrations already written are still recognised.
- `unforced_policy_tables` reads the FORCE decision from the emitted SQL, falling back
  to the old `force=False` keyword wherever a pre-1.1.0 operation still records it that
  way. Reading only the new form would put every already-forced table back on the
  `--force-rls` backlog.
- `TableCoverage.as_kwargs()` returns a typed `PolicyKwargs` rather than
  `dict[str, object]`. The generator now *calls* `sql.create_table_rls(**kwargs)`
  instead of rendering the call as text, and text was never type-checked.

## [1.0.2] - 2026-07-31

No package changes — tooling only.

### Added

- `actionlint` to pre-commit, so a malformed workflow is caught before it is
  pushed. It lints one file at a time, so it does **not** catch the
  cross-workflow permission validation that broke the first attempt at 1.0.1 —
  noted in the config next to the hook.

## [1.0.1] - 2026-07-31

No package changes — release automation only.

### Fixed

- **Merging a version bump to `main` never actually cut a release.**
  `tag-release.yml` pushes the tag with the default `GITHUB_TOKEN`, and GitHub
  suppresses workflow triggers for events created with that token, so
  `release.yml`'s `on: push: tags` never fired. Every release from v0.5.0 to
  v1.0.0 was cut by hand from the Actions tab while both workflows documented
  the fan-out as automatic; v1.0.0 sat tagged with no release until it was
  dispatched manually. `tag-release.yml` now *calls* `release.yml` as a
  reusable workflow, which runs inside the same run, emits no event, and is
  therefore not suppressed — no PAT, and no recursion risk reintroduced.

  The release job moved to its own `_create-release.yml` for that call, because
  a called workflow's permissions are validated statically across all of its
  jobs regardless of `if:` — so calling `release.yml` would have meant granting
  every push to main the `id-token: write` its PyPI job declares. Split, the
  automated path *cannot* publish rather than being trusted not to. Publishing
  to PyPI is otherwise unchanged and stays manual-only.

## [1.0.0] - 2026-07-30

First stable release. Adds multi-tenancy as a database-enforced capability, and
renames the base models to make room for it.

### ⚠️ BREAKING

**Every rung of the instrument ladder shifted down one.** All three renames are
behaviour-identical — the same class under a new name:

| 0.7.0 | 1.0.0 | Behaviour |
| --- | --- | --- |
| `DutarModel` | `TarModel` | identical |
| `SetarModel` | `DutarModel` | identical |
| `GuitarModel` | `SetarModel` | identical |
| — | `GuitarModel` | **new meaning:** `SetarModel` + tenancy |

To upgrade without adopting tenancy, rename in that order — bottom-up, so no
intermediate state collides:

```
GuitarModel -> SetarModel
SetarModel  -> DutarModel
DutarModel  -> TarModel
```

Nothing else about those models changed: no new columns, no new migrations, no
behavioural difference. `makemigrations` should report no changes afterwards.

`GuitarModel` keeps its name and gains tenancy, so a model left on `GuitarModel`
by accident will fail loudly rather than quietly: without `GUITARS_TENANT_MODEL`
the `guitars.tenancy.E003` system check errors and names it.

**The soft-delete rule guards changed.** Databases migrated before 1.0.0 still
carry the old SQL — see *Fixed* below for why that matters and how to replace it.

### Added

- **Multi-tenancy**, enforced in two layers. `GuitarModel` contributes a non-null
  tenant `ForeignKey` (target `GUITARS_TENANT_MODEL`, name `GUITARS_TENANT_FIELD`,
  `CASCADE`, `editable=False`), wraps all three managers in `TenantedManager`, and
  gets a `tenant_scope` row-level-security policy from `makeguitarmigrations`.
  The Python layer raises `TenantScopeError` on an unscoped read or a cross-tenant
  write; the PostgreSQL layer enforces the same scope on joins, cascades,
  `_base_manager`, `instance.save()` and raw SQL. See
  [`docs/tenancy.md`](docs/tenancy.md).
- `guitars.tenancy` public API: `tenant()`, `tenancy_bypassed()`, `@tenanted`,
  `TenantedManager`, `TenantScopeError`, `get_tenant()`, `is_bypassed()`,
  `set_reporter()`, `tenant_spec()`, `local_tenant_fields()`.
- **MTI children get their own policy**, correlated to the ancestor holding the
  tenant column by the shared primary key. Relying on the ancestor's policy would
  leave every child-only statement unfiltered. See
  [ADR 0003](docs/adr/0003-mti-owner-join-policy.md).
- New settings: `GUITARS_TENANT_MODEL`, `GUITARS_TENANT_FIELD`,
  `GUITARS_TENANT_ENFORCE`, `GUITARS_TENANT_AUTOFILL`, `GUITARS_TENANT_POLICIES`,
  `GUITARS_RLS_FORCE`, `GUITARS_RLS_EXEMPT_ROLES`.
- New commands: `audittenancy` (asks a live database whether the policies bind,
  with `--require-force` for a deploy gate) and a `migrate` override that runs
  inside `tenancy_bypassed()` — without it a `RunPython` backfill under RLS
  matches no rows and is silently marked applied. `audittenancy` also reports
  when the role it connected as holds `SUPERUSER` or `BYPASSRLS`: every other
  check reads the catalog, which shows the same "enforced" whoever is asking, so
  those two would otherwise leave the audit passing over a connection no policy
  constrains. A warning, never fatal — it describes the connection, not the
  database.
- `makeguitarmigrations --force-rls`, the second stage of a staged retrofit.
- System checks `guitars.tenancy.E001`–`E003` and `W001`, registered at import of
  `guitars.models` so they fire even when guitars is used as a pure library.
  `W001` verifies that the `migrate` override above is the one `INSTALLED_APPS`
  order actually selects, since losing it is otherwise silent.
- `docs/` — tenancy, soft deletion, migrations, MTI — plus four ADRs and
  `CONTEXT.md`, the domain glossary.

### Fixed

- `refresh_from_db()` (via `HasCachedPropertyModel`) only expired
  `cached_property`s declared on the concrete class itself — one inherited from a
  mixin or an abstract base stayed stale after a refresh, and naming it explicitly
  in `expire_cached_properties()` raised `KeyError`. The scan now walks the MRO.
- `queryset.hard_delete()` on a non-MTI model spliced the hard-deletion switch and
  the `DELETE` into one parameterised multi-statement `execute`, which only works
  under psycopg's client-side binding (Django's default). It now issues three
  statements inside the same transaction, like the MTI path always did, so
  `server_side_binding = True` no longer breaks it.
- **A rolled-back `hard_delete()` turned every later `.delete()` on that
  connection into a permanent delete.** `hard_delete()` sets
  `rules.hard_deletion` transaction-locally; PostgreSQL reverts that on rollback,
  but not to *unset* — a custom setting that was set and rolled back reads back as
  the **empty string**. The rule guards tested `= 'off'`, which the empty string
  matches neither way, so the `DO INSTEAD` rule stopped firing. With
  `CONN_MAX_AGE` or any pool the blast radius is the connection, not the
  transaction. All guards are now `<> 'on'`: anything but an explicit opt-in
  preserves the row.

  **Existing databases need one action.** The rules were created by migrations
  that call these SQL constants by name, so a database migrated before 1.0.0 still
  carries the old guard, and no new migration is generated for it — the idempotency
  digest covers the operation source, not the SQL it expands to. Write a one-off
  migration that re-runs the same constants; they are now created `OR REPLACE`, so
  each definition is swapped in place inside one transaction, never leaving the
  table without a rule. See [`docs/soft-deletion.md`](docs/soft-deletion.md) for the
  shape.

  Do **not** reverse the enforcement migration and re-apply it. Its `reverse_sql`
  drops the rules, so between the two `migrate` commands every `.delete()` on those
  tables is a permanent delete — and reversing to a previous migration unapplies
  everything after it, not just the enforcement one. New databases are correct on
  first `migrate`.
- Role names in `GUITARS_RLS_EXEMPT_ROLES` were interpolated into policy SQL
  **unquoted**, so `metabase-ro` was a syntax error and `BI_Reader` silently bound
  `bi_reader`. Role-derived names are now quoted at both nesting levels, and every
  table/column identifier must prove it is a bare lower-case identifier or the
  generator refuses at build time rather than emitting SQL that fails inside
  `migrate`. Output for ordinary lower-case names is byte-identical.
- `makeguitarmigrations` reported a cascade foreign key reached through MTI as an
  unsupported limitation, when the ancestor's own rule already covers the whole
  chain through the shared `_deleted_at`.
- **A tenant policy was never regenerated once it existed**, so a model that gained
  a tenant dimension — or had its tenant column renamed, or its
  `GUITARS_RLS_EXEMPT_ROLES` edited — kept the *old, weaker* predicate in the
  database. Dedupe was keyed on the table name alone, and the comment header carried
  none of the predicate, so the generator emitted nothing and
  `makemigrations --check` reported nothing to do: the Python layer enforced the new
  dimension while row-level security enforced only the old one, silently, on exactly
  the paths (raw SQL, `_base_manager`, cascades) where the policy is the only guard.

  Policy headers now carry a `[POLICY:<digest>]` identity covering everything that
  determines what the policy says, and a changed shape emits `sql.replace_table_rls`
  — PostgreSQL has no `CREATE OR REPLACE POLICY`, so re-emitting the `CREATE` form
  would fail `migrate`. `force` is excluded from the identity, keeping the
  `--force-rls` retrofit workflow unchanged. See
  [`docs/migrations.md`](docs/migrations.md).
- **A tenant primary key containing a comma silently widened the policy to several
  tenants.** The predicate splits the published GUC on `,` and tests membership, so a
  single pk of `acme,globex` encoded identically to the two-tenant scope
  `['acme', 'globex']` and PostgreSQL read it as "tenant acme OR tenant globex" —
  while the Python manager filtered on the exact string and matched neither. The
  database half was therefore strictly *wider* than the Python half. Such a value is
  now refused when the scope is published, so it fails closed. Only reachable with a
  non-integer tenant primary key.
- `audittenancy` could not see a policy that existed but enforced the **wrong
  scope**, and counted a table without `FORCE` as "enforced" in its summary. It now
  compares each live policy's `tenant.*` settings and its `pg_depend` column
  references against what the models imply — warned by default, fatal under the new
  `--require-match` — and counts only clean tables as enforced. It also resolves a
  table name through the search path in the order PostgreSQL does, so two same-named
  tables in two schemas no longer collide.
- The settings are compared for **both halves of a policy separately**. `USING`
  governs reads and `WITH CHECK` governs writes, and they are independently
  editable: a policy left as `USING (<tenant match>) WITH CHECK (true)` reads as
  fully scoped while accepting every cross-tenant write, and neither the `USING`
  settings nor the `pg_depend` columns give it away (`true` references nothing).
- `makeguitarmigrations` interpolated the primary-key *field name* into the
  timestamp trigger and soft-delete rule where PostgreSQL needs the *column*. The
  two agree for an ordinary `id`, so this only bit a model whose primary key sets
  `db_column` or is a `OneToOneField(primary_key=True)` — where the generated rule
  named a column that does not exist and failed at `migrate`. The MTI forms already
  used the column.

### Changed

- The three soft-delete rules are created `OR REPLACE`, so a rule can be redefined
  without an instant in which the table has none — and an instant without a
  `soft_delete` rule is an instant in which `DELETE` destroys rows. Output on a fresh
  database is otherwise unchanged.
- `sql.py` became the `sql/` package (`triggers`, `soft_delete`, `policy`), still
  re-exported flat — `from guitars import sql` is unchanged, and every name in it
  remains a frozen interface that generated migrations read by name.
- Migration-file mechanics (digest stamping, scanning, `--empty` scaffolding, app
  scoping) moved to `guitars.management._generator`. MTI column-ownership
  resolution moved to `guitars.introspection`, shared by the generator and by
  tenancy coverage discovery so the two cannot disagree.
- The generated migrations and the command's output use one vocabulary,
  **enforcement migrations**, with four precisely-named kinds inside. The previous
  wording ("advanced migrations") described nothing, and the same concept had
  gathered four names. Generated filenames are now `*_auto_enforcement*`.
- `makeguitarmigrations` now also generates tenant policies, so there is still one
  command. `audittenancy` validates app labels the way the generator does, so a
  typo cannot produce a green gate that audited nothing.
- The test suite runs as a deliberately non-superuser PostgreSQL role that owns its
  tables (`scripts/postgres-init.sql`) — the exact condition `FORCE ROW LEVEL
  SECURITY` exists to constrain. Existing checkouts need
  `docker compose down -v` once.
- CI now gates on `makemigrations --check`, `audittenancy --require-force`, and
  100% coverage.

## [0.7.0] - 2026-07-06

### Added

- Full **multi-table inheritance (MTI)** support for dated and soft-deletable
  models. A concrete model subclassing another concrete `GuitarModel` now works
  end to end: `makeguitarmigrations` detects that a child's `_updated_at` /
  `_deleted_at` columns live on an ancestor table (via column ownership, not
  `hasattr`) and generates the right database objects — a redirect soft-delete
  rule that preserves the child row and marks the parent, a `set_parent_updated_at`
  trigger so a child-only `update()` still bumps the parent's `_updated_at`, and
  cascade rules attached to the owning table. `hard_delete()` (instance and
  queryset) now clears the whole MTI table chain with no orphaned parent row.
  Works at any inheritance depth via the shared-PK invariant.

### Notes

- MTI children of a soft-deletable base must declare their own `Meta` (an empty
  `class Meta: pass` suffices) so Django doesn't re-declare the parent's
  `_deleted_at` partial index against the child's non-local column
  (`models.E016`).
- Not yet supported: cascading *into* an MTI child through a FK on the child's
  own table when its `_deleted_at` lives on a farther ancestor; the command skips
  it with a warning rather than emitting a broken rule.

## [0.6.0] - 2026-07-03

### Changed

- Merged the separate `publish.yml` workflow into `release.yml`, now named
  "Release and Publish". A `vX.Y.Z` tag push still only creates the GitHub
  Release; PyPI publishing remains manual-only, opted into via the `publish`
  input on a `workflow_dispatch` run.
- Release/publish `workflow_dispatch` runs now select the tag from the native
  "Use workflow from" ref selector instead of a free-text input.
- Restricted CI to the `main` branch and removed the `develop` branch from the
  development flow.

## [Unreleased]

## [0.5.1] - 2026-07-03

### Added

- `makemigrations` now also generates the advanced trigger/rule migrations that
  `makeguitarmigrations` produces, so the soft-delete rules and `updated_at`
  triggers can no longer be silently forgotten. `makemigrations --check`
  validates both layers. Opt out with `GUITARS_AUTO_MAKE_MIGRATIONS = False` to
  keep the explicit two-command workflow; the standalone `makeguitarmigrations`
  command is unchanged.

### Changed

- `makeguitarmigrations` now accepts optional app labels to scope generation
  (e.g. `makeguitarmigrations blog`), and `makemigrations` forwards any app
  labels it receives, so a scoped `makemigrations blog` only generates guitar
  migrations for `blog`. With no labels, all `LOCAL_APPS` are scanned as before.
  An unknown app label is now rejected the same way Django's own
  `makemigrations` rejects one, so a typo can no longer turn `--check` into a
  silent no-op. Cross-app CASCADE soft-delete rules are attributed to the
  *parent* model's app, so scoping to a child app alone skips the rule; the
  command now prints a warning naming the skipped rule and the app to include
  to close the gap.
- (dev only) `publish.yml` is now `workflow_dispatch`-only instead of firing on
  every `vX.Y.Z` tag push, so shipping to PyPI is a deliberate manual step.
  `release.yml` now only creates a GitHub Release for tags reachable from
  `main`, and its "update an existing release" path no longer breaks (it
  previously passed `gh release edit` a `--generate-notes` flag that command
  doesn't support).

## [0.3.0] - 2026-06-11

### Added

- Interactive release tooling under `scripts/` (development only, not shipped
  in the wheel): `bump.sh` bumps `pyproject.toml` and seeds a changelog
  section; `release.sh` creates the git tag and GitHub release from the
  matching changelog notes. Documented in `scripts/README.md`.

### Changed

- `guitars.__version__` is now read from the installed package metadata
  (`importlib.metadata`) instead of a hardcoded string, making
  `pyproject.toml` the single source of truth for the version.

### Documentation

- `CLAUDE.md` repo guidance for contributors and AI assistants, plus a
  "Releasing" section in the README.
- Clarified the setar etymology (three strings by name) versus the model's
  actual string-count ladder.

## [0.2.0] - 2026-06-06

### Added

- `DutarModel` — the lightest base: `.update()` / `.aupdate()` and
  cached-property invalidation, with no timestamp or soft-delete columns.
- `DatedModel`, `UpdatableModel`, and `HasCachedPropertyModel` are now exported
  from `guitars.models` for composing custom bases.

### Changed

- `SetarModel` now builds on `DutarModel` (`DatedModel` + `DutarModel`); the
  public API is unchanged.

## [0.1.0] - 2026-06-04

### Added

- `SetarModel` — base abstract model: DB-default `_created_at` / `_updated_at`
  timestamps, `.update()` / `.aupdate()` helpers, and cached-property
  invalidation on `refresh_from_db()`.
- `GuitarModel` — `SetarModel` combined with `SoftDeletableModel`.
- `SoftDeletableModel` with `LiveManager` / `ArchiveManager` /
  `AllObjectsManager` — PostgreSQL-enforced soft deletion, cascade soft delete,
  and `hard_delete()`.
- `DisableSignals` context manager for temporarily muting Django signals.
- `makeguitarmigrations` management command — generates the PostgreSQL
  trigger/rule migrations behind the timestamps and soft deletion.

[Unreleased]: https://github.com/Behnam-RK/django-guitars/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.7.0
[0.6.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.6.0
[0.5.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.5.1
[0.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.3.0
[0.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.2.0
[0.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.1.0
