# 0035 — a cascade key through `to_field` matches on the column it holds

- **Status:** accepted
- **Date:** 2026-10-07
- **Affects:** `_CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE`, `_SOFT_DELETE_REVIVE_ARM`, `_CREATE_SOFT_DELETE_SELF_CASCADE_FUNCTION`, `introspection.to_field_refusal`, `operations._referenced_key`

## Context

Issue #59. The cascade rule matched `"<fk>" = old."<owner pk>"`. For `ForeignKey(Owner, to_field='code')` the column holds the owner's `code`, not its pk. With an integer column that archived the wrong rows (a seat holding number 2 matched the ticket whose pk was 2); with a char column `CREATE RULE` compared varchar with bigint and `migrate` failed. The 2.16.0 revive arms and the self-cascade trigger carried the same comparison. `soft_delete()` and the `.delete()` fast path had been made to refuse such a key rather than inherit it.

## Decision

The private templates take a `{referenced_key}` slot at their **matching** positions, the child's column against the owner's: the rule, the revive arms (flat and the per-key bodies retirements rebuild) and the self-cascade trigger's child match. Pairing one row across a statement's before and after images stays on `{primary_key}`: the pk is what identifies a row, even when the same statement rewrites its `to_field` value. The slot is the owner's pk for every key into the primary key, rendered through the same text, so those keys' SQL and `[SQL:]` digests do not move and nothing re-emits for them.

A column the rule cannot read, declared **below** the table holding `_deleted_at` (the rule fires on the holder and sees `old.` of that table), is **refused** through `classify_cascade`, as a joined key already was: no rule, a note naming the column, no edge in the rule-cycle graph. `soft_delete()` and the fast path then take every other `to_field` key, the blocking gap gone.

## Why

- **The column is the fact.** The child holds what the target's field holds, so the rule reads that field; nothing else makes the match right for both integer and char columns.
- **Rejected: refuse every `to_field` key.** Smaller, and the runtime guards already existed, but a supported Django shape would lose SQL-level cascade entirely, and the fix is a slot at known positions.
- **Rejected: always substitute `fk.target_field.column`.** For a key into an MTI child `target_field` is the child's parent link, not the holder's pk the template slot holds today: every such rule would re-emit.
- **Strongest objection.** An owner with one `to_field` key re-emits its whole per-owner revive trigger, because the trigger's digest covers every arm. Accepted: one owner, once.

## Consequences

**Accepted costs.** A `to_field` key whose column is a different table's, below the holder, now gets a note and no rule where 2.16 wrote a wrong one. The joined form still refuses `to_field` outright.

**Reversibility.** Restoring the pk comparison would reintroduce wrong rows or a failing `migrate` for these keys.

## Related

- [ADR 0018](0018-self-referential-cascade-trigger.md) · [ADR 0025](0025-joined-cascade-rule.md) · [ADR 0033](0033-one-revive-trigger-per-owner.md) · [`soft-deletion.md`](../soft-deletion.md) · #59
