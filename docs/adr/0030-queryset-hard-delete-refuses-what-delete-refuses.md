# 0030 — queryset `hard_delete()` refuses what `delete()` refuses

- **Status:** accepted
- **Date:** 2026-10-07
- **Affects:** `HardDeletableQuerySet.hard_delete`, `_guard_bulk`

## Context

The plain form of queryset `hard_delete()` compiled `self.query` as a `DeleteQuery`. A `DELETE` has no `LIMIT`, `OFFSET`, combinator or `DISTINCT ON`, so the compile dropped them: `order_by('pk')[:1].hard_delete()` permanently removed every row the filter matched, and a combined queryset kept one operand's filter: a `union()` removed its first half only, an `intersection()` or `difference()` more than it matched. The MTI form read its keys with `values_list('pk')` first, so it honoured every one of those shapes, and a plain `.values()` changed nothing a `DELETE` reads. Found in round 5 of the review loop on #69.

## Decision

Both forms run `_guard_bulk(self, 'hard_delete')` first and refuse exactly the four shapes Django's `delete()` refuses: a slice, a combined queryset (`union()`, `intersection()`, `difference()`), `distinct(*fields)` and `values()`/`values_list()`. That includes the shapes that did no harm on 2.14.2.

## Why

- **One rule.** `delete()`, `soft_delete()` and now `hard_delete()` answer the same question about a queryset the same way. A refusal that depends on the form (plain or MTI) or on which of the four shapes is in play is a rule a reader has to look up, for the one operation that cannot be undone.
- **Rejected: refuse only what lost data.** Refusing a slice, a combined queryset and `distinct(*fields)` in the plain form alone would have kept 2.14.2's working shapes working. It leaves the two forms disagreeing with each other and with `delete()`, and a model gaining or losing a concrete parent would silently move a queryset from allowed to refused.
- **Rejected: honour the shapes.** Reading the keys first, as the MTI form does, would make every shape work. It costs a read on the common path, and a sliced permanent delete is rare enough that refusing it is the safer default.
- **Strongest objection.** It refuses shapes that were safe, in a patch release: a consumer slicing an MTI queryset before a permanent delete now gets an error instead of the delete it asked for. Accepted because no known consumer does, and loosening later breaks nobody.

## Consequences

**Accepted costs.** A removal in a patch release (2.14.3): any of the four shapes on an MTI model, or a plain `.values()` queryset, now raises `TypeError` or `NotSupportedError` where it worked. No known consumer calls queryset `hard_delete()` on any of them.

**Reversibility.** Easy to loosen: honouring a shape later breaks nobody. Tightening again after a loosening would be the hard direction.

## Related

- [ADR 0026](0026-soft-delete-and-delete-fast-path.md) · [`soft-deletion.md`](../soft-deletion.md) · #69
