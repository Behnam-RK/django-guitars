"""Shared mechanics for generating raw-SQL migrations.

``makeguitarmigrations`` is the only command that writes them today, and the mechanics
are the delicate part, not the SQL. They live here rather than inside the command for
two reasons: ``makemigrations`` (guitars' override) reaches for the app-scoping and
label-validation helpers, and ``audittenancy`` must reject an unknown app label with the
same error the generator does, or a scoped deploy gate could pass having checked
nothing. A second generating command would inherit all of it:

* **Idempotency has three layers**, and this module owns only the first. A content
  digest stamped on the migration's first line (``[DIGEST:...]``) identifies an
  unchanged operation set; each command's own regex scan of existing migration files
  for the comment header it emits per operation identifies which tables are already
  covered; and a ``[SQL:...]`` identity on each header (``makeguitarmigrations``'s own,
  not shared here) identifies whether a covered table's operation is the SQL the kit
  emits *today*. All three matter: the file digest alone cannot handle a partially
  covered app, the header scan alone cannot tell "already done" from "done
  differently", and without the per-operation identity a recognised header read as
  covered forever, which is how the 1.0.0 soft-delete guard rewrite shipped no
  migration to any existing database.
* **Scaffolding re-enters ``makemigrations``.** These commands do not template a
  migration from scratch; they run ``makemigrations --empty`` and rewrite the result,
  which is why the ``makemigrations`` override must skip its guitar/tenant step on
  ``--empty`` or the two recurse.

Keeping one copy means a fix lands for both. The two commands diverged in exactly this
code before it was shared.

This module is deliberately about *files*, not models: what belongs in a migration is
each command's own business.
"""

from __future__ import annotations

import hashlib
import os
import re
import textwrap
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management import CommandError, call_command


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


def is_local(app: AppConfig) -> bool:
    """Whether *app* is one of the project's own, per ``settings.LOCAL_APPS``.

    Keyed on ``app.name``, because ``LOCAL_APPS`` holds dotted module paths
    (``tests.testapp``) rather than Django's short labels.

    Duplicated as ``guitars.tenancy.discovery.is_local`` on purpose -- see that copy for why
    neither module may import the other.
    """
    return app.name in settings.LOCAL_APPS


def is_in_scope(app: AppConfig, requested: set[str]) -> bool:
    """Whether *app* is local and, when scoping, among the *requested* labels.

    *requested* is the set of positional app labels passed to the command; an empty
    set means "all local apps" (the default, unscoped behaviour). Note the two
    different keys: local-ness is keyed on ``app.name`` to match ``LOCAL_APPS``, while
    scoping is keyed on ``app.label`` to match Django's positional arguments.
    """
    return is_local(app) and (not requested or app.label in requested)


def validate_app_labels(requested: set[str]) -> None:
    """Reject unknown app labels, mirroring Django's own ``makemigrations``.

    Without this a typo silently matches no app, which would turn ``--check`` into a
    no-op that exits 0 having validated nothing -- the worst possible outcome for a
    CI gate.
    """
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
    """Yield ``(path, content)`` for every migration file in *app*, in filename order.

    Sorted, not in ``glob`` order, and that is load-bearing rather than tidiness. Django
    numbers migrations ``NNNN_name.py``, so filename order is application order -- and a
    scanner that records the *last* value it saw for something (which is how the tenant
    policy's recorded shape is recovered) gets the currently-applied one only if it reads
    them in that order. ``glob`` yields in directory order, which is filesystem-dependent
    and would make the answer differ between machines.
    """
    app_migrations_dir = migrations_dir(app)
    if not app_migrations_dir.is_dir():
        return
    for path in sorted(app_migrations_dir.glob('*.py')):
        yield path, path.read_text(encoding=_ENCODING)


def digest_of(operations: list[str]) -> str:
    """Content digest for an operation set.

    Not a security boundary -- it identifies "these exact operations were already
    written" -- hence ``usedforsecurity=False``, which also keeps the call legal on a
    FIPS-enabled build where plain md5 raises.

    Callers must render operations deterministically (sorted tables, sorted dict
    literals); an unstable rendering produces a new digest on every run and therefore
    a new migration on every run.
    """
    return hashlib.md5('\n'.join(operations).encode(), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# Writing migrations
# ---------------------------------------------------------------------------


def create_empty_migration_file(app: AppConfig, name: str) -> str:
    """Scaffold via ``makemigrations --empty`` and return the created filename.

    Django prints the path it wrote rather than returning it, so it is parsed back out
    of the captured output. A failure to match is raised rather than guessed at: the
    alternative is rewriting whichever file the glob happened to find first.

    Either separator, because the anchor is there to prove this is a *path* rather than
    prose mentioning a filename -- and on Windows the path Django prints is separated by
    backslashes, where insisting on ``/`` turns every generation into the CommandError
    below.

    By the time that CommandError can be raised, Django has already written the empty
    scaffold to disk -- this function has no reference to its name to clean it up, and
    deleting a file this command didn't stamp is a bigger footgun than leaving it (a
    write interrupted between "file exists" and "our marker is on it" should never be
    guessed at and removed out from under whatever produced it). It is left in place,
    on purpose: unstamped, it carries no ``[DIGEST:...]``, so a later run can never
    mistake it for already covering anything, and the message below names where to look.
    """
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


def write_migration_file(
    app: AppConfig,
    migration_file: str,
    operations: list[str],
    operations_digest: str,
    *,
    generated_by: str,
    dependencies: list[tuple[str, str]] | None = None,
) -> None:
    """Rewrite an ``--empty`` scaffold to carry *operations*.

    *generated_by* names the command in the first-line marker.

    There is deliberately no import to insert. Operations carry their SQL literally, so a
    generated migration imports nothing from the kit -- a migration that reaches back into
    the package at ``migrate`` time is a migration whose meaning changes when the package is
    upgraded, which is the opposite of what Django freezes migration files for.
    """
    file_path = migrations_dir(app) / migration_file
    lines = file_path.read_text(encoding=_ENCODING).splitlines(keepends=True)

    # Replace Django's own "Generated by" line with ours, carrying the digest.
    lines[0] = f'# Generated by {generated_by} command! [DIGEST:{operations_digest}]\n'

    # Append indented operations before the closing bracket of ``operations = [``.
    for operation in operations:
        indented = textwrap.indent(operation, ' ' * 8)
        for line in indented.split('\n'):
            lines.insert(-1, f'{line}\n')

    # Depend on the singleton function migration(s) where needed. Skip self-references
    # and anything Django's scaffold already wrote (it depends on the latest migration,
    # which may well be the function migration itself), so it is never listed twice.
    migration_stem = Path(migration_file).stem
    for dependency in dependencies or []:
        if migration_stem == dependency[1]:
            continue
        if any(f'"{dependency[1]}"' in line or f"'{dependency[1]}'" in line for line in lines):
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
