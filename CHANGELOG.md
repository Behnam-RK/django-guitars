# Changelog

<!-- doc-budget: exempt — release history; length tracks release count, not verbosity -->

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Full history and diffs: [GitHub releases](https://github.com/Behnam-RK/django-guitars/releases).

## [Unreleased]

## [2.4.0] - 2026-08-21

- Fixed: the owned rule's last-owner guard now reads **every** `OwningForeignKey` pointing at the dependent, not only the rule's own column. 2.3.0 scanned the declaring owner's column on the declaring owner's table alone, so where a dependent was owned from two places, soft-deleting the last owner *of one kind* archived it while a live owner *of another kind* still pointed at it — data loss, reproduced against a real database within a day of that release. Each rule now carries one `NOT EXISTS` arm per owning column targeting the dependent: N owning columns produce N rules of N arms each. `on_delete` never covered this and could not have — `RESTRICT`/`PROTECT` govern deletion of the *target*, while the owned rule fires when an *owner* is soft-deleted ([ADR-0012](docs/adr/0012-cross-owner-last-owner-guard.md), `docs/owned-relations.md`).
- Fixed: an owner that keeps `_deleted_at` on a multi-table-inheritance ancestor now contributes a **joined** arm — its key on its own table, its liveness on the ancestor's, matched on the primary-key value the chain shares. Reading such an owner as contributing nothing was a second instance of the same data loss, reproducible in this repo's own test app: `Album.press_kit` and `Orchestra.programme` over one `PressKit`, where archiving the last album archived the kit out from under a live orchestra. That owner is still refused a rule of its *own* — it would have to fire on a table its key is not on — so it spares what it owns while never stamping it, leaving a dependent whose last owner is one of those live rather than archived.
- Fixed: an arm reading the *dependent's own* table excludes the target row, by the rule's key. `tests/testapp`'s `Placard` now owns itself, so the shape ships in a generated migration and is asserted against Postgres rather than as rendered text. A target declaring an `OwningForeignKey` to itself is refused a rule of its own — that relation is a one-node `ON UPDATE` cycle — but still contributes an arm to every other owner's rule, and a row of it pointing at its own primary key read as its own live owner, pinning the target un-archivable by any owner forever.
- Fixed: self-exclusion is keyed on the **row**, and on the table the arm reads *liveness* from — the ancestor's where it joins, since that is what it matches one row per. An arm reading the table the rule fires on excludes `old."<pk>"`; one reading the dependent's excludes `old."<fk>"`; anything else carries none, no row of theirs going away in that statement. Keying on the table holding the key instead left both exclusions off every joined arm: one owner and co-owner in a single MTI chain read as its own last live owner and pinned the target un-archivable, and an MTI child of the dependent owning it back did the same. Excluding only the declaring column's arm would have let a row owning the target through two of its own columns read as its own last live owner and hold the target alive forever — the shape `tests/testapp`'s `Album.press_kit`/`Album.alt_press_kit` pair has always had.
- Changed: a dependent owned from exactly one place renders **byte-identically** to 2.3.0 — arm 0 keeps the literal `guitars_owner` alias and the co-owner block collapses to nothing — so its `[SQL:…]` identity does not move and no `DROP`+`CREATE` is emitted for it. Only multi-owner rules regenerate. Rule *names* are unchanged either way, so a regenerated `CREATE OR REPLACE RULE` lands on the same database object and nothing has to be retired. A new byte-exact snapshot test pins the single-owner rendering; the owned family previously had only round-trip idempotency coverage.
- Changed: arms are read over the **whole model registry**, not `LOCAL_APPS` — a live owner is live whether or not the kit generates enforcement for its app, and excluding non-local owners would have re-created this bug for third-party models. Consequence to know: a rule's text now depends on models in packages you do not control, so upgrading such a package can move its `[SQL:…]` identity.
- Changed: **an owned rule is now refused where a policy on a co-owner's table filters on a tenant dimension the dependent's policy does not**, with a warning. Compared per dimension rather than as "is the dependent tenanted at all": reaching the dependent's row already put the session inside every dimension the two share, so only a dimension the co-owner has and the dependent lacks can hide a live owner — an untenanted dependent being the case where that is all of them. Asked of **both** tables a joined arm reads, liveness coming from the ancestor's, where a policy hides exactly the same live owner. Compared per emitted *policy* rather than per `tenanted_manager`, too: a dimension that traverses a relation is left to Python scoping and filters nothing, so reading the manager both withheld correct rules and missed the case where the dependent declares a dimension its own policy cannot predicate. A model outside `LOCAL_APPS`, which this kit emits no policy for, is read one way on each side, since the two sides subtract and add: the co-owner's manager counts as enforced (its own package may carry the policy), the dependent's as absent (nothing here can promise a session was narrowed). One default could not be safe for both. The guard's `NOT EXISTS` is an ordinary `SELECT`, so a tenant policy on a table it reads hides an out-of-tenant live owner and a still-owned row is stamped — the one place the kit's guards do not fail safe. 2.3.0 could only reach that through the table you declared the key on; an arm reaches tables the declaring model never names. **This is the one shape where a schema green on 2.3.0 can go red on 2.4.0.** The declaring owner's own tenancy is deliberately not re-examined, that shape having shipped in 2.3.0. Gated on `GUITARS_TENANT_POLICIES`, so a Python-only tenancy project is unaffected.
- Changed: an owned refusal that fires over a rule the project has **already** recorded is now an error that fails `--check`, naming the `DROP RULE` to run. Refusing emits nothing, so unlike every other refusal — which only ever fires where no rule was written — the stale rule would otherwise stay live and wrong under a green `--check`. No command retires a rule.
- Added: `_scoped_owned_gap_notes`, the owned twin of the cascade gap notes. A scoped run whose in-scope models move an out-of-scope owned rule's guard now says so on stdout. Warned rather than escalated: an unscoped run — what CI runs — re-derives every rule and emits the `DROP`+`CREATE`. It re-runs the owned candidate test over apps the run was *not* asked about, so it asks for that test's verdict without its warnings — otherwise a scoped run would report another app's misconfigurations, and fail `--check` over one of them. It also applies the refusals that come *after* that test — self-update, rule cycle, tenancy mismatch — since a relation refused there has no rule for an in-scope arm to make stale, and a note about one would ask for an unscoped run that prints the same note again.
- Note: the guard's per-**statement** limit is unchanged. One multi-row `UPDATE` soft-deleting two owners of a shared dependent still leaves the dependent alive, PostgreSQL running an `ON UPDATE` rule's action before the original update. It fails *safe*, and closing it needs a statement-level trigger — considered and rejected for this release, with the reasoning recorded in ADR-0012 rather than left to be rediscovered.
- Note: the tenancy refusal above is not mirrored in `hard_delete()`'s candidate test, so it still follows a relation the generator now refuses and, reading the co-owner through the same policy, removes a row that co-owner still references. The foreign-key check is exempt from RLS, so that aborts the transaction at `COMMIT` — loud, and the outcome `docs/owned-relations.md` already records for the per-visible-row limit — unless the key was declared `db_constraint=False`, which gives up the check that makes it loud. Recorded in ADR-0012 rather than closed: the shared answer belongs beside `introspection.rule_update_cycle_edges`.
- Note: `hard_delete()` is unchanged and was never wrong here — `_still_referenced` already walked every relation into the target's MTI chain. It archived the dependent only because its Phase 1 is a plain `delete()`, which fires the rule. The fix is entirely in the generated SQL.

## [2.3.0] - 2026-08-21

- Added: `guitars.models.OwningForeignKey`, declaring that a row **owns** what its foreign key points at — soft-deleting the owner soft-deletes the target. Cascades were inbound only: the generator walked reverse relations and keyed off `on_delete=CASCADE`, so the one shape it could not express was the key living on the owner. `on_delete` cannot carry it either — it says what happens to *this* row when the target goes, the opposite direction, so declaring the relation `CASCADE` to reach the generator emitted the rule backwards and told Django's `Collector` the same untruth. `on_delete=CASCADE` on an `OwningForeignKey` is refused as `guitars.E001`, and a non-primary-key `to_field` as `guitars.E002` — the rule correlates the key against the target's primary key, which is also what makes ownership into an MTI child work. The generated rule is named after its foreign-key column under a `soft_delete_owned_` prefix, so two owned keys to one table get distinct names and neither can ever meet the inbound `soft_delete_related_` namespace — a PostgreSQL rule is namespaced by name alone, so a collision replaces silently rather than failing ([ADR-0011](docs/adr/0011-owner-side-soft-delete-ownership.md), `docs/owned-relations.md`).
- Added: the owned rule carries a **last-owner guard** — a target another live row still points at survives, and is stamped when the last owner goes. Emitted unconditionally rather than only where a `UniqueConstraint` proves single ownership: constraint-shaped SQL would go silently wrong the day the constraint was dropped, since that changes no model field, so the operation's `[SQL:...]` identity would not move and `--check` would stay green over an unguarded rule. `hard_delete()` applies the same predicate in Python, narrowed three ways because it *removes* the row where the rule only stamps a column: the whole collected batch is spared rather than one row; an archived referrer still counts, its foreign key being on disk; and **any** surviving foreign key holds the row back, not only the owning column — a second `OwningForeignKey`, a relation the generator refused, a plain `ForeignKey` from anywhere, and one pointing at *any* level of an MTI chain, since collecting one row of a chain removes every table in it. Dropping a still-referenced row fails the deferred constraint at `COMMIT` and takes the whole transaction with it. Collection runs to a fixpoint over everything being removed, not one pass per ownership hop, since a row spared by a reference that is *itself* collected later would otherwise be left stranded. A `CASCADE` referrer never counts, being collected *with* the row it points at, and is discounted up front rather than left to the fixpoint — it is only ever collected as a consequence of collecting the very row it would hold back, so a later pass could never reach it and every owned row that has children would stay archived forever. That discount is by **row**, not by relation: one model can hold a `CASCADE` key *and* a plain one to the same target, and it goes with that target either way. It reaches the whole `CASCADE` closure rather than one hop, since a *grand*child is collected too and the same "collected only as a consequence" argument applies to it verbatim. Because that closure is computed for every candidate at once, sparing one target is re-asked rather than subtracted: a spared row keeps its closure alive, so a referrer inside it survives after all and holds the *next* target back — one subtraction removed that one with a live key still on disk. A key aimed at a non-primary-key `to_field` is read through the column it actually holds, not the primary key it stands for, which it would never match — by the guard **and** by the `CASCADE` collection it defers to, the two being each other's premise: discounting a child is only safe because collection really does follow it. Referrers are read through the base manager, never a model's default one, for the same reason Django's own `Collector` does: a filtering default manager on a plain `ForeignKey`'s model would hide a live referrer, which then reads as absent. The *rule*'s guard remains per foreign-key column, per statement, and — since its `NOT EXISTS` is an ordinary `SELECT` — per row a tenant policy lets it see; `docs/owned-relations.md` spells out all three and what they cost.
- Changed: instance-level `hard_delete()` now follows owned relations, removing each owned row *after* the batch that owned it — the reverse of the child-first `CASCADE` order, since the owner still references what it owns. Without it, `hard_delete()` left the owned row stranded with nothing pointing at it and no cascade able to reach it. It follows only relations that actually carry a rule — the same candidate test the generator applies — since one it refused leaves the target alive on the soft-delete path. Queryset-level `hard_delete()` is unchanged: as with reverse-FK children, it does not walk them.
- Note: three shapes are refused by the generator with a warning rather than by a field check, since each depends on more than the field itself: a target with no `_deleted_at` (nothing to stamp, and an `OwningForeignKey` has no other purpose), an *owner* with no `_deleted_at` (nothing ever transitions, so the rule could never fire), and a relation that would close a **cycle** of `ON UPDATE` rules. That last covers self-ownership and ownership of an MTI descendant, where the rule's action updates the table it fires on, but also longer loops — two models owning each other, or an owned rule closing a ring through another model's `CASCADE` rule. A rule's action expands *before* the original statement, so PostgreSQL rewrites the cycle into itself and rejects every `UPDATE` to every table in it, a plain `save()` included, as infinite rule recursion. The whole registry's ON UPDATE graph is consulted, not the model in hand, and **every** edge on a cycle is refused rather than one chosen edge — which one got dropped would otherwise depend on iteration order, and `--check` would flap. `hard_delete()` refuses all of these too.
- Fixed: a **self-referential** `on_delete=CASCADE` foreign key on a soft-deletable model (a category tree, a comment thread) generated a cascade rule whose action updated the table it fires on. PostgreSQL rewrites such a rule into itself and rejects it at rewrite time, so **every** `UPDATE` on that table failed with `infinite recursion detected in rules for relation` — a plain `save()` included, and the `WHERE` guard never got the chance to run. `migrate` reported success and left the table unusable. Predates this release; found while guarding the same shape on the new owned rule, which is refused for the same reason. The same is now true of any longer cycle, cascade and owned rules alike. All of them warn and emit nothing.
- Fixed: instance-level `hard_delete()` entered a reverse-`CASCADE` child at the level that declares the foreign key, not at that level's MTI root, so a key declared on an MTI *child*'s own table (with no ancestor foreign key to the same target shadowing it) removed the child table's row and left every ancestor table's row behind — soft-deleted, reachable by no query of the child, and pointed at by a parent-link foreign key from nowhere. The seed and the new owned hop both enter from the root; this was the one entry point that did not. Predates this release. A parent-link relation still enters at the level it names, since it is the walk *down* the chain.
- Note: "`hard_delete()` follows exactly the relations the generator emits rules for" holds per relation, not across app scope. Enforcement is generated only for `settings.LOCAL_APPS`, so an `OwningForeignKey` on a model in an installed-but-not-local app gets no rule while `hard_delete()` still follows it — removing an owned row the rule never archived. Such a model has no soft-delete rule of its own either, so its plain `.delete()` already destroys rows; the kit is not usable from a non-local app, and this is one more consequence rather than a new one.
- Note: no enforcement command retires a rule — true of cascade rules since 0.x, and now of owned rules too. Dropping an `OwningForeignKey` makes the generated `RemoveField` fail at `migrate` (`cannot drop column ... rule ... depends on column`), and converting one back to a plain `ForeignKey` leaves the rule live while `--check` stays green. Add an explicit `DROP RULE` to that migration, copying the name from the migration that created it.
- Changed: an owned rule is named `soft_delete_owned_<n>_<target>_<n>_<fk>`, every variable segment carrying its own length — schema included. `('shop_press_kit', 'kit_id')` and `('shop_press', 'kit_kit_id')` used to concatenate to the same string, and since PostgreSQL dedupes a rule by name per table, the second `CREATE OR REPLACE` silently replaced the first and one ownership stopped cascading with `--check` green. Sizing the column alone would have ruled out that *adjacent* split while leaving the table/column boundary unparseable, so `('a_5_b', 'c')` and `('a', 'b_1_c')` would still have met on `soft_delete_owned_a_5_b_1_c`; a length before *each* segment leaves nothing to guess at, and no two `(schema, table, column)` triples can reach one name. The cascade family's plain `<table>_<fk>` suffix has the same hole and **cannot move**: it shipped in 0.x, and since nothing retires a rule a rename would leave every migrated project's old rule live beside the new one. Both families are reported regardless — a proof in one is not a reason to stop watching. Owned rules are new in 2.3.0, so renaming them costs nothing; a project that already migrated a 2.3.0 pre-release should drop the old-named rules by hand. See `docs/migrations.md`'s "Rule names".
- Fixed: the rule-name clash report compared the operation's *dedupe key*, and two distinct cascade relations can share one. An MTI parent and its child resolve to the same `owner_table`, and `seen_related_tables` is per call, so a model with a `CASCADE` foreign key to each of them minted two "primary" rules for one related table — same bare name, same table, and the same key. The report read those two claims as one and said nothing while the second `CREATE OR REPLACE` silently killed the first cascade; worse, their identical header left the scan a single digest to record for two operations, so one of the pair read as stale on every later run and `makeguitarmigrations` emitted a fresh migration forever, `--check` never green. The claim is now keyed on the relation, whose column is always filled in. Predates 2.3.0's clash report in the sense that the collision did; nothing detected it before.
- Changed: a non-primary-key `to_field` is now refused by the generator and by `hard_delete()` as well as by the field check. `guitars.E002` reports it, but `--skip-checks` still reaches the generator and `hard_delete()` runs no system checks at all — the same reason the `ON UPDATE` cycle graph re-asks `guitars.E001` rather than trusting it. A redirected key made the rule correlate `old."<column>"` against the target's *primary* key, stamping whichever row happened to carry the `to_field` value as its pk, and `hard_delete()` handed those same values to its collection as primary keys and **removed** those rows. Both sides now share one predicate.
- Fixed: a relation refused for a non-primary-key `to_field` still contributed an edge to the `ON UPDATE` rule graph, so it could close a cycle no rule can form — and every edge on a cycle is refused, which took the legitimate ownership pointing the other way down with it. That relation silently stopped archiving its target, with a warning naming a cycle that did not exist. The graph now carries only relations that really do get a rule, which is the invariant it is read for.
- Changed: a rule-name clash now **fails `--check`**, not only stderr. One of the two rules does not exist in the database the migration ships to, which is exactly what `--check` exists to catch; a generating run has written the files by the time it is known, so there it stays a report.
- Added: `guitars.W001` warns about an `OwningForeignKey` whose `on_delete` *clears* the key — `SET_NULL`, `SET_DEFAULT`, `SET(…)`. A warning rather than an error because it is legal and occasionally wanted, but silent otherwise: deleting the *target* runs Django's `Collector`, which clears the column on every owner **before** the rule turns the `DELETE` into an `UPDATE`, so the archived row is unreachable from its former owners and `hard_delete()` can never collect it. `E001`'s hint no longer recommends `SET_NULL` for the same reason.
- Note: `hard_delete()`'s collection reaches no `GenericRelation` on an owned target. Correct for the sparing half — a generic relation carries no foreign-key constraint, so it cannot fail at `COMMIT` and must not hold the row back — but a gap on the collecting half: only Phase 1's `Collector` walks `_meta.private_fields`, and an owned row is stamped by a *rule* rather than collected, so `hard_delete()` on its **owner** leaves the generically-related rows pointing at a primary key that no longer exists. Deleting through the target itself does clean them up. `docs/owned-relations.md` carries the workaround.
- Changed: every `OwningForeignKey` in `tests/testapp` now declares `DO_NOTHING`, what the docs tell a consumer to use, rather than the `SET_NULL` they warn against — Django's `Collector` clears the key on every owner before the rule rewrites the `DELETE`, leaving the archived target unreachable from its former owners and uncollectable by `hard_delete()`. The reference models are what a consumer copies, and the whole owned-relations suite had been exercising the hazardous shape.
- Note: ownership *into* an MTI child is supported — the key holds the primary-key value every table in the chain shares, so the rule correlates against the ancestor owning `_deleted_at`. Ownership declared *on* an MTI child whose `_deleted_at` lives farther up is refused with a warning, the mirror of the existing inbound limitation: the rule fires on the ancestor's table, where `old."<column>"` cannot name a column the child holds.

## [2.2.1] - 2026-08-19

- Fixed: the test harness only. Both modules writing to `tests/makemigrations_override/migrations/` carry `xdist_group` to stay on one worker, but xdist honours that mark only under `--dist loadgroup`, and its default (`load`) ignored it — so the full suite failed at random with a `ModuleNotFoundError` for a migration one worker unlinked while another was importing it. `--dist loadgroup` now sits in `addopts`, and a test asserts it is set. No change to the shipped package.
- Fixed: the test harness only. `tests/test_concurrency.py` swept its leftover worker-thread connections with `pg_terminate_backend`, which also killed the backend behind the Django connection asgiref keeps in its one persistent thread for the async ORM. A later async test reusing that thread found the object still there and the server gone, failing with `OperationalError: terminating connection due to administrator command` — latent until `--dist loadgroup` put the two modules on the same worker in that order. The sweep now closes that thread's connections inside the thread first, so what it terminates belongs to threads that no longer exist. No change to the shipped package.

## [2.2.0] - 2026-08-19

- Fixed: `audittenancy`'s autofill check was presence-only — a `guitars_fill_*` function whose body had been hand-edited, or generated by an older kit, was reported healthy while it misbehaved (a lost `tenant.bypass` guard stamps every `tenancy_bypassed()` insert with the last published scope; a lost separator guard writes `a,b` into the column). The body is now compared against the kit's own template, whitespace-collapsed so a differently-indented migration is not drift, and the finding names the missing guard where it can ([#29](https://github.com/Behnam-RK/django-guitars/issues/29), [ADR-0010](docs/adr/0010-autofill-body-comparison.md)). Warned by default, fatal under `--require-match`, the same severity as predicate drift. The finding names the guard where the body is still recognisably this kit's, and stays generic where it is not — a body retyped by hand (`TRUE` for `true`) is not accused of losing every guard, and a guard commented out rather than deleted is named as missing instead of being read as intact. One finding per function, naming the tables it fills for, since one `pg_proc` row serves every model on a `(dimension, column)` pair.

## [2.1.1] - 2026-08-16

- Fixed: a renamed `GUITARS_TENANT_FIELD` or tenant-FK `db_column` left the previous autofill trigger in place, still calling a function that dereferenced a dropped column — **every `INSERT` on the table then failed**, while `audittenancy` reported it healthy. `makeguitarmigrations` now retires an autofill trigger the models no longer require, and `audittenancy` reports one the database has and the models do not expect ([#27](https://github.com/Behnam-RK/django-guitars/issues/27), `docs/migrations.md`'s "Retirement").
- Fixed: flipping a manager to `autofill=False` was a no-op in the database — the trigger kept filling. It is now dropped, making ADR-0005's "the opt-out is auditable in `pg_trigger`" true.
- Fixed: a tenant column owned by an untenanted MTI ancestor got **no** autofill trigger at all, silently. The trigger now goes on the ancestor's table, attributed to that ancestor's app ([ADR-0009](docs/adr/0009-relocated-owner-table-autofill.md), [#28](https://github.com/Behnam-RK/django-guitars/issues/28)). It is refused, with a note, where descendants sharing the column disagree about autofilling it or claim it under different dimensions. **Note:** the trigger also fills the ancestor's own direct inserts, which are untenanted — pass `autofill=False` if that is wrong for your models.
- Changed: 2.1.0's note that `DisableSignals` "costs the friendly message, not the guarantee" held only where a trigger was actually emitted, which the two fixes above make true generally.
- Added: `TableCoverage.owner_autofill_columns` and `owner_autofill_notes()` in `guitars.tenancy.discovery`, and `AUTOFILL_FUNCTION_PREFIX`, which `audittenancy` uses to tell this library's triggers from an application's own.

## [2.1.0] - 2026-08-16

- Added: tenant autofill is now a `BEFORE INSERT` trigger ([ADR-0005](docs/adr/0005-trigger-based-tenant-autofill.md)), covering `bulk_create`, multi-row `INSERT`, `INSERT … SELECT` and raw SQL. Run `makemigrations` + `migrate`; **no backfill is needed or possible** — the tenant column is `NOT NULL`, so an existing `NULL` cannot exist.
- Changed: the `pre_save` write guard is now diagnostics. `DisableSignals` (and `update(_disable_signals=True)`) no longer disables tenancy enforcement; it costs the friendly message, not the guarantee.
- Added: `audittenancy` reports a table whose manager autofills but whose trigger is missing — warned by default, fatal under `--require-match`.
- Added: `TableCoverage.autofill_columns`, and `autofill_function_name()`/`autofill_trigger_name()` in `guitars.tenancy.discovery`. The trigger is named after the function it calls, so a table tenanted on two local dimensions gets one trigger per `(column, GUC)` pair instead of two colliding on one name.

## [2.0.3] - 2026-08-14

- Repo-wide documentation shrink pass under an enforced line budget (`scripts/doc_budget.py`, wired into pre-commit).

## [2.0.2] - 2026-08-14

- [ADR-0005](docs/adr/0005-trigger-based-tenant-autofill.md) moves to **accepted**, marked not yet implemented (targeted 2.1.0).

## [2.0.1] - 2026-08-14

- Added: ADR index, three new ADRs, `docs/api-reference.md`.
- Fixed: `scripts/bump.sh` release-section placement and a false "seeded changelog" report; `LOCAL_APPS`/`--adopt`+`--force-rls` doc corrections.

## [2.0.0] - 2026-08-06

- **BREAKING:** all generated SQL now quotes/validates identifiers; `db_table` may be schema-qualified. Run `makeguitarmigrations`/`migrate` once (SQL text only, no data changes).
- **BREAKING:** unscoped-queryset deny-list is now an allow-list; `Manager.raw()` denied unscoped.
- **BREAKING:** `TenantScopeError` splits into `TenantScopeMissing`/`TenantScopeViolation`/`TenantValueError`; `TenantedManager` renamed `tenanted_manager`; `guitars.tenancy.__all__` trimmed (GUC names → `guitars.gucs`, `tenant_spec`/`local_tenant_fields` → `.spec`, lifecycle hooks → `.testing`).
- **BREAKING:** `update(_save=False, _save_all_fields=True)` now raises; `SoftDeletableModel.cls` removed.
- Added: schema-qualified `db_table` end-to-end; `guitars.tenancy.W002` pooling-leak check.
- Changed: audit-mode `Reporter` receives structured context; several internal duplications consolidated.

## [1.3.0] - 2026-08-02

- Added: behavioural test families for concurrency, drift, legacy-migration upgrade, migrate-override, property-based fuzzing, MTI owner-join at depth.

## [1.2.0] - 2026-08-02

- Added: 100%-gated branch coverage; a Python×Django×PostgreSQL CI matrix; `psycopg` extra; a runtime drift check over Django's `QuerySet` surface.

## [1.1.3] - 2026-08-02

- Fixed: a switch-off failure after a successful `hard_delete()` was swallowed, leaking the hard-deletion switch on; `update(_disable_signals=True)` mis-reported a bypass on a no-op write.

## [1.1.2] - 2026-08-02

- Fixed: `DisableSignals` race could permanently disconnect every signal receiver; `_disable_signals=True` over-suppressed all eight `DEFAULT_SIGNALS`; `update()` collapsed an empty field set into a full-row rewrite; `hard_delete()` ignored `self.db`; deny-list missed `_raw_delete`/`explain()`.

## [1.1.1] - 2026-07-31

- Changed: upgraded pinned GitHub Actions versions.

## [1.1.0] - 2026-07-31

- **Action required:** `makemigrations --check` fails on first run after upgrade by design — run it once to deliver the 1.0.0 soft-delete guard fix to existing databases.
- Added: `makeguitarmigrations --adopt`; refresh/adopt SQL forms.
- Fixed: generated migrations no longer `from guitars import sql`, carrying SQL literally instead; changed trigger-function/tenant-policy SQL now correctly re-emits.

## [1.0.2] - 2026-07-31

- Added: `actionlint` in pre-commit.

## [1.0.1] - 2026-07-31

- Fixed: a version bump merged to `main` never actually cut a release, since GitHub suppresses triggers for tags pushed with the default token; `tag-release.yml` now calls `release.yml` directly.

## [1.0.0] - 2026-07-30

First stable release. **BREAKING:** the instrument ladder shifted down one rung (behaviour-identical) to make room for multi-tenancy; `GuitarModel` keeps its name and gains tenancy.
- **BREAKING:** soft-delete rule guards changed from `= 'off'` to `<> 'on'` — see Fixed.
- Added: multi-tenancy end to end (tenant FK, scoped managers, RLS policy, `audittenancy`, tenancy-bypassed `migrate`, system checks); MTI owner-correlated policies; `docs/` and four ADRs.
- Fixed: leaking hard-deletion switch after rollback; missed `cached_property` expiry; `hard_delete()` under server-side binding; unquoted exempt-role names; stale tenant policies after model changes; a comma in a tenant pk widening RLS match; `audittenancy` blind to wrong-scope policies; pk field name used where a column was needed.

## [0.7.0] - 2026-07-06

- Added: full MTI support for dated/soft-deletable models at any depth (requires an MTI child to declare its own empty `Meta`). Not yet supported: cascading into an MTI child via an FK on its own table when `_deleted_at` lives farther up.

## [0.6.0] - 2026-07-03

- Changed: merged `publish.yml` into `release.yml`; CI restricted to `main`.

## [0.5.1] - 2026-07-03

- Changed: `publish.yml` made `workflow_dispatch`-only.

## [0.5.0] - 2026-07-03

- Added: `makemigrations` generates enforcement migrations by default; both commands accept scoping app labels; CI workflows added. Fixed: app-label validation; cross-app cascade-rule skip now warns.

## [0.3.0] - 2026-06-11

- Added: interactive release tooling under `scripts/`; `CLAUDE.md`.

## [0.2.0] - 2026-06-06

- Added: `DutarModel`; `DatedModel`/`UpdatableModel`/`HasCachedPropertyModel` exported.

## [0.1.0] - 2026-06-04

- Added: initial release — `SetarModel`, `GuitarModel`, `SoftDeletableModel`, `DisableSignals`, `makeguitarmigrations`.

[Unreleased]: https://github.com/Behnam-RK/django-guitars/compare/v2.4.0...HEAD
[2.4.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.4.0
[2.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.3.0
[2.2.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.2.1
[2.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.2.0
[2.1.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.1.1
[2.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.1.0
[2.0.3]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.3
[2.0.2]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.2
[2.0.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.1
[2.0.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v2.0.0
[1.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.3.0
[1.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.2.0
[1.1.3]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.3
[1.1.2]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.2
[1.1.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.1
[1.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.1.0
[1.0.2]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.0.2
[1.0.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.0.1
[1.0.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v1.0.0
[0.7.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.7.0
[0.6.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.6.0
[0.5.1]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.5.1
[0.5.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.5.0
[0.3.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.3.0
[0.2.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.2.0
[0.1.0]: https://github.com/Behnam-RK/django-guitars/releases/tag/v0.1.0
