# 0032 — `hard_delete()` removes every row it collected, or rolls back

- **Status:** accepted
- **Date:** 2026-10-07
- **Affects:** `SoftDeletableModel.hard_delete`, `HardDeletableQuerySet.hard_delete`, `_hard_delete_by_key`, `_require_removed`, `HardDeleteIncompleteError`

## Context

Instance `hard_delete()` collects the tree first, then deletes it table by table under the session switch. Through 2.14.4 nothing checked what each `DELETE` actually removed. Called on an instance of tenant A inside `tenant(label=b)`, the row policy hid the root, so Phase 1 archived nothing and the root's `DELETE` removed nothing, while its children, on a table with no policy, were removed. The transaction committed: children gone for good, the root live (#72). The MTI queryset form had the same gap per table of a chain.

## Decision

Wherever `hard_delete()` deletes rows by keys it collected first, each `DELETE` must remove exactly that many rows, or it raises `HardDeleteIncompleteError` (a `GuitarsError`) inside the walk's transaction, which rolls back everything before it. That is every table of the instance walk (an empty scope compiling a table to nothing counts as zero removed), the MTI queryset form's own table and its ancestors, and the plain queryset form's key-read path for a window or aggregate filter. The message names the table and both counts, and lists the causes without saying which applied.

## Why

- **Fail closed.** The walk's whole contract is "this tree, gone". A short count means part of it is still there, and committing the rest leaves dangling or orphaned state that nothing reports. Rolling back keeps the archive Phase 1 would have made, or nothing at all; both are recoverable.
- **Where keys are collected first, and only there.** A count needs an expectation. The plain queryset form's single `DELETE` collects nothing, so "fewer than collected" has no meaning there; a descendant table in an MTI chain holds rows only for the keys that are one, so it is not counted either.
- **No cause claimed.** A hidden row, a policy, a concurrent writer and a row already gone (a table with no soft-delete rule yet, whose row Phase 1's `delete()` really removes) produce the same count from inside the transaction. Naming one would be a guess, and a wrong one sends the reader after the wrong bug.
- **Rejected: skip the missing rows.** It commits a partial tree, which is the bug.
- **Rejected: check the instance's own row up front.** A clearer error for the reported case, and one more `SELECT`, but it misses a hidden child, a policy on an inner table and a concurrent writer.
- **Strongest objection.** A concurrent writer removing one collected row now aborts a walk that would have reached the same end state. Accepted: the walk cannot tell that writer from a hidden row, and retrying is cheap where losing rows is not.

## Consequences

**Accepted costs.** Code that called `hard_delete()` under a mismatched scope and relied on it completing now gets an error, and so does a `hard_delete()` on a table whose enforcement migration has not been generated and applied, which completed through 2.14. An empty scope raises `HardDeleteIncompleteError` where it raised Django's `EmptyResultSet`. No extra statement: the count comes with each `DELETE`.

**Reversibility.** Removing the check is one line per site, and would reopen #72.

## Related

- [ADR 0031](0031-hard-delete-resolves-its-alias-as-delete-does.md) · [`soft-deletion.md`](../soft-deletion.md) · #72
