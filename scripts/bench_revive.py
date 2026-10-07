"""Time a single-row ``UPDATE`` of an owner table carrying N cascade keys' revives: one trigger
per key (2.11.0-2.15.x) against one per owner (2.16.0, #70). Not run in CI.

    uv run python scripts/bench_revive.py [--keys 16] [--updates 2000]

Needs the dev Postgres (``docker compose up -d``). Builds its tables in a scratch schema inside
one transaction and rolls it back, so it leaves nothing behind.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from pathlib import Path

import django


# ``core`` is the dev harness at the repo root, which a script under ``scripts/`` cannot import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from django.db import connection, transaction  # noqa: E402

from guitars.sql import soft_delete as sd  # noqa: E402


SCHEMA = 'guitars_bench'
OWNER = f'{SCHEMA}.owner'


def _child(index: int) -> str:
    return f'{SCHEMA}.child_{index}'


def _tables(cursor, keys: int) -> None:
    cursor.execute(f'CREATE SCHEMA {SCHEMA}')
    cursor.execute(
        f'CREATE TABLE {OWNER} (id serial PRIMARY KEY, val int NOT NULL DEFAULT 0, '
        '_deleted_at timestamptz)'
    )
    cursor.execute(f'INSERT INTO {OWNER} DEFAULT VALUES')
    for index in range(keys):
        cursor.execute(
            f'CREATE TABLE {_child(index)} (id serial PRIMARY KEY, owner_id int NOT NULL, '
            '_deleted_at timestamptz, _updated_at timestamptz)'
        )
        cursor.execute(f'CREATE INDEX ON {_child(index)} (owner_id)')
        cursor.execute(f'INSERT INTO {_child(index)} (owner_id) VALUES (1)')


def _slots(index: int) -> dict:
    return {
        'related_table': _child(index),
        'primary_key': 'id',
        'foreign_key': 'owner_id',
        'updated_at_assignment': sd._SOFT_DELETE_REVIVE_UPDATED_AT,
    }


def _per_key(cursor, keys: int) -> None:
    for index in range(keys):
        name = f'{SCHEMA}.revive_{index}'
        cursor.execute(
            sd._CREATE_SOFT_DELETE_REVIVE.format(
                function=name, trigger=f'revive_{index}', table=OWNER, **_slots(index)
            )
        )


def _per_owner(cursor, keys: int) -> None:
    arms = ''.join(sd._SOFT_DELETE_REVIVE_ARM.format(**_slots(index)) for index in range(keys))
    cursor.execute(
        sd._CREATE_SOFT_DELETE_REVIVE_OWNER.format(
            function=f'{SCHEMA}.revive_on_owner',
            trigger='revive_on_owner',
            table=OWNER,
            primary_key='id',
            arms=arms,
        )
    )


def _measure(install, keys: int, updates: int) -> float:
    """Milliseconds per ``UPDATE``, the median of five runs after a warm-up."""
    with transaction.atomic(), connection.cursor() as cursor:
        _tables(cursor, keys)
        install(cursor, keys)
        statement = f'UPDATE {OWNER} SET val = val + 1 WHERE id = 1'
        for _ in range(200):
            cursor.execute(statement)
        runs = []
        for _ in range(5):
            start = time.perf_counter()
            for _ in range(updates):
                cursor.execute(statement)
            runs.append((time.perf_counter() - start) * 1000 / updates)
        transaction.set_rollback(True)
    return statistics.median(runs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--keys', type=int, default=16)
    parser.add_argument('--updates', type=int, default=2000)
    args = parser.parse_args()

    shapes = {
        'no revive trigger': lambda cursor, keys: None,
        'one per key (2.15)': _per_key,
        'one per owner (2.16)': _per_owner,
    }
    results = {
        name: _measure(install, args.keys, args.updates) for name, install in shapes.items()
    }
    baseline = results['no revive trigger']
    out = sys.stdout
    out.write(
        f'{args.keys} cascade keys, {args.updates} single-row UPDATEs per run, median of 5\n'
    )
    for name, ms in results.items():
        out.write(f'  {name:<22} {ms:7.4f} ms/UPDATE  (+{ms - baseline:.4f} over none)\n')


if __name__ == '__main__':
    main()
