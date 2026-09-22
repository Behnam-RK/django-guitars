# 0022 — enforcement is generated per database, not per model

- **Status:** accepted
- **Date:** 2026-09-22
- **Affects:** `guitars.routing`, `guitars.introspection.owner_arms`, `guitars.introspection.owned_tenancy_refusals`, `guitars.management.enforcement`, `guitars.tenancy.discovery`, `guitars.operations.RetireEnforcement`

## Context

Issue #52. A consumer routed one app to a ClickHouse alias through an ordinary Django database router and kept `tenanted_manager()` on its model, wanting the Python half — scoped querysets, `TenantScopeMissing` — which is backend-independent. `makeguitarmigrations` discovers work from the *shape* of the model and had never heard of a router, so it wrote `CREATE TRIGGER … EXECUTE FUNCTION` and `CREATE POLICY` for that model, and `migrate --database clickhouse` died in ClickHouse's parser. Until then the two halves of a tenanted manager were inseparable, so the choice was a manager the migration system breaks on, or a bespoke scoped manager reimplementing the same semantics beside it.

The anchor fact is that `django.db.migrations.operations.special.RunSQL.database_forwards` already reads `router.allow_migrate(schema_editor.connection.alias, app_label, **self.hints)`. `migrate` asks the router about every enforcement operation this kit emits; it asks with `hints = {}`, and the reporter's router answers *yes* for that app on that alias — that is what puts the tables there. So the generator asks about model shape while `migrate` asks about routing, and only the generator can close the gap.

Three options were on the table, all named in the issue: consult the router; a `GUITARS_TENANT_ENFORCE_VENDORS` setting defaulting to `('postgresql',)`; or a per-model `tenanted_manager(..., enforcement=False)`.

## Decision

A leaf module `guitars.routing`, beside `guitars.local_apps` and for its reason — `tenancy` and `management` both need it. `migrates_to_postgresql(model)` is `True` when **any** alias `router.allow_migrate_model` accepts for that model has `vendor == 'postgresql'`, and short-circuits to `True` when `settings.DATABASE_ROUTERS` is empty. `vendor_skip_note(model)` renders one string per model whoever asks — a per-caller suffix made the generator and tenancy discovery print two spellings of one skip, which the report's equality dedupe could not collapse.

Every walk that decides whether to *emit* asks it: `_build_operations`' model loop, `_table_app_labels`, `_cascade_key_maps`, `_is_cascade_candidate` and `_is_owned_candidate` (both ends of each relation, not just the owner), `command.py`'s singleton-function scan, and `tenancy.discovery`'s `app_coverage`, `owner_autofill_notes` and `_dimensions`. It is also asked by `introspection.owner_arms`, `introspection.owned_tenancy_refusals` and `models.soft_deletion._declared_owning_fields`, so the generator and `hard_delete()` cannot disagree. `audittenancy` and `sweepowned` raise `CommandError` on a non-PostgreSQL connection; `RetireEnforcement.database_forwards` returns without executing.

Walks that answer "does any model still hold this table name" are deliberately **not** gated: `scanning.live_tables`, `operations._live_names`, `command._index_reverse_relations` and `tenancy.discovery._owner_column_claims`. `guitars.E003` is not gated either. `introspection._rule_update_edges` **is** gated, at both ends of every edge.

## Why

`allow_migrate_model` over `db_for_write`, which the issue suggested: `db_for_write` answers where a *query* goes, returns one alias, and `ConnectionRouter` falls back to `DEFAULT_DB_ALIAS` when no router implements it — so a read-replica router, which implements `db_for_read`/`db_for_write` and not `allow_migrate` and is the commonest router there is, would read as moving tables it does not move. It can also return a string absent from `DATABASES`, and `connections[<that>]` raises `ConnectionDoesNotExist` inside a command that opens nothing today. Iterating `connections` makes every alias configured by construction. Not `_meta.can_migrate` either, though `migrate` consults it too: it is `False` for an unmanaged model, and `_table_app_labels` deliberately lets an unmanaged model host as a fallback.

