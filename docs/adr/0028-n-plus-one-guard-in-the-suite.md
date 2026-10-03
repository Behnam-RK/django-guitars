# 0028 — the suite fails on a repeated lazy load, and the collector's is opted out by name

- **Status:** accepted
- **Date:** 2026-10-03
- **Affects:** `tests/conftest.py` (`_n_plus_one_guard`), `django-zeal` in the dev group

## Context

The kit's selling point is that it moves work into PostgreSQL, so a query count that grows with the data is a defect here, not a style issue. Nothing in the suite counted queries, so N+1 shapes (a lazy foreign-key read in `__repr__`, the collector's per-row parent read for an MTI child) were found by a consumer's request log, 641 reads in one request (#55).

## Decision

An autouse, function-scoped fixture wraps every test in `zeal.zeal_context()`, so a lazy related-object load repeated from one call site raises `NPlusOneError`. A test that deliberately runs Django's deletion collector, whose per-row parent read is the N+1 the fast path exists to avoid, opts out with `zeal_ignore()`, and only the collector variant does: the fast-path variants stay guarded, which is what proves they issue no per-row read. Scale-invariance and exact statement-count tests cover what a lazy load does not, such as `set_config` round trips.

## Why

- **Why a library, not hand counts?** A count per test pins one shape; the guard watches every test, including ones written later.
- **Why opt out rather than allow-list a model or field?** The allow-list would also hide a real regression on that field. An opt-out is local to the test that names the collector.
- **Strongest objection.** A guard that raises on a heuristic can flag a harmless loop. The opt-out is the pressure valve, and each use must say what it is excusing.

## Consequences

**Accepted costs.** A dev dependency, and a fixture every test pays for. A new test that trips the guard must be fixed or opted out with a reason.

**Reversibility.** Delete the fixture and the dependency; nothing shipped depends on them.

## Related

- [ADR 0026](0026-soft-delete-and-delete-fast-path.md) · issue #55
