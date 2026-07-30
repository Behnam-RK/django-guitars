# Domain model

The vocabulary this codebase uses, and means precisely. Definitions only — the
mechanics live in [`docs/`](docs/).

Where two words could describe the same thing, only one is used. Where two things
could share a word, they get different ones. That is the whole purpose of this
file: a reader who has these terms can read any docstring in the repo, and a
contributor who uses them writes code the next reader recognises.

## Enforcement

**Enforcement layer**
: One of the two places a guarantee is imposed. The **Python layer** is the loud
one — it raises where a developer can see it. The **database layer** is the
complete one — it applies to every statement, including the ones no Python
touches. Neither is redundant.

**Enforcement migration**
: A generated migration made of `RunSQL` operations. Django's own migrations
describe *schema*; these describe what the database guarantees about the **rows**.

**Enforcement operation**
: One `RunSQL` entry inside such a migration. There are four kinds, each with a
name of its own: **timestamp trigger**, **soft-delete rule**, **MTI redirect
rule**, **tenant policy**. Never "advanced migration", which says nothing.

**Frozen interface**
: A name that generated migrations in consuming projects read by name — every
public name in `guitars.sql`, and every per-operation comment header. Renaming one
breaks `migrate` on a fresh database elsewhere, or makes the generator emit
duplicates.

**Fail closed / fail open**
: Which way a mechanism breaks when its inputs are missing. Fail closed = deny,
lose access. Fail open = allow, lose the guarantee. Every guard in the kit is
written so the *unknown* case fails closed — including the soft-delete rules,
whose closed direction is "keep the row".

## Soft deletion

**Soft delete**
: Setting `_deleted_at` instead of removing the row. Performed by a PostgreSQL
rule, not by Python, so it holds for bulk and raw deletes.

**Hard delete**
: Actually removing the row, by opting out of the rule for the duration of one
transaction. Always explicit: `hard_delete()`.

**Archive**
: A soft-deleted row. `_archives` is the manager that shows only those. "Archived"
and "soft-deleted" are the same state; prefer whichever reads better locally.

**Live**
: A row with `_deleted_at IS NULL`. What `objects` shows.

**Cascade rule**
: A `DO ALSO` rule that propagates a soft delete to rows related by
`on_delete=CASCADE`. Keyed on the `_deleted_at` transition rather than on
`.delete()`, which is why it survives bulk and raw deletes.

## Multi-table inheritance

**Owner**
: The concrete model whose **physical table** declares a given column. For a model
using an abstract base, itself; for an MTI child, the ancestor that declared it.
Resolved via `model._meta.get_field(name).model`, never `hasattr`.

Deliberately *owner*, not *parent*: in a chain three deep the column may live two
tables up, so the immediate parent may not have it either.

**Shared-PK invariant**
: Every table in an MTI chain shares one primary-key **value** — the child's PK is
a parent link holding the ancestor's id. Every correlated join in the kit rests on
this.

**Redirect rule**
: The `ON DELETE … DO INSTEAD` rule on an MTI child's table: preserves the child
row and stamps `_deleted_at` on the owner.

**Own-table** / **inherited**
: Whether a column or foreign key is physically on this model's table, or reached
through an ancestor's. The distinction decides which SQL is valid, so it is never
elided.

## Tenancy

**Tenant**
: The entity rows belong to — an organisation, a shop, a workspace. Named by
`GUITARS_TENANT_MODEL`; guitars ships no tenant model of its own.

**Dimension**
: A named axis a model is scoped on. Usually one (`tenant`, `org`, `shop`), but a
hand-declared `TenantedManager` may take several. The dimension name is *not*
necessarily a column name — see **multi-hop**.

**Frame**
: The active `{dimension: value}` mapping. Lives in a `ContextVar`, so it survives
`await` and `sync_to_async`.

**Scope** (verb: to scope; **scoped**, **unscoped**)
: Entering a frame — `with tenant(org=acme)`. A **scoped** read is filtered to the
frame; an **unscoped** one has no frame active and is refused.

A **collection scope** names several values for one dimension, meaning "either of
these". A dimension bound to `None` is **absent**, not a wildcard.

**Bypass**
: Deliberately suspending enforcement — `tenancy_bypassed()`. The only cross-tenant
path there is, and the only one there will be: one greppable name means every
cross-tenant access in a codebase can be found.

**Coverage**
: Which tables are policy-eligible, and how each is predicated. One definition,
shared by the migration generator and the live audit, so a build gate and a deploy
gate cannot disagree about what protection means.

A model can be **partially covered**: own-table dimensions enforced, others left to
Python. Reported as exactly that.

**Multi-hop dimension**
: A dimension reached through a relation (`org="release__org"`). Python can scope
it; a policy cannot, because there is no column on this table to predicate on.

**Owner-join policy**
: The policy form for an MTI child — a correlated `EXISTS` against the owner table
on the shared PK. Necessary because a child-only statement never touches the
ancestor.

**Enforcement mode**
: `GUITARS_TENANT_ENFORCE`. **strict** raises on a write violation; **audit**
reports once per distinct finding and proceeds. Audit softens the *Python* layer
only — no session setting makes a policy lenient — so it belongs before the
policies bind.

**Binding** (of a policy)
: Whether a policy actually constrains the connecting role. A policy can exist,
be `ENABLE`d, and bind nothing: the table's owner bypasses non-`FORCE` RLS
silently. "Has a policy" and "is protected" are different claims and are never
conflated.

**GUC**
: A PostgreSQL session setting — the bridge from the Python frame to a policy
predicate. The frame is published as `tenant.<dimension>` plus `tenant.bypass`.
Used because a policy cannot read a `ContextVar`.

## Naming conventions

- Metadata fields are underscore-prefixed: `_created_at`, `_updated_at`,
  `_deleted_at`. So are non-default managers: `_archives`, `_all_objects`.
- Settings are namespaced `GUITARS_*`. The two pre-existing exceptions,
  `LOCAL_APPS` and `TRIGGER_FUNCTION_APP`, are kept for compatibility.
- Session settings are `<mechanism>.<knob>`: `rules.hard_deletion`,
  `tenant.bypass`.
- The base models are named by string count — see
  [Pick your instrument](README.md#pick-your-instrument). The ladder follows the
  etymology (`du` = two, `se` = three), not any instrument's current string count.
