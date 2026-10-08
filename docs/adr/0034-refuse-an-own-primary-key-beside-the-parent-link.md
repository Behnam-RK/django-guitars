# 0034 — an MTI child's own primary key beside its parent link is refused

- **Status:** accepted
- **Date:** 2026-10-07
- **Affects:** `guitars.E005`, `checks.pk_not_parent_link`, `checks.refuses_pk_not_parent_link`, `_build_operations`, `introspection.owner_arms`, `tenancy.discovery._classify`

## Context

Issue #64. An MTI child may declare `code = AutoField(primary_key=True)` beside `root_link = OneToOneField(Root, parent_link=True)`; Django accepts it. Every join this kit writes from such a child to its ancestor reads the child's primary key as the link, and for this shape the two differ. It reaches eight places: the MTI redirect rule (a `DELETE` of the child archives an unrelated `Root` row and leaves its own live), the parent `_updated_at` trigger, owned arms, the **tenant policy's owner join** (it matches another tenant's row), queryset and instance `hard_delete`, `sweepowned`, and every key *into* such a model, which stores the child's key where the root's rules compare the root's id.

## Decision

`guitars.E005` is an `Error` for an MTI child whose primary key is not one of its parent links, where an *ancestor* holds what the kit joins up for: `_updated_at` or `_deleted_at`, or a local tenant dimension, whose owner join reads the key, one finding per model declaring it, as E003 reports. The generator re-asks the **whole-chain** predicate, since `--skip-checks` reaches it: no MTI redirect rule and no parent trigger for the chain, no owned arm for a refused inheriting owner, no tenant owner-join policy (Python scoping still applies), no cascade rule for a key *into* such a model. Each says so in a note naming E005 except the owned arm, dropped without one of its own. `hard_delete()`, which runs no check, raises `ImproperlyConfigured` for it, for every model of the tree it is called on (the queryset form deletes the whole tree by shared key) and, in the instance form, before Phase 1 and for every cascade, generic or owned hop it can seed from. The coverage-plan gaps stay, as the guard under `--skip-checks`.

## Why

- **Fixing the join covers one of eight.** `get_ancestor_link` fixes the redirect rule and trigger for a direct child and keeps every normal shape byte-identical. A mid-chain model would need a join the one-hop templates cannot say, and a key into such a model needs target-side templates in every family; the tenant policy and `hard_delete` each want their own change.
- **One answer at every site that joins**, tenancy included, where a wrong join is a security problem rather than a wrong archive.
- **Rejected: fix the direct child, refuse the rest.** A partial fix whose boundary users must learn, in a shape this rare, with two security-relevant sites in the diff.
- **Rejected: fix everything.** The largest change in the kit's history for a shape nothing in the project uses.
- **Strongest objection.** A project with this shape cannot run `check` or `migrate` until it restructures. Accepted because the alternative is a generated rule archiving rows that are not the model's own, and the fix is mechanical: drop the explicit key, or make the model its own with a foreign key to its parent.

## Consequences

**Accepted costs.** A multi-parent model is left alone (its primary key is one of its links, a separate shape), and so is a parent link that is the primary key but targets a `to_field` of the parent: the joins read the parent's own key where the link stores that column, the same wrong match through a cause E005 does not name. A plain Django MTI model carrying no kit column and no tenant spec is not checked, nor is a child holding the column or tenant dimension itself, which is read off its own table: nothing here joins on its key. Under `--skip-checks` the refused chain gets no redirect rule, so a `.delete()` of the child removes its row for good where it archived an unrelated ancestor row: E005 is an `Error` for E003's reason. An `OwningForeignKey` aimed at such a model is not re-asked by the generator and stays a coverage-plan gap; `hard_delete()` refuses it since 2.18.0.

**Reversibility.** Removing the check is one line; the generator's gates would then need the join fixed, per site.

## Related

- [ADR 0003](0003-mti-owner-join-policy.md) · [ADR 0015](0015-refuse-soft-deletable-mti-orphans.md) · [`mti.md`](../mti.md) · #64
