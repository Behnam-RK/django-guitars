# 0002 — FORCE ROW LEVEL SECURITY by default

- **Status:** accepted
- **Date:** 2026-07-30
- **Affects:** `GUITARS_RLS_FORCE`, `guitars.sql.policy`, `audittenancy`

## Context

`ALTER TABLE … ENABLE ROW LEVEL SECURITY` does not constrain the table's **owner**.
That is not a footnote — it is the default, and it is silent. The owner's queries
come back unfiltered with no error and no log line.

Your application role owns its tables, because it runs the migrations. So a policy
shipped with `ENABLE` alone is inert against the exact process it was written to
constrain. `ALTER TABLE … FORCE ROW LEVEL SECURITY` closes it.

The two are separable for one legitimate reason: retrofitting policies onto a
populated database. Shipping them inert lets you create the policies, watch the
audit trail, and only then make them bind.

## Decision

Emit `FORCE` alongside every policy by default (`GUITARS_RLS_FORCE = True`).
`False` is the opt-out, and `makeguitarmigrations --force-rls` lands `FORCE` as a
separate second stage when the soak is clean.

The value is written into the generated migration as a **literal argument** to
`sql.create_table_rls(...)`, not read from settings at migrate time.

## Why

**A library must not ship an inert security feature.** The failure mode of
defaulting to `False` is a project that has policies in `pg_policies`, passes a
naive audit, believes it is protected, and is not. Nobody discovers that until a
cross-tenant leak. The failure mode of defaulting to `True` is a retrofit that
needs one setting flipped — discovered immediately, at the first query, by the
person doing the retrofit.

Between "silently insecure by default" and "occasionally inconvenient by default",
the choice is not close.

**Rollback is cheap.** `ALTER TABLE … NO FORCE ROW LEVEL SECURITY` is seconds, not
a migration rewrite. So the cost of the aggressive default is bounded in a way the
cost of the permissive one is not.

**The literal, not the setting.** A migration whose SQL depended on the settings in
force when it ran would produce different databases from the same migration
history, and would silently change an already-reviewed migration's meaning when
someone edited a setting. Writing the decision into the file makes the file the
record.

That choice is also what makes the second stage correct: `--force-rls` reads back
the `force=` literal each operation was generated with, so it acts only on policies
that really shipped inert. Keying on "a policy exists with no separate FORCE
operation" instead would match every policy generated with FORCE inline — which is
every policy, under the default — and emit a redundant migration per table.

## Consequences

- **`audittenancy --require-force`** exists so a deploy pipeline can fail on a
  table in the `ENABLE`-without-`FORCE` state. It is opt-in because a project
  mid-retrofit is legitimately in that state.
- **The test suite must run as a non-superuser role that owns its tables**, since
  that is the exact condition `FORCE` constrains. `scripts/postgres-init.sql`
  provisions it, and `test_the_connecting_role_cannot_bypass_rls` asserts the
  precondition — without it every RLS assertion in the suite would pass vacuously.
- **Two bypasses remain, and neither is detectable.** `SUPERUSER` and the
  `BYPASSRLS` role attribute both bypass policies unconditionally, `FORCE` or not.
  `audittenancy` cannot see them, because a bypassing role sees a database that
  looks perfectly protected. The only defence is not running the application as
  such a role, which is documentation, not enforcement.

## Related

- [ADR 0003 — the MTI owner-join policy](0003-mti-owner-join-policy.md)
- [docs/tenancy.md](../tenancy.md)
