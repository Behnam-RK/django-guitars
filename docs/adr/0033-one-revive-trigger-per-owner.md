# 0033 — one revive trigger per owner table, leaving at its first test

- **Status:** accepted
- **Date:** 2026-10-07
- **Affects:** `_CREATE_SOFT_DELETE_REVIVE_OWNER`, `_SOFT_DELETE_REVIVE_ARM[_JOINED]`, `HEADER_SOFT_DELETE_REVIVE_OWNER`, `_revive_operations`, `_retired_cascade_operations`, `ExistingOperations.soft_delete_revive_owner`
- **Supersedes:** the one-trigger-per-key shape of [ADR 0024](0024-inverse-cascade-revive-rules.md)

## Context

[ADR 0024](0024-inverse-cascade-revive-rules.md) gave every cascade key its own statement-level revive trigger on the owner table, and accepted that each fires on every `UPDATE` of that table. A consumer upgrading from 2.8.0 to 2.14.2 measured it (#70): 16 revive triggers on one table, a single-row `UPDATE` about 15% slower, about half of that the triggers. Each ran a full `UPDATE … FROM` join over the transition tables, whether or not the statement revived anything. `scripts/bench_revive.py` measures the shape on 16 keys: +0.245 ms an `UPDATE` per key against +0.047 ms per owner, over no trigger at all.

## Decision

- **One trigger and function per owner table**, `soft_delete_revive_on_<owner>`, every cascade key's revive an arm of its body, flat and joined forms alike. The body asks once, behind the hard-deletion guard, whether any row went from archived to live; only then do the arms run.
- **Keyed `(owner_table,)`**, and hosted **for life** by the app whose migrations created it: first the owner table's app, as retirement's is, or, for an owner outside `LOCAL_APPS`, the smallest-label app contributing an arm. A computed host could move, and the new one's `DROP TRIGGER` would then run before the old one's `CREATE` on a fresh `migrate`. Only a creator leaving `LOCAL_APPS` moves it, and the new host's migration then depends on every earlier create. A routed-away owner gets none ([ADR 0022](0022-router-gated-enforcement.md)). Its arms come off the registry-wide sweep, one per relation, so an MTI descendant in another app contributes to its ancestor's owner. Its `[SQL:]` digest covers the whole body: a key added or gone re-emits it, and the trigger is retired only with the owner's last key.
- **Every recorded per-key revive is retired** by the upgrade, a routed-away owner's excepted, in the app writing its owner's trigger, `IF EXISTS` over both tables' spellings ([ADR 0029](0029-retiring-a-deleted-childs-revive-trigger.md)), ordered after its create ([ADR 0021](0021-retirement-ordered-against-its-create.md)). Its reverse rebuilds that trigger, flat or joined, so the migration unapplies. The per-key headers and scanners stay, to read history.

## Why

- **The cost was per key and paid by every `UPDATE`.** One trigger fires once, and the early exit makes a statement that revives nothing pay one probe of the transition tables.
- **Keyed by the owner, not the key.** The trigger fires on the owner, so that is what decides how many fire. A per-key header kept beside it would have two families claiming one object.
- **Rejected: keep per-key triggers, add the early exit to each.** Far less generator work, a changed body re-emitting each, but still N trigger invocations a statement.
- **Rejected: measure first.** The issue's numbers already attribute the cost; the benchmark confirms the shape rather than choosing it.
- **Strongest objection.** A child table dropped without its retirement (#63) used to fail every `UPDATE` of the owner, loudly. Now it fails only an `UPDATE` that revives a row, which is rare and may first happen in production. Accepted because the generator still sees it: deleting the child's model removes its arm, the owner's digest moves, and `makemigrations --check` in a run including the owner's app fails until it is regenerated. A run scoped away from that app names the trigger as missing, out of date or no longer called for, comparing digests.

## Consequences

**Accepted costs.** Every consumer gets one enforcement migration on upgrade in each app hosting an owner, retiring its per-key revives and creating the per-owner ones; `--check` is red until it is generated. Changing one key's arm rebuilds the owner's whole function. The two MTI keys that used to clash on a per-key name (a model keyed to both a parent and its child) no longer do: both are arms of one function.

**Reversibility.** The upgrade migration unapplies to the per-key triggers. Returning to them in code would need the same transition the other way.

## Related

- [ADR 0024](0024-inverse-cascade-revive-rules.md) · [ADR 0025](0025-joined-cascade-rule.md) · [ADR 0029](0029-retiring-a-deleted-childs-revive-trigger.md) · [`soft-deletion.md`](../soft-deletion.md) · [`migrations.md`](../migrations.md) · #70
