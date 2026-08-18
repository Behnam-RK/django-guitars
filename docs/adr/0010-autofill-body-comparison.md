# 0010 — `audittenancy` compares an autofill function's body, whitespace-collapsed

- **Status:** accepted
- **Date:** 2026-08-18
- **Affects:** `guitars.management.commands.audittenancy`, `guitars.sql.triggers`

## Context

Through 2.1.1 the audit compared expected autofill function *names* against
`pg_proc.proname` and never looked inside. The policy check sitting beside it
already knew presence is not enough — it compares `tenant.*` GUCs and
`pg_depend` column references precisely because a policy can be present and
wrong.

An autofill function can be present and wrong the same way, and nothing else
notices: the policy is intact, reads and cross-tenant writes are refused, and
only the value the trigger writes is wrong. A body that lost its
`tenant.bypass` guard stamps every `tenancy_bypassed()` insert with the last
published scope; one that lost the `position(',' in …)` guard writes `'a,b'`
into the column under a collection scope. Both come from an ordinary event: a
hand-edit, or a database migrated by an older kit whose template differed
([#29](https://github.com/Behnam-RK/django-guitars/issues/29)).

What kept it unfixed was the false-positive risk. `prosrc` stores a `$$…$$`
body verbatim, including the indentation the generator gave it when it inlined
the SQL into a migration — so a naive text comparison reports every project
whose migration came from a differently-indented template. Two options were on
the table: normalise the text, or record a digest in the generated function at
generation time and compare digests.

## Decision

Compare the live `prosrc` to the body rendered from the kit's own template,
with every run of whitespace collapsed to a single space on both sides
(`audittenancy._squeeze`). The expected body is sliced out of
`_CREATE_TENANT_AUTOFILL_FUNCTION` between its two `$$` delimiters
(`sql.triggers._tenant_autofill_body`), from the same slot builder the
generator writes migrations with — so there is one definition of a healthy
body, not two.

On a mismatch, probe `_TENANT_AUTOFILL_GUARDS` — verbatim slices of that same
template — to name the missing guard and the hazard it lets through, falling
back to "not the function this kit writes" when every guard is present. The
finding joins the existing drift bucket: warning by default, fatal under
`--require-match`, the same severity as predicate drift.

## Why

A generation-time digest is exact, but reports **unknown** for every function
generated before the change — which is precisely the population that carries a
diverged body. It would also rewrite every generated function migration in
every consuming project to gain that, and only for functions written after the
upgrade.

Whitespace-collapsed text needs no regeneration and works on the databases that
exist today. Its accepted weakness is the other direction: it cannot see past
formatting, so a body reformatted *and* changed inside a string literal would
have to differ in that literal to be caught — which it does, since every guard
lives in one.

Against the alternative of probing only for the guard fragments and never
comparing the whole body: that passes a function with an *added* statement, and
an autofill trigger is a place where an addition (a second assignment, a
`RAISE`) is as damaging as a deletion.

## Consequences

**Accepted costs.** A cosmetically edited body — an added comment, a renamed
local — is reported as drift, with the generic message. That is a warning by
default and an accurate statement that the database is not running what the
models describe. Diagnostics are only as good as the guard list, so
`tests/test_enforcement_identity.py` asserts every fragment is still a
substring of the template it came from; a fragment that drifted would otherwise
be reported missing on every healthy database.

**Reversibility.** Contained: the comparison is one method
(`Command._autofill_body_drift`) and one helper pair in `sql.triggers`. Nothing
is written to the database or to a migration, so removing the check costs
nothing beyond losing it. Adopting a digest later is compatible — it would
become the exact path for functions generated after it, with this comparison as
the fallback for the rest.

## Related

- [ADR 0005](0005-trigger-based-tenant-autofill.md) — why autofill is a trigger at all.
- [ADR 0009](0009-relocated-owner-table-autofill.md) — the relocated host whose findings this reuses.
- [ADR 0006](0006-inline-generated-migration-sql.md) — why the generated SQL is inlined, hence re-indented.
- `docs/tenancy.md`'s "Auditing"; `tests/test_management_audittenancy.py::TestAWrongAutofillBody`.
