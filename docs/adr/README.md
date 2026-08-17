# Architecture Decision Records

Decisions that were hard to reverse and are surprising without context, recorded
at the moment they were made. Not a running log of every choice — only the ones
where the "why" would otherwise have to be reconstructed from git archaeology.

New ADR? Start from [`template.md`](template.md).

| ADR | Decision |
| --- | --- |
| [`0001`](0001-swappable-tenant-model.md) | why `GuitarModel` owns a swappable tenant FK, and what that costs |
| [`0002`](0002-force-rls-by-default.md) | why `FORCE ROW LEVEL SECURITY` is the default |
| [`0003`](0003-mti-owner-join-policy.md) | why MTI children get their own policy; includes the "RLS with no policy is default-DENY" finding |
| [`0004`](0004-unscoped-base-manager.md) | why `base_manager_name` is left unset, with the evidence |
| [`0005`](0005-trigger-based-tenant-autofill.md) | Tenant autofill is a `BEFORE INSERT` trigger, not the `pre_save` receiver, which stays as diagnostics — covering `bulk_create`, multi-row `INSERT`, `INSERT … SELECT` and raw SQL, and putting enforcement out of `DisableSignals`' reach (2.1.0) |
| [`0006`](0006-inline-generated-migration-sql.md) | why generated migrations carry enforcement SQL literally instead of referencing `guitars.sql` by name |
| [`0007`](0007-identifier-quoting-and-schema-qualification.md) | why every generated identifier is quoted/validated, and how schema-qualified `db_table` is supported |
| [`0008`](0008-unscoped-queryset-allow-list.md) | why the unscoped-queryset guard is an allow-list, and why `Manager.raw()` is denied unscoped |
| [`0009`](0009-relocated-owner-table-autofill.md) | why a tenant autofill trigger moves onto the MTI ancestor owning its column, what the shared trigger makes every descendant agree on, and why stamping the untenanted ancestor's own inserts is accepted (2.1.1) |
