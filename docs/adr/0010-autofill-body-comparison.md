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
only the value the trigger writes is wrong. A body that lost its `tenant.bypass`
guard stamps every `tenancy_bypassed()` insert with the last published scope; one
that lost the `position(',' in …)` guard writes `'a,b'` under a collection scope;
one that lost `NEW."col" IS NOT NULL` overwrites a tenant the caller supplied.
Each comes from an ordinary event: a hand-edit, or an older kit's template
([#29](https://github.com/Behnam-RK/django-guitars/issues/29)).

What kept it unfixed was the false-positive risk. `prosrc` stores a `$$…$$` body
verbatim, indentation and all, so a naive comparison reports every project whose
migration came from a differently-indented template. Two options: normalise the
text, or record a digest in the generated function and compare digests.

## Decision

Compare the live `prosrc` to the body rendered from the kit's own template, with
every run of whitespace collapsed to a single space on both sides
(`sql.triggers._squeeze`, beside the template it tolerates, so the compare and
the probe below cannot apply two tolerances). The expected body is sliced out of
`_CREATE_TENANT_AUTOFILL_FUNCTION` between its two `$$` delimiters
(`sql.triggers._tenant_autofill_body`), from the same slot builder the generator
writes migrations with — so there is one definition of a healthy body, not two.

On a mismatch, probe `_TENANT_AUTOFILL_GUARDS` — verbatim slices of that same
template, one per hazard — to name the missing guard and what it lets through,
falling back to a generic "not the function this kit writes" at both ends of the
probe: every guard present, and fewer than two present. A probe tolerates
whitespace but not a changed token, so a body retyped through psql (`TRUE` for
`true`) fails nearly every probe while guarding as before; below two intact
guards that is the likelier reading, and naming the rest would be alarming
claims that are all false. The probe alone drops comment-only lines first: the
squeeze would otherwise put a commented-out guard beside live code, still
reading as intact. The finding joins the existing drift bucket: warning by
default, fatal under `--require-match`, as predicate drift is.

The finding is per *function*, not per table: one `pg_proc` row serves every
model on a `(dimension, column)` pair, so an edited body is one fact, and the
tables it fills for are named inside the finding rather than repeating it once
each — N copies would inflate the heading and the `--require-match` failure count
for a single root cause.

Two things are refused rather than compared. A dimension or column carrying `$$`
closes the template's dollar quoting early, and `_tenant_autofill_slots` raises
— in the *slot builder*, because `_BARE_IDENTIFIER` admits `$`, so a `db_column`
like `a$$b` would otherwise pass the generator and emit a migration `migrate`
rejects with a bare syntax error while only the audit complained. Refusing in
the one builder both paths go through makes it a build-time error for each. And
the catalog query asks only for functions named with `AUTOFILL_FUNCTION_PREFIX`:
an application's own `BEFORE INSERT` trigger is not this command's business, and
its body should not be on the wire to establish that.

## Why

A generation-time digest is exact, but reports **unknown** for every function
generated before the change — which is precisely the population that carries a
diverged body. It would also rewrite every generated function migration in
every consuming project to gain that, and only for functions written after the
upgrade.

Whitespace-collapsed text needs no regeneration and works on the databases that
exist today. Against the alternative of probing only for the guard fragments and
never comparing the whole body: that passes a function with an *added* statement,
and an autofill trigger is a place where an addition (a second assignment, a
`RAISE`) is as damaging as a deletion.

## Consequences

**Accepted costs.** A cosmetically edited body — an added comment, a renamed
local — is reported as drift, with the generic message: a warning by default, and
an accurate statement that the database is not running what the models describe.
Diagnostics are only as good as the guard list, so
`tests/test_enforcement_identity.py` asserts every fragment is still a substring
of its template; a drifted fragment would otherwise read as missing everywhere.

**Reversibility.** Contained: the comparison is one method
(`Command._autofill_body_drift`) and one helper set in `sql.triggers`. Nothing is
written to the database or to a migration, so removing the check costs nothing
beyond losing it. Adopting a digest later is compatible — it would become the
exact path for functions generated after it, with this as the fallback.

## Related

- [ADR 0005](0005-trigger-based-tenant-autofill.md) — why autofill is a trigger at all; [ADR 0009](0009-relocated-owner-table-autofill.md) — the relocated host whose findings this reuses.
- [ADR 0006](0006-inline-generated-migration-sql.md) — why the generated SQL is inlined, hence re-indented.
- `docs/tenancy.md`'s "Auditing"; `tests/test_management_audittenancy.py::TestAWrongAutofillBody`.