`any` over `all` because the two failures are not symmetric. Emitting where the DDL cannot apply aborts a migration, loudly, on a database the operator is already looking at. Withholding where it can leaves `.delete()` permanently deleting rows on PostgreSQL, silently — the one direction this kit must never fail in.

Against a setting: it is a second place to say where a model lives, free to disagree with the router that actually puts it there, and `RunSQL` cannot consult it, so the two answers would diverge exactly where it matters. Against `enforcement=False`: it is tenancy-only, and a `DutarModel` on ClickHouse breaks on the `_updated_at` trigger with no manager to hang a flag on — while every call site would have to remember it.

The strongest objection to what was chosen is that a router is a runtime object and this consults it at *generation* time, when no database need be reachable. That is why the predicate reads `connections[alias].vendor`, a class attribute on the backend wrapper, and why the `DATABASE_ROUTERS` short-circuit comes first: a project with no routing constructs no wrapper, imports no backend module, and cannot raise from a half-configured secondary alias. The no-op is a proof, not an argument, and a test asserts it by making every connection access raise.

The ungated walks are the 2.9.1 lesson (ADR 0020) resolved in the other direction. A routed-away model's table is still *held* — gating `live_tables` would make recorded coverage read as a deleted model and start emitting retirements, which is the failure this release exists to avoid. Gating the reverse-relations index is what lost a cascade rule entirely in 2.9.1, so each consumer applies the gate where it decides to emit instead. `_owner_column_claims` fails toward emitting, so dropping a claimant there could turn a refusal into a rule. And `E003` describes what Django's `Collector` does, which is backend-independent: the chain is destroyed on ClickHouse too, and silencing the error would hide real data loss for a model that genuinely loses rows.

`_rule_update_edges` goes the other way, and it was nearly left ungated on the ground that an extra edge only adds refusals and so fails safe. That is wrong, and the module's own comment already said so: an invented edge closes a cycle that cannot form and takes the legitimate rule pointing back down with it. A routed-away table sitting on a cycle would have withheld a rule between two PostgreSQL tables, which is the direction this kit must never fail in. Routing, never *scoping* — a scoped run still means the rule exists, so scope must not be read there.

## Consequences

**Accepted costs.** An **MTI chain split across backends** is out of scope and is not guarded. Where a child is routed away while its ancestor stays, the cascade keys the child's foreign keys produce name the ancestor's table and the referrer's, neither of which is the routed-away one, so retirement's positive-evidence test passes and a live rule on the PostgreSQL alias can be dropped. The shape cannot function regardless — the child's table carries a foreign key into the ancestor's, across two servers — so this is a misconfiguration the kit would ideally name rather than act on, and naming it is left for the release that meets one. A model routed onto a PostgreSQL alias *and* a non-PostgreSQL one still gets its DDL, and `migrate --database <the other>` still aborts on it: `RunSQL` asks with no model hint, and carrying `hints={'model_name': …}` on every operation would re-digest every enforcement migration in every consuming project. The project's own router narrows that case. A migration already generated for a now-routed-away model stays on disk, is not dropped, and must be deleted by hand — deleting it is not this command's call, since a table mapping to nothing is a deleted model on one reading and a scoped run on another. And the gate stops the generator writing DDL that cannot apply without making soft deletion *work* on another backend: a routed-away `SetarModel` has no rule stamping `_deleted_at`, so `.delete()` really deletes. That shape is documented as unsupported rather than guarded, because guarding `hard_delete()` alone would dress it up as supported.

**Reversibility.** Easy. The predicate is one function with one short-circuit; removing the calls restores the old behaviour, and no generated SQL, header, `[SQL:…]` identity or frozen name changed — a project with no router generates byte-identical migrations before and after.

## Related

- [ADR 0020](0020-proxy-models-are-not-mti-children.md) — the model-level exclusion whose call-site sweep this one repeats.
- [ADR 0011](0011-owner-side-soft-delete-ownership.md) — why the generator and `hard_delete()` must read one answer.
- [`docs/migrations.md`](../migrations.md)'s "Routing" · [`docs/tenancy.md`](../tenancy.md)
