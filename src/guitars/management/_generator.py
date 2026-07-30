"""Shared mechanics for the commands that generate raw-SQL migrations.

Two commands write migrations the same way -- ``makeguitarmigrations`` (triggers and
rules) and ``maketenantmigrations`` (row-level-security policies) -- and the
mechanics are the delicate part, not the SQL:

* **Idempotency has two layers.** A content digest stamped on the migration's first
  line (``[DIGEST:...]``), plus each command's own regex scan of existing migration
  files for the comment header it emits per operation. Either alone is insufficient:
  the digest catches an unchanged operation set, the header scan catches a *partially*
  covered app so only the genuinely new operations get written.
* **Scaffolding re-enters ``makemigrations``.** These commands do not template a
  migration from scratch; they run ``makemigrations --empty`` and rewrite the result,
  which is why the ``makemigrations`` override must skip its guitar/tenant step on
  ``--empty`` or the two recurse.

Keeping one copy means a fix lands for both. The two commands diverged in exactly this
code before it was shared, and the import-insertion logic below is the scar.

This module is deliberately about *files*, not models: what belongs in a migration is
each command's own business.
"""

from __future__ import annotations

import hashlib
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
    'migration_with_digest_exists',
    'validate_app_labels',
    'write_migration_file',
]

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


def iter_migration_files(app: AppConfig) -> Iterator[tuple[Path, str]]:
    """Yield ``(path, content)`` for every migration file in *app*."""
    migrations_dir = Path(app.path) / 'migrations'
    if not migrations_dir.is_dir():
        return
    for path in migrations_dir.glob('*.py'):
        yield path, path.read_text()


def migration_with_digest_exists(app: AppConfig, operations_digest: str) -> bool:
    """Whether *app* already has a generated migration stamped with this digest."""
    migrations_dir = Path(app.path) / 'migrations'
    if not migrations_dir.is_dir():
        return False
    for path in migrations_dir.glob('*.py'):
        first_line = path.read_text().split('\n', 1)[0]
        match = RE_DIGEST.search(first_line)
        if match and match.group('digest') == operations_digest:
            return True
    return False


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


def create_empty_migration_file(app: AppConfig, name: str) -> str:  # pragma: no cover
    """Scaffold via ``makemigrations --empty`` and return the created filename.

    Django prints the path it wrote rather than returning it, so it is parsed back out
    of the captured output. A failure to match is raised rather than guessed at: the
    alternative is rewriting whichever file the glob happened to find first.
    """
    buf = StringIO()
    call_command('makemigrations', app.label, '--name', name, '--empty', stdout=buf)
    output = buf.getvalue()

    match = re.search(rf'/(?P<filename>\d{{4}}_{re.escape(name)}\.py)', output)
    if not match:
        raise CommandError(f'Could not find the created migration file! Command output: {output}')

    return match.group('filename')


def write_migration_file(
    app: AppConfig,
    migration_file: str,
    operations: list[str],
    operations_digest: str,
    *,
    generated_by: str,
    import_line: str,
    dependencies: list[tuple[str, str]] | None = None,
) -> None:
    """Rewrite an ``--empty`` scaffold to carry *operations*.

    *generated_by* names the command in the first-line marker; *import_line* is the
    module import the operations need (``from guitars import sql``, say).
    """
    file_path = Path(app.path) / 'migrations' / migration_file
    lines = file_path.read_text().splitlines(keepends=True)

    # Replace Django's own "Generated by" line with ours, carrying the digest.
    lines[0] = f'# Generated by {generated_by} command! [DIGEST:{operations_digest}]\n'

    # Insert the import after the scaffold's last import, found rather than assumed at
    # a fixed offset: a change to Django's --empty template would otherwise land the
    # import inside the class body. For the current template this is the same position
    # a hard-coded index produced, so generated output is unchanged.
    last_import = max(
        (i for i, line in enumerate(lines) if line.startswith(('import ', 'from '))),
        default=0,
    )
    lines.insert(last_import + 1, '\n')
    lines.insert(last_import + 2, f'{import_line}\n')

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
        dep_line = f'        ("{dependency[0]}", "{dependency[1]}"),\n'
        dep_idx = next(i for i, line in enumerate(lines) if 'dependencies = [' in line)
        lines.insert(dep_idx + 1, dep_line)

    file_path.write_text(''.join(lines))
