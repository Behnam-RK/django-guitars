# 0007 — Quote and validate every generated identifier; support schema-qualified `db_table`

- **Status:** accepted
- **Date:** 2026-08-09
- **Affects:** `sql/triggers.py`, `sql/soft_delete.py`, `sql/policy.py`, `db_table` handling end-to-end, every generated enforcement migration (digest bump)

## Context

`sql/policy.py` validated and quoted every identifier it interpolated
(`_bare()`, `_quote_ident`, `_quote_literal`) and raised a build-time error on a
hostile table name. `sql/triggers.py` and `sql/soft_delete.py` did raw
`.format(table=...)` with **no** validation, fed the same
`model._meta.db_table` value: a mixed-case table name generated DDL targeting
the wrong (lowercase-folded) relation; a reserved word (`user`, `order`)
produced a syntax error; a schema-qualified name (`analytics.events`) built an
invalid identifier by splicing the dot straight in.

There was also a direct contradiction: `audittenancy` went to real trouble to
disambiguate same-named tables across Postgres schemas, treating
schema-per-tenant as "an ordinary shape" — while `sql/policy.py`'s `_bare()`
refused schema-qualified names outright. One of the two was wrong.

Alternatives considered:
1. Leave `triggers.py`/`soft_delete.py` unvalidated, document the limitation
   (plain lowercase, unqualified names only).
2. Validate/quote in `triggers.py`/`soft_delete.py` but continue refusing
   schema-qualified names, resolving the `audittenancy` contradiction by
   restricting `audittenancy` instead.
3. Extend `policy.py`'s validation/quoting machinery to every SQL-emitting
   module, and add genuine schema-qualified `db_table` support end-to-end.

## Decision

Option 3. Every generated trigger, rule, and policy statement now quotes and
validates its SQL identifiers through one shared module. `db_table` may be
schema-qualified (a bare `'schema.table'`, or Django's own pre-quoted
`'"schema"."table"'`), resolved end-to-end through name derivation, generated
DDL, and existence-check queries (fixing `CHECK_RULE_EXISTS_ON_TABLE`, which
previously matched only the bare `pg_rules.tablename` column and so reported
"no rule" forever for a correctly-configured schema-qualified table). Rule
names derived from long table names are truncated with a content-hash suffix
once they'd exceed Postgres's 63-byte identifier limit, so two long names can
no longer silently collide onto the same rule.

This is the one 2.0 milestone that changes emitted SQL text for the common
case too (not just new capability) — every consuming project generates exactly
one new enforcement migration on upgrading, re-issuing the now-quoted/
schema-aware forms of its triggers, rules, and policies. No column, data, or
behavior for an already-working lowercase/unqualified name changes; only the
SQL text's quoting does.

## Why

Option 1 leaves a real correctness bug (silent wrong-table DDL on a mixed-case
name) permanently unfixed and codifies an artificial restriction Django itself
doesn't impose. Option 2 resolves the contradiction in the direction that
removes capability `audittenancy` already assumed was normal, rather than
extending the weaker module to match the stronger one — schema-per-tenant is a
real, already-supported deployment shape on the audit side; refusing it on the
generation side would have been the actually-inconsistent position.

Digest churn (one migration per app on upgrade) is an accepted, one-time cost
of fixing identifier handling for real — a quieter fix that avoided the churn
wasn't available, since the SQL text genuinely changes shape for every
already-migrated table.

## Consequences

**Accepted costs.**
- Every 2.0-upgrading project's `makeguitarmigrations` generates one new
  enforcement migration per app with an enforced table — a mandatory,
  called-out-in-advance step, not optional.
- Schema-qualified support is scoped to what `audittenancy` already assumed
  (schema-per-tenant), not arbitrary cross-schema relationships beyond what
  existing MTI/cascade logic needs to resolve.
- Four dead `CHECK_*` constants (`CHECK_TRIGGER_FUNCTION_EXISTS`,
  `CHECK_PARENT_TRIGGER_FUNCTION_EXISTS`, `CHECK_TRIGGER_EXISTS_ON_TABLE`,
  `CHECK_RULE_EXISTS_ON_TABLE`) were removed as part of this milestone, having
  had zero consumers — a 2.0 major-version-only removal, since `guitars.sql`
  names are otherwise frozen (see ADR-0006).

**Reversibility.** Low for the quoting itself (reverting reopens the mixed-case/
reserved-word bugs). The schema-qualification support is additive and could be
narrowed later without breaking already-generated migrations, since a bare
unqualified name is a strict subset of what's now accepted.

## Related
- [ADR 0006](0006-inline-generated-migration-sql.md) — why the frozen-name
  obligation this milestone's dead-constant removal interacts with exists
- `docs/mti.md` — the one remaining schema-qualification caveat (own-table
  trigger resolution via `search_path`)
- `CHANGELOG.md`'s `[2.0.0]` entry, M4 section
