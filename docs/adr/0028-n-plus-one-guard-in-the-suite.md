# 0028 — the suite fails on a repeated lazy load, and the collector's is opted out by name

- **Status:** accepted
- **Date:** 2026-10-03
- **Affects:** `tests/conftest.py` (`_n_plus_one_guard`), `django-zeal` in the dev group

## Context

The kit's selling point is that it moves work into PostgreSQL, so a query count that grows with the data is a defect here, not a style issue. Nothing in the suite counted queries, so N+1 shapes (a lazy foreign-key read in `__repr__`, the collector's per-row parent read for an MTI child) were found by a consumer's request log, 641 reads in one request (#55).

## Decision

An autouse, function-scoped fixture wraps every test in `zeal.zeal_context()`, so a lazy related-object load repeated from one call site raises `NPlusOneError`. One allow-list entry exists, in `tests/settings.py`: a repeated `QuerySet.get()` from one call site, since tests re-read a row per assertion by design. Canaries in `tests/test_n_plus_one_guard.py` pin that related-object loads stay fatal with that entry in force. A test that deliberately runs a known N+1 opts out locally with `zeal_ignore()`; the first is Django's deletion collector in the `delete()` fast-path tests (#58), where only the collector variant opts out, so the fast-path variants stay guarded and prove they issue no per-row read. Exact statement-count tests cover what a lazy load does not, such as `set_config` round trips.

## Why

- **Why a library, not hand counts?** A count per test pins one shape; the guard watches every test, including ones written later.
- **Why allow-list only `.get()`, and opt out per test otherwise?** An entry for a model or a field would also hide a real regression on it, everywhere. The `.get()` entry is the one shape tests produce on purpose, and the canaries keep it from widening; anything else is excused only inside the test that needs it.
- **Strongest objection.** A guard that raises on a heuristic can flag a harmless loop. The opt-out is the pressure valve, and each use must say what it is excusing.

## Consequences

**Accepted costs.** A dev dependency, and a fixture every test pays for. A new test that trips the guard must be fixed or opted out with a reason.

**Reversibility.** Delete the fixture and the dependency; nothing shipped depends on them.

## Related

- Issue #55 · ADR 0026 (the `delete()` fast path, arriving with #58) · `tests/test_n_plus_one_guard.py`
