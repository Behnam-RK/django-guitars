# 0001 — GuitarModel owns a swappable tenant foreign key

- **Status:** accepted
- **Date:** 2026-07-30
- **Affects:** `guitars.models.GuitarModel`, `GUITARS_TENANT_MODEL`, `GUITARS_TENANT_FIELD`

## Context

Tenancy needs a column to predicate on. Something has to decide what that column
is called and what it points at.

The obvious cautionary tale is `AUTH_USER_MODEL`. A swappable model referenced by
a settings string is one of Django's most-regretted APIs: it is resolved lazily, it
makes migrations depend on a setting, and swapping it after the fact is close to
impossible. Copying that pattern deserves an argument.

The alternatives considered:

1. **The consumer declares the FK themselves** and composes `tenanted_manager()` by
   hand on every model.
2. **A checked requirement** — the kit asserts that any `GuitarModel` subclass has
   *some* field it can use, without contributing one.
3. **`GuitarModel` contributes the FK**, targeting `settings.GUITARS_TENANT_MODEL`
   and named `settings.GUITARS_TENANT_FIELD`.

## Decision

Option 3. `GuitarModel` contributes a non-null `ForeignKey` to
`GUITARS_TENANT_MODEL`, named `GUITARS_TENANT_FIELD` (default `tenant`), with
`on_delete=CASCADE`, `editable=False`, and
`related_name="%(app_label)s_%(class)s_set"`.

Because the field's *name* comes from a setting, it cannot be declared in the
class body. It is contributed with `add_to_class` after it — the same entry point
Django's own metaclass uses — and abstract bases have their `local_fields` and
`local_managers` copied down when a concrete subclass is defined, so it reaches
every subclass exactly as a declared field would.

## Why

**Option 1 does not scale to the guarantee.** Getting tenancy right per model
means remembering three managers, `autofill`, and a non-null column, every time.
The asymmetry is not hypothetical: wrap `objects` and forget `_all_objects`, and
you have a manager that reads across tenants in Python. Something has to own the
correct arrangement, and a base class is what owns arrangements.

**Option 2 sounds safer and is worse.** A "checked requirement" means the check
fires at `manage.py check` time on a model the developer already wrote, with a
message explaining what shape it should have had. Contributing the field means
there is no shape to get wrong.

**The `AUTH_USER_MODEL` objections mostly do not apply here**, and the ones that do
are cheaper:

- *Lazy resolution* — the FK target is a string until a concrete subclass is
  defined, which is normal for any abstract base with a relation.
- *Migrations depend on a setting* — true, and the same is true of any FK. If the
  consumer's tenant model declares `Meta.swappable = "GUITARS_TENANT_MODEL"`,
  Django writes the setting reference into migrations instead of the concrete
  label, exactly as it does for `AUTH_USER_MODEL`.
- *Swapping later is painful* — also true, and the reason the setting has **no
  default**. There is no sensible one, and guessing would wire a project to the
  wrong table silently.

`CASCADE` rather than `PROTECT` because a tenant's rows are meaningless without
it — and because the kit's deletion is a *soft* delete, "cascade" archives rather
than destroys.

`editable=False` because the field is framework-owned: it stays out of ModelForms
and the admin, and the write guard fills it from the active scope. That is also
why `GuitarModel` passes `autofill=True` explicitly, overriding the global default
— requiring every call site to name a field it cannot see would be ceremony
without a payoff.

## Consequences

**Accepted costs.**

- **One dimension, one field.** `GuitarModel` scopes on exactly one axis. A model
  needing two, or a multi-hop dimension, declares `tenanted_manager()` by hand — which
  remains fully supported, and is the documented escape hatch rather than a
  fallback.
- **A `CASCADE` FK per tenanted model** means one cascade soft-delete rule on the
  tenant table per model, if the tenant model is itself soft-deletable. At N models
  a tenant soft-delete fires N `UPDATE`s in one statement. Correct, and cheap next
  to orphaned rows, but it scales with model count.
- **Without the setting the rung is inert.** `guitars.models` must stay importable
  for projects on the lower rungs, so a missing `GUITARS_TENANT_MODEL` is not an
  import error. It is caught by the `guitars.tenancy.E003` system check, which
  names the offending models and offers both ways out — set the setting, or drop to
  `SetarModel`. The check is registered at import of `guitars.models` rather than
  by `tenancy.install()`, because `install()` never runs in exactly the
  configuration that needs the check.

**Reversibility.** Poor, as with any FK-bearing base class. That is priced in by
the setting having no default: adopting the rung is an explicit act.

## Related

- [ADR 0004 — leaving `base_manager_name` unset](0004-unscoped-base-manager.md)
- [docs/tenancy.md](../tenancy.md)
