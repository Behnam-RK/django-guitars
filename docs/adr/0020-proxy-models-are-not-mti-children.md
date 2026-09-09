# 0020 — a proxy model is not an MTI child, in both directions

- **Status:** accepted
- **Date:** 2026-09-09
- **Affects:** `guitars.introspection.is_mti_child`, `Command._index_reverse_relations`,
  `OperationsMixin._build_operations`, the two MTI notes

## Context

Django gives a proxy model a populated `_meta.parents` and an empty `local_fields`, the same
pair a real multi-table-inheritance child shows for a column its ancestor declares. The kit's
`is_mti_child` reads exactly that pair, so through 2.9.0 it answered **true** for a proxy, and
`column_owner` then resolved the proxy's own concrete model. Every MTI operation a proxy earned
therefore named its own table as its parent.

Reported as issue #45, which read the result as a redundant second update. It is worse in one
direction and inert in another. The rule half is a no-op: both templates name their rule
`soft_delete`, PostgreSQL dedupes a rule on its name per table, and for a proxy the two render
byte-identically. The trigger half breaks `migrate` outright — triggers are not deduped by name
and the plain form has no `OR REPLACE`, so a project declaring a proxy over a `DutarModel` or
below could not apply its enforcement migration. A third symptom went unrecorded:
`needs_parent_function` walks its own model list, so a proxy alone forced the parent
trigger-function migration into a project with no MTI.

Fixing the predicate alone leaves the proxy walking the cascade, owned and tenancy families in
`_build_operations`, where every key it produces already belongs to its concrete model. Skipping
proxies there alone leaves `needs_parent_function` wrong. Both were needed.

Skipping proxies in that loop then opened a worse defect than the one being fixed.
`Field.related_model` for `ForeignKey(SomeProxy)` **is the proxy** — Django normalises that in
`_relation_tree`, not in the field — so `reverse_relations_mapping` filed the cascade arm under
a model owning no table, and the proxy was the only model reaching it. Through 2.9.0 the proxy's
own colliding trigger masked this by aborting the migration; skipping proxies without more turns
that loud failure silent, the rule absent with `--check` green and a raw `DELETE` on the owner
leaving every child live.

## Decision

**Three guards, not one.**

1. `is_mti_child` answers `False` for a proxy, so every caller agrees — including
   `needs_parent_function`, which no loop filter can reach.
2. `_build_operations` skips proxies at the head of its model walk, as `_table_app_labels` and
   the tenancy discovery walk already do.
3. `reverse_relations_mapping` is keyed on `field.related_model._meta.concrete_model`, and a
   proxy is skipped while indexing since its `get_fields()` is its concrete model's.

A pre-2.9.1 migration carrying such an operation is **named, not retired**: a recorded MTI key
no local model reaches through an ancestor, and whose table still maps, is reported with the
instruction to delete the operation. It gives both readings — a proxy, or a model flattened out
of inheritance — and sends the reader to the database rather than claiming what is live.

## Why

**Both code guards, because they answer different questions.** The predicate is asked by callers
walking their own model lists; the loop skip covers families that never ask it. Either alone
leaves a live defect, and both are one line.

**Keyed on the concrete model rather than un-skipping proxies in the loop.** Un-skipping restores
the original bug. Normalising the key is what Django itself does in `_relation_tree`, what the
`Collector` does, and what the owned family already does through `column_owner` — it makes the
generator agree with the three parties it must agree with, rather than adding a fourth rule.

**Named rather than retired.** Nothing can be dropped on positive evidence: whether the object
is live turns on the family and on `--adopt`, and the record cannot tell a proxy's artefact from
a flattening. Retiring on a guess destroys a live trigger; a note costs a line. This follows
2.9.0's retirement rule rather than inventing a second.

**No refusal, unlike `guitars.E003`.** A proxy is ordinary Django and the shape is handled
correctly now. Both notes stay advisory rather than failing `--check`, the broken file failing
`migrate` loudly on its own: the warning adds guidance, not the only signal.

## Consequences

**Accepted costs.** A proxy over a *real* MTI child recorded the very key its concrete child
still requires, so the **set difference** is blind to it. The evidence is elsewhere: that
migration carries the header twice, one table taking one such operation, so the second note
reads the repeat off the file and names the file — which the record-driven one, working from a
record with no provenance, cannot. Neither can edit a migration.

A **legacy cross-app hazard** this branch surfaces without causing. Through 2.9.0 an FK-to-proxy
cascade rule was written into the *proxy's* app while retirement is attributed to the app owning
the table the rule fires on. Those differ, and the retirement carries no edge to the creating
migration, so a fresh `migrate` can order the `DROP RULE` first and abort with
`rule … does not exist`. It reproduces identically on 2.9.0; only what puts the two in different
apps is proxy-specific. New rules land in the concrete model's app either way, so nothing
generated from 2.9.1 on reaches it.

The record-driven note also stays silent where the orphaned object is genuinely live but its
table maps to no local model — 2.9.0's positive-evidence rule, deliberately kept.

**Reversibility.** Cheap. The three guards are independent one-liners and the note is advisory.
Undoing any of them reintroduces a defect this ADR names, so the reason to revisit would be a
Django release that changes what `_meta.parents` or `Field.related_model` mean for a proxy.

## Related

- [ADR 0015](0015-refuse-soft-deletable-mti-orphans.md) — the other MTI shape the kit refuses.
- [ADR 0019](0019-migration-lifecycle-objects.md) — the positive-evidence rule these notes follow.
- [`docs/mti.md`](../mti.md), [`docs/migrations.md`](../migrations.md), issue #45.
