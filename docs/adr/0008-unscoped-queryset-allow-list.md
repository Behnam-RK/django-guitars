# 0008 — Invert the unscoped-queryset deny-list to an allow-list

- **Status:** accepted
- **Date:** 2026-08-10
- **Affects:** `guitars.tenancy.querysets` (`_ALLOWED_UNSCOPED`, the queryset-sweep), `Manager.raw()`'s tenancy behavior

## Context

`_UntenantedQuerySet` (the guard applied when no tenant scope is active) denied
a hand-curated list of specific queryset methods. Every other method — anything
Django added in a future release, or any method guitars itself later defined —
was implicitly *allowed* by default, until someone noticed the gap and added it
to the deny-list. Deny-by-default in name only: the actual default was fail
**open**. M0 had already closed the two worst known holes (`_raw_delete`,
`explain()`) non-breaking, but `Manager.raw()` was deliberately left open for
this milestone, since handling it properly looked like it might need an
explicit API decision rather than a quiet addition to the list.

Alternatives considered:
1. Keep the deny-list, and pair it with a dynamic enumeration test (already
   added in M1) that fails the suite when Django's actual `QuerySet` surface
   gains a method the list hasn't classified — catches drift, but the default
   for an unclassified method is still "allowed" until the test is written and
   passes.
2. Invert to an allow-list: name the methods known safe to leave reachable
   without an active scope, deny everything else Django or guitars defines by
   default.

## Decision

Option 2. `_ALLOWED_UNSCOPED` names the queryset methods known safe to leave
reachable unscoped (lazy/metadata operations, each with a documented reason);
`_apply_default_deny_sweep` denies every other public method Django or guitars
defines that nothing already handles explicitly. A Django release adding a
queryset method, or a future guitars queryset method, is now denied until
someone classifies it as safe — fail-closed instead of fail-open.

`Manager.raw()` is resolved as **denied** on an unscoped queryset: the
`RawQuerySet` it returns is a distinct class that never passes back through the
denying queryset, so leaving it allowed would have handed out an unscoped
escape hatch — the same reasoning `hard_delete()` was already denied under (the
"the database's job" argument that argued for permitting it turned out to prove
too much: raw SQL bypasses every guard, not just the ones already denied).
`tenancy_bypassed()` remains the explicit, visible way to use it unscoped.

A downstream consumer's own custom queryset method is deliberately left
reachable — it can only reach the database through a primitive this module
already denies, so classifying it explicitly would add ceremony without a
safety payoff.

## Why

Option 1 alone still leaves the *default* fail-open — a new method is safe only
once someone writes a test asserting it's classified, which is exactly backward
from how a deny-list should behave under an unknown future addition. Option 2
makes "not yet classified" and "not safe" the same state by construction,
matching the "fails open" pattern this project's own `guc.py` docstring already
names as the standard to hold every guard to.

The `Manager.raw()` decision specifically resolves an inconsistency flagged in
review: `hard_delete()` was already denied on "the database's job, do it
through `tenancy_bypassed()`" reasoning, and leaving `raw()` open was the same
shape of hole with a different name.

## Consequences

**Accepted costs.**
- Breaking for any consumer calling `Manager.raw()` unscoped today — must wrap
  in `tenancy_bypassed()` going forward. Called out prominently in the
  changelog as the change most likely to affect a real consumer.
- A future Django queryset method is denied-by-default until classified, which
  means a project could see a new Django release's method rejected under
  guitars where it worked under plain Django, until `_ALLOWED_UNSCOPED` is
  updated. Traded deliberately for closing the fail-open default.

**Reversibility.** Low — reverting to a deny-list reopens the exact fail-open
gap (M0's `_raw_delete`/`explain()` incident, and the `raw()` inconsistency)
this decision exists to close.

## Related
- [ADR 0004](0004-unscoped-base-manager.md) — related unscoped-manager
  reasoning
- `tests/test_tenancy_denylist.py` — the M1 dynamic enumeration test, now
  cross-checking against the runtime `_ALLOWED_UNSCOPED`
- `CHANGELOG.md`'s `[2.0.0]` entry, M5 section
