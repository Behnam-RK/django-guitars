# 0006 — Inline enforcement SQL into generated migrations, not referenced by name

- **Status:** accepted
- **Date:** 2026-07-31
- **Affects:** `makeguitarmigrations` generator, `guitars.sql`'s frozen-interface obligation, every generated `RunSQL` operation

## Context

Before 1.1.0, generated enforcement migrations did `from guitars import sql` and
referenced the SQL by constant name rather than embedding its literal text. That
meant a migration file's *meaning* was a function of whichever guitars version was
installed when `migrate` ran — Django's migration model assumes replaying a fixed
history reproduces a fixed database, and a name-reference breaks exactly that: a
fresh `migrate` on 1.0.0 built `<> 'on'` soft-delete guards at migration `0003`,
while a database that had already run that same-numbered migration under 0.7 had
`= 'off'` guards. Identical history, two different live databases, invisible to
anyone reading the migration file or running `--check`.

This was not theoretical. The 1.0.0 soft-delete-guard rewrite (fixing a
rolled-back `hard_delete()` that permanently disabled soft deletion for the rest
of a connection) needed every existing database to pick up the new guard SQL. It
didn't: the digest-based idempotency check covered only the migration file's
*source*, which still just said `sql.CREATE_SOFT_DELETE_RULE.format(...)`, and the
per-table header-recognition scan short-circuited before a content digest was
ever reached. Every already-migrated database kept running the old, buggy guard
forever, silently.

Alternatives considered:
1. Keep referencing `guitars.sql` by name; rely on changelog/documentation
   discipline to tell users to hand-write a migration whenever enforcement SQL
   changes.
2. Record which guitars version generated a migration, and have `--check`/the
   generator compare that recorded version against the one installed.
3. Inline the literal SQL text into every generated `RunSQL` operation, so the
   migration file is self-contained and fixed forever once written.

## Decision

Option 3. From 1.1.0 on, generated migrations carry enforcement SQL literally —
never `from guitars import sql`. Each operation's comment header additionally
carries a `[SQL:<digest>]` identity computed from that literal text, so a future
change to what a given operation *would* emit is detected by content rather than
by trusting a recognized header forever.

Migrations already committed in the pre-1.1.0 name-referencing form are left
as-is and keep working — they are not retroactively rewritten. `guitars.sql`'s
public names accordingly become a **frozen interface**: they must never be
renamed, since existing consuming-project migrations resolve them by name at
`migrate` time forever. This is a closed, fixed-size obligation (guarded by
`tests/test_sql_interface.py`), not one that grows with every future generated
migration — nothing generated after 1.1.0 depends on the names at all.

## Why

Inlining is the only option that removes "migration meaning depends on installed
version" at its root rather than mitigating it. Option 1 is what already failed
in practice — the 1.0.0 fix's changelog entry said "hand-write a migration," and
the digest/header interaction meant that instruction wouldn't have been reachable
even if followed correctly. Option 2 would tell you a mismatch exists but still
leaves the migration's actual SQL unfixed — no way to regenerate exactly the
delta without also tracking content, which is what option 3 does directly.

Inlining does cost something: it's what put the SQL "out of reach" of any future
digest scheme for the pre-1.1.0 tail, which is why `guitars.sql`'s names must
stay frozen forever rather than for a deprecation window. That tradeoff was made
deliberately — a permanent but fixed-size, non-growing obligation on old names,
in exchange for every migration generated from 1.1.0 on being self-contained and
correct by construction.

## Consequences

**Accepted costs.**
- `guitars.sql`'s public names, and the generated operations' comment headers,
  are a frozen interface that can never be renamed without breaking `migrate` on
  a fresh database in a project holding pre-1.1.0 migrations.
- A generated migration no longer visibly says "this is a soft-delete rule" via
  a recognizable symbol — it's inlined SQL text. Comment headers exist
  specifically to keep this legible.
- The generator is slightly more complex: each operation renders its own literal
  SQL and content digest rather than a short `.format()`/name reference.

**Reversibility.** Low. Migrations already written inline are self-contained and
would survive a revert untouched, but every migration generated *after* one would
reopen the pre-1.1.0 failure mode — a file whose meaning depends on the installed
guitars version — which is the exact thing this decision exists to close. The
frozen-name obligation would also become open-ended again rather than fixed-size.

## Related
- [`docs/migrations.md`](../migrations.md) — "Idempotency has three layers" and
  the inlining narrative
- `CHANGELOG.md`'s `[1.1.0]` entry
- `tests/test_sql_interface.py` — guards the frozen `guitars.sql` names
