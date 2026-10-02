# 0025 — A cascade key on an MTI descendant takes a joined rule

- **Status:** accepted
- **Date:** 2026-10-02
- **Affects:** `makeguitarmigrations`, `introspection.rule_update_cycle_edges`, `soft_delete_related_*`

## Context

A `CASCADE` key declared on an MTI descendant's own table, while `_deleted_at` lives on an
ancestor, got no rule: the flat form is `UPDATE <child> SET _deleted_at`, and the child has no such
column. The generator skipped it with a note, so only Django's Python `Collector` archived those
children. A raw `DELETE` or a bulk update left them live under an archived parent, which fails
toward **exposing** data, and a `soft_delete()` that leans on the rules (#55) would inherit it.

## Decision

Emit a **joined** rule in the existing cascade family. It fires on the target as before and updates
the ancestor: `UPDATE <ancestor> SET _deleted_at = new._deleted_at WHERE <ancestor pk> IN (SELECT
<child parent link> FROM <child> WHERE <fk> = old.<pk>) AND _deleted_at IS NULL`. Every table in a
chain stores one pk value, so the descendant's own parent-link column names the ancestor's row
directly, one subselect however deep. The revive twin ([ADR 0024](0024-inverse-cascade-revive-rules.md))
gets the same form.

The cycle graph files the edge against the table the rule **updates**, the ancestor. A descendant
cascading to its **own root** is the one-node cycle, and is refused with the usual note.

## Why

- **Same family, same header.** The key, header, rule name and `[SQL:...]` identity are unchanged,
  so no frozen interface moves; only the body differs. Nothing was recorded for these keys before,
  so an upgrade is a plain `CREATE`.
- **Not a trigger for the self-root shape.** That is the [ADR 0018](0018-self-referential-cascade-trigger.md)
  form, and worth doing, but a second new object family in one release is a second thing to review.
  Refusal is the safe default; #55's `soft_delete()` is to raise on it, not leave children live.
- **Strongest objection:** the rule archives the whole ancestor row, so a sibling descendant sharing
  it is archived too. That is what the MTI redirect rule already does to a `DELETE` on any one table.

## Consequences

**Accepted costs.** The subselect and the update run under the **invoker's row-level security**, as
every cascade does: a session that cannot see a tenanted descendant cannot archive through it.
The ancestor's `_updated_at` moves when it is archived, pinned in `tests/test_cascade_join.py`.

**Reversibility.** A retirement's `reverse_sql` **refuses** for a joined key: the key names no
ancestor, and the flat template would be built against a table without `_deleted_at`.

## Related

- [MTI](../mti.md) · [ADR 0013](0013-cross-app-migration-dependency-edges.md) · [ADR 0018](0018-self-referential-cascade-trigger.md)
