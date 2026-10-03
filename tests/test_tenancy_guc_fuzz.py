"""Random interleavings of tenant scopes and savepoints, checking after every query that the
database setting equals the active scope. Tenant scopes and atomic blocks each nest strictly, but
interleave freely -- the non-lexical shapes example tests miss. Fails on the pre-2.11.1 code."""

from __future__ import annotations

import random

import pytest
from django.db import transaction

from guitars.tenancy import tenant
from tests.conftest import scalar


_DIMENSIONS = ('label', 'other')
_OPERATIONS = (
    ['tenant_push'] * 2
    + ['tenant_pop']
    + ['block_push'] * 2
    + ['savepoint_push']
    + ['block_commit']
    + ['block_rollback'] * 2
    + ['query'] * 2
)


def _setting(dimension: str) -> str:
    return scalar('SELECT current_setting(%s, true)', [f'tenant.{dimension}']) or ''


def _program(rng: random.Random, labels: list, steps: int) -> list:
    scopes: list = []  # (dimension, value, manager), innermost last
    blocks: list = []  # ('atomic', manager) or ('raw', sid), innermost last
    trace: list[str] = []
    try:
        for _ in range(steps):
            operation = rng.choice(_OPERATIONS)
            trace.append(operation)
            if operation == 'tenant_push' and len(scopes) < 4:
                dimension = rng.choice(_DIMENSIONS)
                label = rng.choice(labels)
                value = str(label.pk) if dimension == 'label' else rng.choice(['X', 'Y'])
                manager = tenant(**{dimension: label if dimension == 'label' else value})
                manager.__enter__()
                scopes.append((dimension, value, manager))
            elif operation == 'tenant_pop' and scopes:
                scopes.pop()[2].__exit__(None, None, None)
            elif operation == 'block_push' and len(blocks) < 4:
                manager = transaction.atomic()
                manager.__enter__()
                blocks.append(('atomic', manager))
            elif operation == 'savepoint_push' and len(blocks) < 4:
                blocks.append(('raw', transaction.savepoint()))
            elif operation in ('block_commit', 'block_rollback') and blocks:
                kind, handle = blocks.pop()
                failing = operation == 'block_rollback'
                if kind == 'atomic':
                    handle.__exit__(*((RuntimeError, RuntimeError(), None) if failing else (None,) * 3))
                elif failing:
                    transaction.savepoint_rollback(handle)
                else:
                    transaction.savepoint_commit(handle)
            elif operation == 'query':
                expected = dict.fromkeys(_DIMENSIONS, '')
                expected.update({dimension: value for dimension, value, _ in scopes})
                found = {dimension: _setting(dimension) for dimension in _DIMENSIONS}
                if found != expected:
                    return [(trace, found, expected)]
    finally:
        while blocks:
            kind, handle = blocks.pop()
            if kind == 'atomic':
                handle.__exit__(None, None, None)
            else:
                transaction.savepoint_commit(handle)
        while scopes:
            scopes.pop()[2].__exit__(None, None, None)
    return []


@pytest.mark.django_db
@pytest.mark.parametrize('seed', range(8))
def test_the_database_always_holds_the_active_scope(tenants, seed):
    rng = random.Random(seed)
    failures: list = []
    for _ in range(60):
        failures += _program(rng, [tenants.a, tenants.b], steps=30)

    assert not failures, f'{len(failures)} programs ended stale; first: {failures[0]}'
