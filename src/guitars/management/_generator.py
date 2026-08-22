"""Shared mechanics for generating raw-SQL migrations -- scoping, label-validation, and
the ``[DIGEST:...]`` idempotency layer (other two in ``docs/migrations.md``). About
*files*, not models: what belongs in a migration is each command's own business."""

from __future__ import annotations

import hashlib
import os
import re
import textwrap
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from django.apps import apps as django_apps
from django.core.management import CommandError, call_command

from guitars.local_apps import is_local


if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.apps import AppConfig


__all__ = [
    'RE_DIGEST',
    'create_empty_migration_file',
    'digest_of',
    'is_in_scope',
    'is_local',
    'iter_migration_files',
    'migrations_dir',
    'validate_app_labels',
    'write_migration_file',
]

#: Every migration file this module reads or writes is UTF-8, regardless of platform
#: locale -- explicit rather than left to ``read_text``/``write_text``'s default.
_ENCODING = 'utf-8'

#: The idempotency marker, written on a generated migration's first line and read back
#: to recognise it later. Only the bracketed digest is matched, so each command is free
#: to word the rest of the line however it likes.
RE_DIGEST = re.compile(r'\[DIGEST:(?P<digest>\w+)\]')


# ---------------------------------------------------------------------------
# App selection
# ---------------------------------------------------------------------------


def is_in_scope(app: AppConfig, requested: set[str]) -> bool:
    """Whether *app* is local and, when scoping, among the *requested* labels -- local-ness
    keys on ``app.name`` (matching ``LOCAL_APPS``), scoping on ``app.label``."""
    return is_local(app) and (not requested or app.label in requested)


def validate_app_labels(requested: set[str]) -> None:
    """Reject unknown app labels, mirroring Django's own ``makemigrations`` -- without this
    a typo silently matches no app, turning ``--check`` into a no-op CI gate that passes."""
    for app_label in sorted(requested):
        try:
            django_apps.get_app_config(app_label)
        except LookupError as err:
            raise CommandError(str(err)) from err


# ---------------------------------------------------------------------------
# Reading existing migrations
# ---------------------------------------------------------------------------


def migrations_dir(app: AppConfig) -> Path:
    """The ``migrations/`` directory for *app*, whether or not it exists yet."""
    return Path(app.path) / 'migrations'


def iter_migration_files(app: AppConfig) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, content)`` for every migration file in *app*, in filename order --
    sorted, not raw ``glob`` order (filesystem-dependent), since Django's ``NNNN_name.py``
    numbering is application order and a "last write wins" scanner depends on it."""
    app_migrations_dir = migrations_dir(app)
    if not app_migrations_dir.is_dir():
        return
    for path in sorted(app_migrations_dir.glob('*.py')):
        yield path, path.read_text(encoding=_ENCODING)


def digest_of(operations: list[str]) -> str:
    """Content digest for an operation set -- not a security boundary, hence
    ``usedforsecurity=False`` (also keeps this legal on a FIPS build). Callers must render
    deterministically; an unstable rendering produces a new migration on every run."""
    return hashlib.md5('\n'.join(operations).encode(), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Writing migrations
# ---------------------------------------------------------------------------


def create_empty_migration_file(app: AppConfig, name: str) -> str:
    """Scaffold via ``makemigrations --empty``, returning the filename parsed back out of
    captured output (Django prints, doesn't return it). Unstamped scaffolds are left in
    place on error -- no ``[DIGEST:...]``, so a later run can't mistake one as covered."""
    buf = StringIO()
    call_command('makemigrations', app.label, '--name', name, '--empty', stdout=buf)
    output = buf.getvalue()

    match = re.search(rf'[\\/](?P<filename>\d{{4}}_{re.escape(name)}\.py)', output)
    if not match:
        raise CommandError(
            f'Could not find the created migration file! Command output: {output}\n'
            f'Check {migrations_dir(app)} for an unstamped scaffold matching "{name}".'
        )

    return match.group('filename')


def _mentions_dependency(line: str, dependency: tuple[str, str]) -> bool:
    """Whether *line* already declares *dependency* -- both halves on it, in either quoting.
    Django's writer ``repr``s the tuple to single quotes; a hand-edited file may use double,
    and a file written before 2.5.0 is read the same way, so neither is assumed."""
    return all(f"'{part}'" in line or f'"{part}"' in line for part in dependency)


def write_migration_file(
    app: AppConfig,
    migration_file: str,
    operations: list[str],
    operations_digest: str,
    *,
    generated_by: str,
    dependencies: list[tuple[str, str]] | None = None,
) -> None:
    """Rewrite an ``--empty`` scaffold to carry *operations*. Deliberately no import to
    insert -- operations carry their SQL literally, so nothing here reaches back into the
    package at ``migrate`` time. See ADR-0006."""
    file_path = migrations_dir(app) / migration_file
    lines = file_path.read_text(encoding=_ENCODING).splitlines(keepends=True)

    # Replace Django's own "Generated by" line with ours, carrying the digest.
    lines[0] = f'# Generated by {generated_by} command! [DIGEST:{operations_digest}]\n'

    # Append indented operations before the closing bracket of ``operations = [``.
    for operation in operations:
        indented = textwrap.indent(operation, ' ' * 8)
        for line in indented.split('\n'):
            lines.insert(-1, f'{line}\n')

    # Depend on the function migration(s) and on whatever creates the objects the operations
    # name. Skip self-references and anything Django's scaffold already wrote.
    migration_stem = Path(migration_file).stem
    for dependency in dependencies or []:
        if migration_stem == dependency[1]:
            continue
        # Both halves on one line, never the name alone: cross-app edges (2.5.0) routinely point
        # at an ``0001_initial``, so matching the name reads the scaffold's own app's as covering
        # another's and drops the edge. Function migration names happened never to collide.
        if any(_mentions_dependency(line, dependency) for line in lines):
            continue
        # Single-quoted, matching what Django's own writer emits (it ``repr``s the tuple)
        # and what a ruff- or black-free formatter leaves alone. The scan above accepts
        # either quoting, so a file written by an older version still dedupes.
        dep_line = f"        ('{dependency[0]}', '{dependency[1]}'),\n"
        try:
            dep_idx = next(i for i, line in enumerate(lines) if 'dependencies = [' in line)
        except StopIteration as err:
            raise CommandError(
                "Could not find 'dependencies = [' in the scaffolded migration; "
                "makemigrations' --empty template may have changed."
            ) from err
        lines.insert(dep_idx + 1, dep_line)

    # Write to a temp file and swap it in: a process killed mid-write leaves the temp file
    # truncated, never the migration Django's loader will actually read.
    tmp_path = file_path.with_suffix(file_path.suffix + '.tmp')
    tmp_path.write_text(''.join(lines), encoding=_ENCODING)
    os.replace(tmp_path, file_path)
