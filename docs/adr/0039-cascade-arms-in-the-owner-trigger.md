# 0039 — the cascade is an arm of the owner's trigger, and the owned rule is retired

- **Status:** accepted
- **Date:** 2026-10-09
- **Affects:** `_SOFT_DELETE_ARCHIVE_ARM[_JOINED]`, `_CREATE_SOFT_DELETE_REVIVE_OWNER_FUNCTION`, `_cascade_operations`, `_owned_operations`, `_retired_cascade_operations`, `introspection.dollar_refusal`, `sweepowned`
- **Amends:** [ADR 0014](0014-statement-level-owned-sweep.md), [ADR 0024](0024-inverse-cascade-revive-rules.md), [ADR 0033](0033-one-revive-trigger-per-owner.md)

## Context

The second half of #80. After [ADR 0038](0038-updated-at-is-a-row-trigger.md) the issue's remaining cost is the rules: every cascade and owned `ON UPDATE` rule is expanded and planned on **every** `UPDATE` of its table, whether or not `_deleted_at` moves, because a rule's `WHERE` is read at execution time and planning is paid before it. One rename of a `Band` became twelve statements over eight tables, and each cascade level multiplies it ([ADR 0024](0024-inverse-cascade-revive-rules.md) measured 127 query trees for a depth-6 chain). The revive half had already left the rewriter for a trigger; the archive had not.

Alternatives: a row trigger per key with a `WHEN` on the transition (near-free on a plain `UPDATE`, but one `UPDATE` of a child per archived parent row on a bulk archive, and one more trigger per key); keeping the rules and trimming their planning (the expansion itself cannot be skipped).

## Decision

- **Each cascade key's archive is an arm of its owner's existing trigger**, beside its revive arm, behind its own `EXISTS` and a first one that a plain `UPDATE` stops at. Flat and joined forms. The arm reads the owner's *before* image for the key and the *after* image for the stamp, as the rule read `old.` and `new.`, and keeps its `AND _deleted_at IS NULL` and the parent's own timestamp ([ADR 0023](0023-cascade-stamps-its-parents-timestamp.md)). A child's `UPDATE` fires its own owner trigger, so a chain goes down with no depth guard.
- **No cascade rule is written.** A rule recorded for a key is **retired**, the key still cascading or not, in the app writing the owner's trigger, after it; its reverse rebuilds the rule as it was written. It is dropped only where that trigger carries the key's arm: a refused arm, or no trigger, keeps the rule, which is then the only thing cascading.
- **The owned rule is retired; the sweep stays**, byte for byte. [ADR 0014](0014-statement-level-owned-sweep.md) showed the rule stamps a strict subset of what the sweep does, so nothing is lost. A rule recorded for a key whose sweep is refused is kept for the same reason.
- **The trigger is renamed `soft_delete_cascade_on_<owner>`**, a new family (`HEADER_SOFT_DELETE_CASCADE_OWNER`, recorded as `soft_delete_cascade_owner`), since it archives as well as revives. The revive-only `soft_delete_revive_on_<owner>` (`soft_delete_revive_owner`, kept to read history) is **retired where the new one is written**, hosted by the app that created it and ordered after it; its reverse rebuilds it, and a retirement whose owner has no cascade key left refuses its reverse, as any does. It is kept where the new trigger is not written (every arm refused). The two reviving one row for the length of a migration is harmless: the provenance test makes whichever runs second a no-op. Later re-emissions are `CREATE OR REPLACE FUNCTION` and `CREATE OR REPLACE TRIGGER` (PG 14), no `DROP`.
- **A statement archiving a row while rewriting its primary key is refused** where a live child still holds a key it vanished from, as the self cascade is ([ADR 0018](0018-self-referential-cascade-trigger.md)): the rule read `old.` per row, the arm pairs a row across the statement on its pk, which that statement moves, so the child would stay live under an archived parent. One left join is still the plain `UPDATE`'s only probe; an archived row whose key moves with nothing live below it is not refused.
- **A name carrying `$$` refuses its key** (`introspection.dollar_refusal`, through `cascade_refusal`), for the arm is spliced into a dollar-quoted body. Classified `REFUSED`, so the `delete()` fast path and `soft_delete()` stand aside and Django cascades it in Python. The rule needed no dollar quoting: left to the generator's own check alone, the key would read as covered and leave its children live.
- **Unchanged:** the multi-table cycle refusal and the self-cascade trigger (a follow-up), the owned refusals, `hard_delete()` and every predicate it shares with the generator.
- `sweepowned` follows a relation the database holds an owned **rule or sweep** for. A scan no longer flags an app for the digest guard at the time it reads a trigger-family retirement ([ADR 0021](0021-retirement-ordered-against-its-create.md) point 9): the one-time retirement of every owned rule would otherwise waive it for ever.

## Why

A trigger takes no part in rewriting, so its cost is one probe of the transition tables per statement, whatever the cascade's depth. Folding into the trigger that already exists adds no fire: the statement trigger a revive needs is there on every owner. Set-based on a bulk archive, where a row trigger per key is not.

The strongest objection is robustness, taken below. The strongest objection to retiring rather than leaving a rule beside the arm is that both would be correct: the rule's `IS NULL` makes whichever runs second a no-op. It was refused because the rule is the cost.

## Consequences

**Accepted costs.** A rule records a dependency on every table and column it names, so a `RemoveField` or `DeleteModel` of a child failed at `migrate` (`rule … depends on column`) and `RetireEnforcement` was the answer. A plpgsql arm records none: dropping or renaming a child's table or foreign-key column leaves the owner's trigger naming what is gone, and it fails an archive of that owner until `makeguitarmigrations` re-emits it (the digest moves, so `--check` is red). The same trade [ADR 0033](0033-one-revive-trigger-per-owner.md) made for the revive. `DISABLE TRIGGER` and `session_replication_role = replica` now switch a cascade off where a rule survived both. Every consumer gets a migration per app hosting an owner or a rule, taking `SHARE ROW EXCLUSIVE` on those tables until it commits (writes block, reads do not): set `lock_timeout` on a large app. The rename costs `DROP TRIGGER` once on each owner table, which holds `ACCESS EXCLUSIVE` until the migration commits: the lock [ADR 0038](0038-updated-at-is-a-row-trigger.md) removed from `_updated_at`'s replacement, accepted here for a name that matches what the trigger does.

A rule's action ran with the privileges of its owner and an arm runs as the invoker, which is the same answer under policies keyed on the `tenant.*` GUCs. An `rls_exempt_<role>` policy is role-keyed and `FOR SELECT`, so a role it exempts reads every child either way and writes none, as before; a role whose scope hides a child leaves it live, fail-safe, as the rule did.

**Reversibility.** The migration unapplies: the rules come back as written, the cascade trigger goes, and the revive-only one is rebuilt. Going back in code would need the transition the other way, and the generator's retirements reversed.

## Related

- [ADR 0014](0014-statement-level-owned-sweep.md) · [ADR 0018](0018-self-referential-cascade-trigger.md) · [ADR 0021](0021-retirement-ordered-against-its-create.md) · [ADR 0024](0024-inverse-cascade-revive-rules.md) · [ADR 0033](0033-one-revive-trigger-per-owner.md) · [ADR 0038](0038-updated-at-is-a-row-trigger.md) · #80
