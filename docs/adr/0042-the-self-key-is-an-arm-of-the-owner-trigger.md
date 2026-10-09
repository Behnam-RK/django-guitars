# 0042 — a self-referential cascade key is an arm of the owner's trigger

- **Status:** accepted
- **Date:** 2026-10-09
- **Affects:** `introspection.CascadeKind.SELF`, `_cascade_key_maps`, `_retired_trigger_operations`, `_SOFT_DELETE_LEAK_CHECK_SELF`
- **Amends:** [ADR 0018](0018-self-referential-cascade-trigger.md) (its trigger is retired; its refusal is kept), [ADR 0024](0024-inverse-cascade-revive-rules.md), [ADR 0039](0039-cascade-arms-in-the-owner-trigger.md), [ADR 0041](0041-cascade-cycles-are-arms.md)

## Context

A key onto the owner's own table took a trigger of its own (ADR 0018), because a rule updating the table it fires on is rewritten into itself. Since 2.19.0 the other cascade keys are arms of the owner's trigger (ADR 0039) and a cycle through several tables is one too (ADR 0041), so the reason for a second family was gone (#85). It also differed from the arms in ways nobody chose: it stamped `NOW()` where an arm copies the parent's stamp (ADR 0023), it had no revive, and it carried its own copy of the primary-key-rewrite refusal.

## Decision

- **A self key is an arm**, archive and revive, of `soft_delete_cascade_on_<table>`, rendered from the same templates; the arm's `UPDATE` fires the trigger again, one level further down. `CascadeKind.SELF` stays a classification, as the model it names is the owner.
- **Its children carry the parent's `_deleted_at`, and a restore finds them**: archive a tree and restore its root, and the subtree archived with it comes back. A child archived earlier keeps its own stamp and stays archived.
- **The refusal is the union of the two.** The owner's leak check already refuses a live child holding a key of a live before-row; the self trigger also refused one holding the archived row's **new** key, which a deferred foreign key permits ahead of its row. That second disjunct is a template of its own, `_SOFT_DELETE_LEAK_CHECK_SELF`, used only for a self key, so no other owner's function changes.
- **The self trigger is retired where the owner's is written**, after it, `IF EXISTS` over every name the table held; unapplying rebuilds it as 2.8.0 wrote it (`_superseded_self_cascade_reverse`). A key the models no longer call for is retired as before, by the table's own host, with a reverse that refuses. Its header, scanner and SQL stay to read history and build that reverse.
- A name carrying `$$` is no longer refused for a self key: the owner's function takes another dollar-quote tag (ADR 0039).

## Why

One family means one set of rules for the stamp, the revive and the refusal, and one trigger per table where a tree had two. The two behaviours it changes were accidents of the mechanism: `NOW()` for want of the parent's stamp in a body that never read it, and no revive because the trigger kept no record of what it archived, which the stamp now is.

## Consequences

**Behaviour change.** A subtree gains the parent's stamp and revives with its root, where it had a fresh `NOW()` and stayed archived. The exposure ADR 0024 names reaches the tree: a child archived in the same transaction as its parent is revived with it. Every app hosting a self key gets a migration replacing the owner's trigger and dropping the self one: `DROP TRIGGER` takes ACCESS EXCLUSIVE on that table until the migration commits, so set `lock_timeout`.

**Gained.** The arm names the key column only inside a branch: dropping it fails an archive of the table, not every `UPDATE` as the self trigger did, until `makeguitarmigrations` re-emits the trigger (ADR 0039).

**Reversibility.** Unapplying the migration rebuilds the self trigger and replaces the owner's trigger without the arm. Going back in code needs the self-key branch of `_cascade_key_maps` removed and the retirement reversed.

## Related

- [ADR 0018](0018-self-referential-cascade-trigger.md) · [ADR 0023](0023-cascade-stamps-its-parents-timestamp.md) · [ADR 0024](0024-inverse-cascade-revive-rules.md) · [ADR 0039](0039-cascade-arms-in-the-owner-trigger.md) · [ADR 0041](0041-cascade-cycles-are-arms.md) · #85
