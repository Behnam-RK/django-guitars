"""Generate the kit's **enforcement migrations**.

Vocabulary, used consistently here and in the docs:

* An **enforcement migration** is a generated migration of ``RunSQL`` operations. Django's
  own migrations describe *schema* -- which tables and columns exist. These describe what
  the database *guarantees about the rows*, for every code path including the ones that
  never call ``save()``.
* An **enforcement operation** is one ``RunSQL`` entry inside such a migration.

There are four kinds, and each already has a precise name of its own:

============================  ===============================================
timestamp trigger             keeps ``_updated_at`` current on any ``UPDATE``
soft-delete rule              rewrites ``DELETE`` into a ``_deleted_at`` stamp
MTI redirect rule / trigger   applies both to a multi-table-inheritance child
tenant policy                 row-level security scoping rows to a tenant
============================  ===============================================

All four ship from one command because they share every mechanic that is actually
difficult: model discovery, MTI column-ownership resolution, dedupe against operations
already written, ``--empty`` scaffolding, digest stamping and app scoping.

**Idempotency has three layers**, and all matter. A ``[DIGEST:...]`` marker on the first
line identifies an unchanged operation set; per-operation comment headers
(``# Updated at Trigger on "x" table!``) identify which tables are already covered; and a
``[SQL:...]`` identity on each header identifies whether the covered table's operation is
the SQL the kit emits *today* -- without it, a table whose header was recognised read as
covered forever, so an edited SQL constant shipped no migration at all. A *partially*
covered app gets only the genuinely new or outdated operations. Those header strings are
therefore **frozen**: reword one and every existing migration stops being recognised, and
the next run emits duplicates. See ``docs/migrations.md`` for the full account.
"""

from __future__ import annotations

import re
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from django.apps import apps as django_apps
from django.conf import settings
from django.core.management import CommandError
from django.core.management.base import BaseCommand
from django.db import models

from guitars import sql
from guitars.introspection import column_owner, has_column, is_mti_child, owns_column
from guitars.management import _generator
from guitars.tenancy.discovery import app_coverage


if TYPE_CHECKING:
    from django.apps import AppConfig

    from guitars.tenancy.discovery import TableCoverage


# ---------------------------------------------------------------------------
# Operation headers
# ---------------------------------------------------------------------------
# The dedupe keys. Frozen strings -- see the module docstring. Kept as bare header
# templates, separate from the ``RunSQL`` body they precede, so the emitter below and the
# ``_RE_*`` scanners further down are describing one thing rather than two.

HEADER_TRIGGER_FUNCTION = '# Define function for updated at triggers!'
HEADER_UPDATED_AT = '# Updated at Trigger on "{table}" table!'
HEADER_SOFT_DELETE = '# Soft Delete Rule on "{table}" table!'
HEADER_SOFT_DELETE_RELATED = (
    '# Soft Delete Related Rule on "{related_table}" that is related to "{table}"!'
)
# A second CASCADE FK from the same related_table to the same owner table needs a header
# distinct from the first's, or this one and the regex below collide on the same dedupe key
# -- see sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA's comment for why the underlying
# PostgreSQL rule name has to disambiguate too. _cascade_candidates below picks one FK per
# pair (sorted first by column) to keep the plain header above unchanged; every other FK on
# the same pair gets this one, naming which column it is.
HEADER_SOFT_DELETE_RELATED_VIA = (
    '# Soft Delete Related Rule on "{related_table}" that is related to "{table}" '
    'via "{foreign_key}"!'
)

# --- Multi-table inheritance (MTI) operations ---

HEADER_PARENT_TRIGGER_FUNCTION = '# Define function for MTI parent updated at triggers!'
HEADER_MTI_UPDATED_AT = (
    '# MTI Updated at Trigger on "{child_table}" table (parent "{parent_table}")!'
)
HEADER_MTI_SOFT_DELETE = (
    '# MTI Soft Delete Rule on "{child_table}" table (parent "{parent_table}")!'
)

# --- Tenancy ---

HEADER_TENANT_POLICY = '# Tenant RLS on "{table}" table! [POLICY:{identity}]'
# A policy whose *shape* changed, rather than one that did not exist. PostgreSQL has no
# CREATE OR REPLACE POLICY, so re-emitting the CREATE form would fail migrate with "policy
# tenant_scope already exists" -- see ``sql.replace_table_rls``.
HEADER_TENANT_POLICY_REPLACED = '# Tenant RLS replaced on "{table}" table! [POLICY:{identity}]'
HEADER_TENANT_FORCE = '# Tenant FORCE RLS on "{table}" table!'


# ---------------------------------------------------------------------------
# Rendering an operation
# ---------------------------------------------------------------------------


def _as_list(statements: str | list[str]) -> list[str]:
    return [statements] if isinstance(statements, str) else list(statements)


def _sql_string_literal(text: str) -> str:
    """One SQL statement as a Python literal.

    Triple-quoted where the text allows it, because a reviewer reads these in a diff and
    ``repr`` collapses a twenty-line rule to one line of ``\\n``. The guard is narrow on
    purpose: a trailing double quote would run into the closing delimiter, an embedded
    triple quote would close it early, and a backslash would be re-interpreted as an escape.
    """
    if '"""' in text or '\\' in text or text.endswith('"'):
        return repr(text)
    return f'"""{text}"""'


def _sql_literal(statements: str | list[str]) -> str:
    """Rendered SQL as a Python literal, for embedding in a generated migration.

    Generated migrations carry their SQL **literally** rather than doing
    ``from guitars import sql`` and naming the constants. Django freezes model state into
    migration files so replaying history reproduces the same database; a migration that
    reads a library constant at ``migrate`` time un-freezes precisely that -- a fresh
    ``migrate`` on one version of the kit and an incrementally-migrated database on another
    end up differing while sharing an identical migration history.

    That indirection is also why a change to a SQL constant used to ship no migration at
    all: the ``[DIGEST:...]`` marker covers the operation source, and the source named the
    constant rather than containing it, so no edit to the SQL could ever move the digest.
    """
    if isinstance(statements, str):
        return _sql_string_literal(statements)
    inner = ''.join(f'        {_sql_string_literal(item)},\n' for item in statements)
    return f'[\n{inner}    ]'


def _sql_digest(forward: str | list[str], reverse: str | list[str]) -> str:
    """Content digest of one operation's SQL, stamped into its header as ``[SQL:...]``.

    This is what makes a changed SQL constant generate its own migration, and it is the
    reason the header carries it rather than the file-level ``[DIGEST:...]``: the per-table
    ``_RE_*`` scan short-circuits first, reporting the table covered, so no operation is
    ever built and the file digest is never reached. The short-circuit is the header, so
    the header is where the content identity has to live.

    Always taken over the **canonical** (create) form of the forward SQL, never over the
    replace or adopt form actually emitted -- see :func:`_operation`.
    """
    return _generator.digest_of([*_as_list(forward), '--', *_as_list(reverse)])[:12]


def _operation(
    header: str,
    forward: str | list[str],
    reverse: str | list[str],
    *,
    emit: str | list[str] | None = None,
) -> tuple[str, str]:
    """Render one ``RunSQL`` operation, returning ``(source, digest)``.

    *emit* substitutes the SQL actually written -- the replace or adopt form -- while the
    digest stays keyed to the canonical *forward*. The two must not be conflated: the
    digest answers "which definition is installed", the form answers "how do I install it
    given what the migration history records". Digesting the emitted form instead makes
    successive runs disagree forever -- a fresh database records the create form's digest,
    the next run builds the replace form to compare against it, the two differ, and a
    replacement migration is written on every run.
    """
    digest = _sql_digest(forward, reverse)
    source = (
        f'{header} [SQL:{digest}]\n'
        f'migrations.RunSQL(\n'
        f'    sql={_sql_literal(emit if emit is not None else forward)},\n'
        f'    reverse_sql={_sql_literal(reverse)},\n'
        f'),\n'
    )
    return source, digest


# Regex patterns for recognising enforcement operations already written to migration files.
# These headers are the dedupe keys -- see the module docstring on why they are frozen.
#
# Most are *derived* from the HEADER_* template they recognise, rather than hand-typed a
# second time: the two used to be independent copies of the same information with nothing
# keeping them in sync, which is how a header could be reworded (or a regex "cleaned up")
# without its counterpart changing to match -- a silent duplicate-migration or silent
# no-op, the worst failure this tool has. Deriving one from the other makes that class of
# drift impossible for every pair that can be derived at all.
_PLACEHOLDER_IN_ESCAPED_TEMPLATE = re.compile(r'\\\{(\w+)\\\}')


def _derive_scanner(header_template: str) -> re.Pattern[str]:
    """Turn a frozen ``HEADER_*`` template into the regex that recognises it.

    Escapes the template's literal text, then turns each escaped ``{field}``
    placeholder into a positional, quote-delimited capture group -- every placeholder
    in the templates this is applied to sits inside a pair of double quotes, so that is
    the only shape handled. A header that fuses two forms, needs a *named* group, or
    must NOT capture one of its own placeholders (see the MTI, soft-delete-related and
    tenant-policy patterns below) resists derivation for a specific, commented reason,
    and stays hand-written.
    """
    escaped = re.escape(header_template)
    pattern = _PLACEHOLDER_IN_ESCAPED_TEMPLATE.sub(r'([^"]+)', escaped)
    return re.compile(pattern)


# The two function patterns used to match the *constant reference*
# (``sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION``) rather than the comment header. Now that
# generated migrations inline their SQL there is no such reference to find, so they match
# the header like every other kind. Both forms of migration carry the header, so this
# recognises the ones already written as well as the ones written from here on.
_RE_TRIGGER_FUNCTION = _derive_scanner(HEADER_TRIGGER_FUNCTION)
_RE_PARENT_TRIGGER_FUNCTION = _derive_scanner(HEADER_PARENT_TRIGGER_FUNCTION)
_RE_UPDATED_AT = _derive_scanner(HEADER_UPDATED_AT)
_RE_SOFT_DELETE = _derive_scanner(HEADER_SOFT_DELETE)
_RE_TENANT_FORCE = _derive_scanner(HEADER_TENANT_FORCE)

# Fuses HEADER_SOFT_DELETE_RELATED and HEADER_SOFT_DELETE_RELATED_VIA -- one optional
# trailing group standing in for two header forms -- so hand-written rather than
# mechanically derived from either alone. Deliberately does not consume the header's
# trailing ``!``: it stops right after the (optional) foreign_key's closing quote, one
# character short of the literal text. That is harmless, not an oversight to "fix" -- the
# ``[SQL:...]`` identity below is read from the *tail* of the header line independently of
# where this match ends, so an unconsumed ``!`` in between changes nothing it reads.
_RE_SOFT_DELETE_RELATED = re.compile(
    r'# Soft Delete Related Rule on "([^"]+)" that is related to "([^"]+)"'
    r'(?: via "(?P<foreign_key>[^"]+)")?'
)
# MTI headers carry a leading "MTI " token, so they never collide with the single-table
# patterns above (which anchor on ``# Updated`` / ``# Soft`` immediately after the comment
# mark). Hand-written rather than derived: naively deriving HEADER_MTI_UPDATED_AT /
# HEADER_MTI_SOFT_DELETE would also capture ``parent_table`` out of ``(parent "...")!``,
# and matching on it would mean a parent model's own restructuring (e.g. a table rename)
# reads an MTI child's still-correct trigger as "not covered" and duplicates it. Both stop
# right after the child table's closing quote, before ``(parent "..."`` even starts.
_RE_MTI_UPDATED_AT = re.compile(r'# MTI Updated at Trigger on "([^"]+)" table')
_RE_MTI_SOFT_DELETE = re.compile(r'# MTI Soft Delete Rule on "([^"]+)" table')
# Matches both policy forms -- the initial CREATE and a later replacement -- because for
# every purpose here they mean the same thing: "this table's policy is recorded in a
# migration, and this is the shape it was written with". The ``[POLICY:...]`` identity is
# what makes a *changed* shape detectable at all; without it the table name alone said only
# that some policy existed, so a model gaining a tenant dimension silently kept the old,
# weaker predicate while --check reported nothing to do. Hand-written, not derived: it fuses
# two header forms via one optional group, which a single-template deriver cannot express.
#
# The FORCE header carries an extra token before "RLS", so it can never match this pattern.
_RE_TENANT_POLICY = re.compile(
    r'# Tenant RLS (?:replaced )?on "([^"]+)" table! \[POLICY:(?P<identity>\w+)\]'
)
# The [DIGEST:...] marker is matched by _generator.RE_DIGEST.

# The per-operation content digest, read off whatever remains of the header line after one
# of the patterns above matched. Deliberately a *separate* pattern applied to the tail
# rather than an optional group bolted onto each one: every header regex above stays
# byte-identical to the frozen strings it has always been, and each still recognises a
# migration written before this token existed. An absent token means "written before the
# SQL was inlined", which reads as stale and is re-emitted once -- the mechanism by which
# the 1.0.0 soft-delete guard fix finally generates itself on an existing project.
_RE_SQL_IDENTITY = re.compile(r'\[SQL:(?P<sql>\w+)\]')

# Whether a policy operation's forward SQL forces row-level security. The lookbehind is not
# optional: every policy operation's ``reverse_sql`` carries ``NO FORCE ROW LEVEL SECURITY``
# in the same slice of text, and without it every table reads as forced.
_RE_FORCED = re.compile(r'(?<!NO )FORCE ROW LEVEL SECURITY')


def _recorded_sql_identity(content: str, match: re.Match) -> str | None:
    """The ``[SQL:...]`` digest on the header line *match* landed on, or ``None``.

    ``None`` covers both "no token" (a pre-inlining migration) and a header this token was
    never written to. Callers treat it as stale rather than as covered, which is the whole
    point: the previous behaviour was to treat any recognised header as current forever.
    """
    line_end = content.find('\n', match.end())
    tail = content[match.end() : line_end if line_end != -1 else len(content)]
    found = _RE_SQL_IDENTITY.search(tail)
    return found.group('sql') if found else None


def unforced_policy_tables(content: str) -> set[str]:
    """Tables whose policy operation in *content* was written without FORCE.

    The only kind ``--force-rls`` has anything to do for: the migration text is the record
    of what was decided when it was generated. Without this the flag emitted a redundant FORCE
    migration for every tenanted table on any project using the default
    ``GUITARS_RLS_FORCE = True``.

    Two formats, because both exist in the wild. A migration written before the SQL was
    inlined records the decision as a ``force=False`` keyword argument; one written since
    records it by containing -- or not containing -- an
    ``ALTER TABLE ... FORCE ROW LEVEL SECURITY`` statement. Reading only the second would
    make every legacy operation look unforced and put already-forced tables back on the
    FORCE backlog, so the keyword is honoured wherever it is still present.

    Each operation is inspected **only within its own text**, bounded by the next policy header.
    A single regex spanning a few lines after the header cannot do this: operations are about
    that long, so a lazy match from a ``force=True`` operation reaches into the next one, claims
    *its* ``force=False``, and consumes the header on the way -- flagging the table that is
    already forced and missing the one that is not. Exactly backwards, and only on a file with
    two adjacent operations, which is every real file.

    Within a file the **last** operation for a table wins, the same rule the caller applies
    across files. Accumulating into a set instead would let a table that shipped
    ``force=False`` and was later replaced with ``force=True`` in the same file stay on the
    FORCE backlog forever -- a redundant migration for a table that is already forced, which
    is the bug this function was extracted to fix, one scope smaller.
    """
    matches = list(_RE_TENANT_POLICY.finditer(content))
    state: dict[str, bool] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        operation = content[match.end() : end]
        state[match.group(1)] = (
            'force=False' in operation
            if 'force=' in operation
            else not _RE_FORCED.search(operation)
        )
    return {table for table, unforced in state.items() if unforced}


def _literal(value: object) -> str:
    """Render a value into a generated migration, deterministically.

    Dicts and sets are emitted in sorted order so the content digest is stable. An unstable
    rendering produces a new digest -- and therefore a new migration -- on every run.

    Scalars (strings included) go through ``repr``, which is what makes the output valid
    Python for every value rather than only for the tidy ones: hand-building a string as
    ``f"'{value}'"`` renders ``db_column="o'brien"`` as a syntax error and a backslash as an
    escape sequence. For every identifier the kit accepts -- ``sql.policy._bare`` requires a
    plain lower-case one -- ``repr`` produces the identical single-quoted text, so digests of
    existing migrations are unchanged.
    """
    if isinstance(value, dict):
        items = ', '.join(f'{_literal(k)}: {_literal(v)}' for k, v in sorted(value.items()))
        return '{' + items + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ', '.join(_literal(item) for item in value) + ']'
    return repr(value)


class ExistingOperations(NamedTuple):
    """Which enforcement operations the migration files already contain.

    Scanned once at construction, by comment header, so a partially covered app receives
    only the operations it is genuinely missing. Named rather than a positional tuple: this
    is ten fields and was once an anonymous one, and a caller unpacking ten sets in the right
    order is a bug waiting to happen.

    Each of the first five maps its key to the ``[SQL:...]`` digest the **most recent**
    operation for that key was written with, or ``None`` for a header from before that token
    existed. Mappings rather than sets because "is this table covered" and "is it covered by
    the SQL the kit emits today" are different questions, and answering only the first is how
    the 1.0.0 soft-delete guard rewrite reached every existing database as a no-op. ``in``
    behaves identically on a dict, so membership tests elsewhere read unchanged.
    """

    triggers: dict[str, str | None]
    soft_deletes: dict[str, str | None]
    #: Keyed on (related_table, table, foreign_key) -- the third element is ``None`` for
    #: the one FK per pair that keeps the plain, historical header (see
    #: HEADER_SOFT_DELETE_RELATED_VIA's comment), or the FK's column for any other FK on
    #: the same pair.
    soft_delete_related: dict[tuple[str, str, str | None], str | None]
    mti_triggers: dict[str, str | None]
    mti_soft_deletes: dict[str, str | None]
    tenant_policies: set[str]
    #: Table -> the ``[POLICY:...]`` identity its **most recent** policy operation was written
    #: with. Compared against the identity the models imply now, so a coverage shape that
    #: changed produces a replacement instead of being mistaken for already covered. Most
    #: recent, not any: a shape taken A -> B -> A must match the migration applied last, so
    #: ``_generator.iter_migration_files`` yields in filename order.
    #:
    #: Kept alongside :attr:`tenant_policy_sql` rather than folded into it, because the two
    #: answer different questions. The identity is what the policy *says*, with ``force``
    #: deliberately excluded so flipping ``GUITARS_RLS_FORCE`` cannot trigger a full
    #: replacement and defeat the staged ``--force-rls`` retrofit. The SQL digest is whether
    #: the text is current. Either one changing means the table needs a replacement.
    tenant_policy_identities: dict[str, str]
    #: Table -> the ``[SQL:...]`` digest of its most recent policy operation, or ``None``.
    tenant_policy_sql: dict[str, str | None]
    #: Tables whose policy operation was written with ``force=False`` -- see
    #: :func:`unforced_policy_tables`. These are the only ones a second FORCE stage can act on.
    unforced_policies: set[str]
    tenant_forces: set[str]
    trigger_function_dependency: tuple[str, str] | None
    parent_trigger_function_dependency: tuple[str, str] | None
    #: The ``[SQL:...]`` digest of the most recent migration defining each singleton trigger
    #: function, or ``None`` for one written before the token existed. The functions are
    #: singletons by *existence*, which is why a change to either body previously shipped
    #: nothing at all: the first thing both ensure methods did was return early.
    trigger_function_sql: str | None
    parent_trigger_function_sql: str | None


class Command(BaseCommand):
    """Generates the enforcement migrations: triggers, rules and tenant policies.

    Each kind is driven by the shape of the model, so declaring the thing *is* the opt-in
    and there is no registry to keep in step:

    * ``_updated_at`` -> a statement-level timestamp trigger.
    * ``_deleted_at`` -> a soft-delete rule, plus cascade rules for related
      soft-deletable models whose FK is ``on_delete=CASCADE``.
    * a ``TenantedManager`` -> a row-level-security tenant policy.

    Multi-table inheritance is handled throughout: because the relevant columns physically
    live on an ancestor's table, each column's owner is resolved via
    ``guitars.introspection`` rather than ``hasattr``, and an MTI child gets the
    parent-propagating trigger, the redirect rule, and the owner-join policy instead of the
    own-table forms. See ``docs/mti.md``.

    Run after ``makemigrations`` when models change -- or let ``makemigrations`` run it for
    you, which is the default.
    """

    help = (
        'Creates enforcement migrations (timestamp triggers, soft-delete rules, tenant policies).'
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.all_models: list[type[models.Model]] = []
        self.reverse_relations_mapping: defaultdict[type[models.Model], set] = defaultdict(set)
        self._setup_models_and_reverse_relations()

        # (app_label, migration_stem) tuples or None, pointing at the singleton function migrations.
        self.trigger_function_dependency: tuple[str, str] | None = None
        self.parent_trigger_function_dependency: tuple[str, str] | None = None

        # Cross-app / MTI cascade rules skipped this run, surfaced as warnings (not silent).
        self._mti_cascade_warnings: list[str] = []
        # Tables tenancy discovery could not cover, with the reason. Also surfaced.
        self._tenancy_notes: list[str] = []

        # Set from --adopt in handle(). Read by _append_if_stale and _tenant_operations, which
        # run per-app during handle() and so always see the resolved value; the default here
        # keeps the command importable and directly instantiable in tests.
        self._adopt: bool = False

        self.existing = self._scan_existing_operations()
        self.trigger_function_dependency = self.existing.trigger_function_dependency
        self.parent_trigger_function_dependency = self.existing.parent_trigger_function_dependency
        # Paired with the two dependencies above, and mutable alongside them for the same
        # reason: a singleton function migration is only "already done" when it both exists
        # and defines the SQL the kit emits today.
        self.trigger_function_sql = self.existing.trigger_function_sql
        self.parent_trigger_function_sql = self.existing.parent_trigger_function_sql

    def add_arguments(self, parser):
        parser.add_argument(
            'args',
            metavar='app_label',
            nargs='*',
            help='Optional app labels to scope generation to (default: all LOCAL_APPS).',
        )
        parser.add_argument(
            '--check',
            action='store_true',
            dest='check_only',
            help=(
                'Exit with a non-zero status if model changes are missing migrations '
                "and don't actually write them."
            ),
        )
        parser.add_argument(
            '--adopt',
            action='store_true',
            dest='adopt',
            help=(
                'Re-emit every enforcement operation for the apps in scope, in a form that '
                'is correct whether or not the database object already exists. For adopting '
                'a database whose triggers, rules or policies were created outside this '
                'command -- by hand, or by another generator whose comment headers this one '
                'cannot read.'
            ),
        )
        parser.add_argument(
            '--force-rls',
            action='store_true',
            dest='force_rls',
            help=(
                'Generate FORCE ROW LEVEL SECURITY migrations for tables whose tenant '
                'policies already exist, and nothing else. Only needed when '
                'GUITARS_RLS_FORCE is False, which defers FORCE to a second stage for a '
                'retrofit onto a populated database.'
            ),
        )

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @staticmethod
    def _tenant_policies_enabled() -> bool:
        """Whether to emit tenant policies at all.

        ``False`` keeps the Python enforcement layer while leaving the database alone --
        for adopting the loud layer first, or for a database where the application role
        cannot own its tables and so could never be constrained by RLS anyway.
        """
        return bool(getattr(settings, 'GUITARS_TENANT_POLICIES', True))

    @staticmethod
    def _rls_force_enabled() -> bool:
        return bool(getattr(settings, 'GUITARS_RLS_FORCE', True))

    @staticmethod
    def _rls_exempt_roles() -> list[str]:
        return list(getattr(settings, 'GUITARS_RLS_EXEMPT_ROLES', []))

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def _setup_models_and_reverse_relations(self) -> None:
        """Populate ``all_models`` and ``reverse_relations_mapping`` from installed apps."""
        for app in django_apps.get_app_configs():
            self.all_models.extend(app.get_models())

        for model in self.all_models:
            for field in model._meta.get_fields():
                if isinstance(field, models.ForeignKey):
                    self.reverse_relations_mapping[field.related_model].add(
                        (model, field, field.remote_field.on_delete)
                    )

    # ------------------------------------------------------------------
    # Migration-file scanning
    # ------------------------------------------------------------------

    def _scan_existing_operations(self) -> ExistingOperations:
        """Scan every local app's migration files for enforcement operations already written.

        Recognition is by comment header, per operation, so an app that is partially covered
        receives exactly the operations it lacks rather than a duplicate of the whole set.
        """
        # Table (or table pair) -> the [SQL:...] digest of its most recent operation.
        # Last write wins throughout, which is only the currently-applied answer because
        # _generator.iter_migration_files yields in filename order -- see its docstring.
        existing_triggers: dict[str, str | None] = {}
        existing_soft_deletes: dict[str, str | None] = {}
        existing_soft_delete_related: dict[tuple[str, str, str | None], str | None] = {}
        existing_mti_triggers: dict[str, str | None] = {}
        existing_mti_soft_deletes: dict[str, str | None] = {}
        existing_tenant_policies: set[str] = set()
        existing_policy_identities: dict[str, str] = {}
        existing_policy_sql: dict[str, str | None] = {}
        #: Table -> whether its *most recent* policy operation was written ``force=False``.
        #: A mapping rather than a set so a later operation can take a table back off the
        #: FORCE backlog; see where it is filled.
        existing_policy_force: dict[str, bool] = {}
        existing_tenant_forces: set[str] = set()
        trigger_function_dep: tuple[str, str] | None = None
        parent_trigger_function_dep: tuple[str, str] | None = None
        trigger_function_sql: str | None = None
        parent_trigger_function_sql: str | None = None

        for app in django_apps.get_app_configs():
            if not _generator.is_local(app):
                continue
            for path, content in _generator.iter_migration_files(app):
                function_match = _RE_TRIGGER_FUNCTION.search(content)
                if function_match:
                    trigger_function_dep = (app.label, path.stem)
                    trigger_function_sql = _recorded_sql_identity(content, function_match)
                parent_match = _RE_PARENT_TRIGGER_FUNCTION.search(content)
                if parent_match:
                    parent_trigger_function_dep = (app.label, path.stem)
                    parent_trigger_function_sql = _recorded_sql_identity(content, parent_match)

                for match in _RE_UPDATED_AT.finditer(content):
                    existing_triggers[match.group(1)] = _recorded_sql_identity(content, match)
                for match in _RE_SOFT_DELETE.finditer(content):
                    existing_soft_deletes[match.group(1)] = _recorded_sql_identity(content, match)
                for match in _RE_SOFT_DELETE_RELATED.finditer(content):
                    existing_soft_delete_related[
                        (match.group(1), match.group(2), match.group('foreign_key'))
                    ] = _recorded_sql_identity(content, match)
                for match in _RE_MTI_UPDATED_AT.finditer(content):
                    existing_mti_triggers[match.group(1)] = _recorded_sql_identity(content, match)
                for match in _RE_MTI_SOFT_DELETE.finditer(content):
                    existing_mti_soft_deletes[match.group(1)] = _recorded_sql_identity(
                        content, match
                    )
                unforced_in_file = unforced_policy_tables(content)
                for match in _RE_TENANT_POLICY.finditer(content):
                    table = match.group(1)
                    existing_tenant_policies.add(table)
                    # Last write wins, within a file and across them -- files arrive in
                    # filename order, which is application order.
                    existing_policy_identities[table] = match.group('identity')
                    existing_policy_sql[table] = _recorded_sql_identity(content, match)
                    # Last write wins here too, and for the same reason. Accumulating these
                    # by union instead left a table on the backlog forever once any migration
                    # had written it ``force=False``: a later replacement carrying
                    # ``force=True`` inlines FORCE and so emits no FORCE header for
                    # ``tenant_forces`` to find, and ``--force-rls`` then wrote a redundant
                    # migration for a table that was already forced.
                    existing_policy_force[table] = table in unforced_in_file
                existing_tenant_forces.update(
                    m.group(1) for m in _RE_TENANT_FORCE.finditer(content)
                )

        return ExistingOperations(
            triggers=existing_triggers,
            soft_deletes=existing_soft_deletes,
            soft_delete_related=existing_soft_delete_related,
            mti_triggers=existing_mti_triggers,
            mti_soft_deletes=existing_mti_soft_deletes,
            tenant_policies=existing_tenant_policies,
            tenant_policy_identities=existing_policy_identities,
            tenant_policy_sql=existing_policy_sql,
            unforced_policies={
                table for table, unforced in existing_policy_force.items() if unforced
            },
            tenant_forces=existing_tenant_forces,
            trigger_function_dependency=trigger_function_dep,
            parent_trigger_function_dependency=parent_trigger_function_dep,
            trigger_function_sql=trigger_function_sql,
            parent_trigger_function_sql=parent_trigger_function_sql,
        )

    # ------------------------------------------------------------------
    # Migration-file helpers
    # ------------------------------------------------------------------

    def _get_trigger_function_host_app(self) -> AppConfig:
        """Return the ``AppConfig`` that will host the singleton trigger-function migration."""
        host_app_name = getattr(settings, 'TRIGGER_FUNCTION_APP', None) or settings.LOCAL_APPS[0]
        host_app_label = host_app_name.rsplit('.', 1)[-1]
        return django_apps.get_app_config(host_app_label)

    @staticmethod
    def _write_migration_file(
        app: AppConfig,
        migration_file: str,
        operations: list[str],
        operations_digest: str,
        dependencies: list[tuple[str, str]] | None = None,
    ) -> None:
        """Rewrite a migration file to include the given custom *operations*.

        Thin wrapper naming this command's marker text; the mechanics live in
        ``_generator``, shared with every command that generates migrations.
        """
        _generator.write_migration_file(
            app=app,
            migration_file=migration_file,
            operations=operations,
            operations_digest=operations_digest,
            generated_by='makeguitarmigrations',
            dependencies=dependencies,
        )

    # ------------------------------------------------------------------
    # Trigger function migration
    # ------------------------------------------------------------------

    def _ensure_function_migration(
        self,
        *,
        recorded: tuple[str, str] | None,
        recorded_digest: str | None,
        header: str,
        create: str,
        replace: str,
        drop: str,
        name: str,
        missing_message: str,
        stale_message: str,
        check_only: bool,
        dependencies: list[tuple[str, str]] | None = None,
    ) -> tuple[tuple[str, str], str] | None:
        """Ensure the host app has a current migration for one singleton trigger function.

        Returns ``((app_label, stem), digest)`` for a migration it wrote, or ``None`` if the
        recorded one is already current.

        These two functions are singletons by *existence*, and that is precisely why a change
        to either body used to ship nothing: the first thing this did was return early on
        "a migration mentioning the function exists somewhere". It now also compares the
        recorded ``[SQL:...]`` digest, so an edited body produces a second migration carrying
        the ``OR REPLACE`` form. ``OR REPLACE`` rather than DROP + CREATE is forced, not
        defensive -- ``DROP FUNCTION`` refuses while any trigger depends on it, and CASCADE
        would take every table's trigger with it.

        Under ``--adopt`` the ``OR REPLACE`` form is used even when nothing is recorded at
        all: the whole premise of the flag is a database the generator has no record of, and
        a plain ``CREATE FUNCTION`` there fails migrate with "function already exists" on
        exactly the database ``--adopt`` exists to bring in. There is no separate adopt form
        for a function the way there is for a trigger, because ``OR REPLACE`` is already the
        one form that is correct whether or not the function exists.
        """
        current_source, current_digest = _operation(header, create, drop)
        if recorded is not None and (recorded_digest == current_digest and not self._adopt):
            return None

        stale = recorded is not None
        if check_only:
            raise CommandError(self.style.ERROR(stale_message if stale else missing_message))

        if stale or self._adopt:
            current_source, _ = _operation(header, create, drop, emit=replace)

        host_app = self._get_trigger_function_host_app()
        migration_file = _generator.create_empty_migration_file(host_app, name=name)
        self._write_migration_file(
            app=host_app,
            migration_file=migration_file,
            operations=[current_source],
            operations_digest=_generator.digest_of([current_source]),
            dependencies=dependencies,
        )

        self.stdout.write(
            self.style.MIGRATE_HEADING(f"Enforcement migrations for '{host_app.label}':")
        )
        self.stdout.write(f'  migrations/{migration_file}')
        return (host_app.label, Path(migration_file).stem), current_digest

    def _ensure_trigger_function_migration(self, *, check_only: bool = False) -> bool:
        """
        Ensure a current standalone migration for the trigger function exists in the host app.
        Sets ``self.trigger_function_dependency`` when done.
        Returns True if a new migration was created, False if the recorded one is current.
        """
        written = self._ensure_function_migration(
            recorded=self.trigger_function_dependency,
            recorded_digest=self.trigger_function_sql,
            header=HEADER_TRIGGER_FUNCTION,
            create=sql.CREATE_UPDATED_AT_TRIGGER_FUNCTION,
            replace=sql.REPLACE_UPDATED_AT_TRIGGER_FUNCTION,
            drop=sql.DROP_UPDATED_AT_TRIGGER_FUNCTION,
            name='auto_enforcement_trigger_function',
            missing_message=(
                '\n\tRun `manage.py makeguitarmigrations` to create '
                'the trigger function migration!\n'
            ),
            stale_message=(
                '\n\tThe updated-at trigger function has changed since the migration that '
                'defines it was written.\n\tRun `manage.py makeguitarmigrations` to '
                'regenerate it.\n'
            ),
            check_only=check_only,
        )
        if written is None:
            return False
        self.trigger_function_dependency, self.trigger_function_sql = written
        return True

    def _ensure_parent_trigger_function_migration(self, *, check_only: bool = False) -> bool:
        """
        Ensure a current standalone migration for the MTI parent updated-at function exists.
        Sets ``self.parent_trigger_function_dependency`` when done. Kept separate from the
        base trigger-function migration so that adding MTI support never re-digests (and thus
        regenerates) the existing single-table function migration.
        Returns True if a new migration was created, False if the recorded one is current.
        """
        written = self._ensure_function_migration(
            recorded=self.parent_trigger_function_dependency,
            recorded_digest=self.parent_trigger_function_sql,
            header=HEADER_PARENT_TRIGGER_FUNCTION,
            create=sql.CREATE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
            replace=sql.REPLACE_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
            drop=sql.DROP_PARENT_UPDATED_AT_TRIGGER_FUNCTION,
            name='auto_enforcement_parent_trigger_function',
            missing_message=(
                '\n\tRun `manage.py makeguitarmigrations` to create '
                'the MTI parent trigger function migration!\n'
            ),
            stale_message=(
                '\n\tThe MTI parent updated-at trigger function has changed since the '
                'migration that defines it was written.\n\tRun '
                '`manage.py makeguitarmigrations` to regenerate it.\n'
            ),
            check_only=check_only,
            dependencies=[self.trigger_function_dependency]
            if self.trigger_function_dependency
            else None,
        )
        if written is None:
            return False
        self.parent_trigger_function_dependency, self.parent_trigger_function_sql = written
        return True

    # ------------------------------------------------------------------
    # Per-app operations
    # ------------------------------------------------------------------

    def _policy_identity(self, table: str, coverage: TableCoverage) -> str:
        """Digest of everything that determines what the ``tenant_scope`` policy *says*.

        Stamped into the operation's comment header so a later run can tell "this table has a
        policy" from "this table has the policy the models currently imply" -- the distinction
        the header lacked, which let a model gain a tenant dimension while the database kept
        predicating on the old one, with ``--check`` reporting nothing to do.

        ``force`` is deliberately **excluded**. It is an ``ALTER TABLE``, not part of the
        policy definition, and it already has a staged mechanism of its own
        (``--force-rls`` reading ``unforced_policies``). Folding it in would make flipping
        ``GUITARS_RLS_FORCE`` emit a full policy replacement for every table and defeat the
        retrofit workflow that setting exists for.

        Rendered through ``_literal``, so dicts are sorted and the digest is stable across
        runs -- an unstable identity would write a new migration every time.
        """
        identity = {
            'table': table,
            **coverage.as_kwargs(),
            'exempt_roles': self._rls_exempt_roles(),
        }
        return _generator.digest_of([_literal(identity)])[:12]

    def _tenant_policy_operation(
        self, table: str, coverage: TableCoverage, *, replacing: bool
    ) -> tuple[str, str]:
        """One ``tenant_scope`` policy operation, with the environment decisions baked in.

        ``force`` and ``exempt_roles`` are resolved from settings **here**, at generation
        time, and the resulting statements are written into the migration literally, so the
        same migration history always produces the same database -- see ``guitars.sql.policy``.

        *replacing* picks the ``replace_table_rls`` form, for a table that already has a
        policy of a different shape. Its ``reverse_sql`` drops RLS rather than restoring the
        previous predicate, which the generator does not know: reversing past this migration
        therefore leaves the table unpolicied, and rolling forward again rebuilds the current
        shape. That is the honest reverse -- the alternative is a reverse that claims to
        restore a policy it cannot reconstruct.

        The digest returned is over the ``create`` form either way, for the reason given in
        :func:`_operation`: keying it to the emitted form would make every run after a
        replacement disagree with the one before it.
        """
        # Kept out of the coverage mapping rather than merged into it: the coverage kwargs are
        # a typed shape describing what the policy predicates on, while these two are
        # environment decisions resolved here so they can be written into the SQL literally.
        exempt_roles = self._rls_exempt_roles() or None
        force = self._rls_force_enabled()
        coverage_kwargs = coverage.as_kwargs()

        forward = sql.create_table_rls(
            table=table, force=force, exempt_roles=exempt_roles, **coverage_kwargs
        )
        reverse = sql.drop_table_rls(table=table, exempt_roles=exempt_roles)
        header = (HEADER_TENANT_POLICY_REPLACED if replacing else HEADER_TENANT_POLICY).format(
            table=table, identity=self._policy_identity(table, coverage)
        )
        return _operation(
            header,
            forward,
            reverse,
            emit=sql.replace_table_rls(
                table=table, force=force, exempt_roles=exempt_roles, **coverage_kwargs
            )
            if replacing
            else None,
        )

    def _tenant_operations(self, app: AppConfig, *, force_rls: bool) -> list[str]:
        """Tenant-policy operations *app* is missing or has outdated, for the requested stage."""
        if not self._tenant_policies_enabled():
            return []

        coverage = app_coverage(app)
        self._tenancy_notes.extend(coverage.notes)

        operations: list[str] = []
        for table, table_coverage in sorted(coverage.tables.items()):
            if force_rls:
                # Three ways there is nothing to do, and each would otherwise write a
                # migration that changes nothing:
                #   * FORCE already has its own operation;
                #   * no policy operation exists yet -- a coverage gap FORCE must not paper
                #     over by forcing a table that nothing scopes;
                #   * the policy shipped with FORCE inline, which is the default.
                if (
                    table in self.existing.tenant_forces
                    or table not in self.existing.tenant_policies
                    or table not in self.existing.unforced_policies
                ):
                    continue
                force_source, _ = _operation(
                    HEADER_TENANT_FORCE.format(table=table),
                    sql.force_rls(table=table),
                    sql.no_force_rls(table=table),
                )
                operations.append(force_source)
                continue

            # Two independent reasons to replace, and both must be checked. The identity
            # answers "does the policy still say what the models imply" -- a dimension added
            # or dropped, a tenant column renamed, an exempt role edited. The SQL digest
            # answers "is the text the kit emits today the text that was written". Checking
            # only the first is how a policy kept an outdated predicate; checking only the
            # second would miss a shape change that happens to render the same length of SQL.
            recorded_identity = self.existing.tenant_policy_identities.get(table)
            current_identity = self._policy_identity(table, table_coverage)
            create_source, create_digest = self._tenant_policy_operation(
                table, table_coverage, replacing=False
            )

            if self._adopt:
                # The premise of --adopt is a policy that exists in the database but was
                # never recorded here, and PostgreSQL has no CREATE POLICY IF NOT EXISTS --
                # so the CREATE form would fail migrate with "policy tenant_scope already
                # exists". The replace form drops first and is correct either way, which is
                # the only thing the generator can honestly assume at this point.
                replace_source, _ = self._tenant_policy_operation(
                    table, table_coverage, replacing=True
                )
                operations.append(replace_source)
            elif recorded_identity is None:
                operations.append(create_source)
            elif (
                recorded_identity != current_identity
                or self.existing.tenant_policy_sql.get(table) != create_digest
            ):
                replace_source, _ = self._tenant_policy_operation(
                    table, table_coverage, replacing=True
                )
                operations.append(replace_source)
        return operations

    def _append_if_stale(
        self,
        operations: list[str],
        recorded: dict,
        key,
        header: str,
        forward: str | list[str],
        reverse: str | list[str],
        *,
        replace: str | list[str] | None = None,
        adopt: str | list[str] | None = None,
    ) -> None:
        """Append one operation to *operations* unless the recorded one is already current.

        Which of the three forms is written is decided by what the migration history knows,
        and the distinction is deliberate rather than a matter of taste -- ``IF EXISTS`` and
        ``OR REPLACE`` are claims about knowledge, and using them where the answer is known
        turns "your database has diverged from its history" into silence:

        * **Nothing recorded** -> the plain ``forward`` form. A collision then fails
          ``migrate`` loudly, which for an unqualified public-schema name like
          ``set_updated_at()`` is exactly what should happen rather than guitars quietly
          clobbering something that is not ours.
        * **Recorded, but stale** (a different digest, or none at all because the header
          predates the token) -> *replace*. The object is known to be ours, so the drop is
          unguarded and a database that has diverged says so.
        * **``--adopt``** -> *adopt*, the only form carrying ``IF EXISTS``. There the
          premise of the flag is that nobody knows what the database already has, so the
          uncertainty is real and was opted into explicitly.

        Kinds with no *replace*/*adopt* form fall back to *forward*, which is correct for
        anything created ``OR REPLACE`` in the first place.
        """
        source, digest = _operation(header, forward, reverse)
        if self._adopt:
            source, _ = _operation(header, forward, reverse, emit=adopt or replace or forward)
        elif key not in recorded:
            pass  # `source` already holds the create form.
        elif recorded[key] == digest:
            return
        else:
            source, _ = _operation(header, forward, reverse, emit=replace or forward)
        operations.append(source)

    def _build_operations(self, app: AppConfig) -> list[str]:
        """Return a list of SQL operation snippets needed for *app*'s models."""
        operations: list[str] = []
        deferred: list[str] = []

        for model in app.get_models():
            table = model._meta.db_table
            # The *column*, not the field name. The two agree for the ordinary ``id`` primary
            # key, which is why this went unnoticed -- but a model whose pk sets ``db_column``,
            # or is a ``OneToOneField(primary_key=True)`` (name ``owner``, column
            # ``owner_id``), would have produced a rule referencing a column that does not
            # exist and failed at ``migrate``. The MTI branches below already used ``.column``.
            primary_key = model._meta.pk.column

            # --- updated_at trigger: own table vs. MTI parent-propagation ---
            if owns_column(model, '_updated_at'):
                self._append_if_stale(
                    operations,
                    self.existing.triggers,
                    table,
                    HEADER_UPDATED_AT.format(table=table),
                    sql.CREATE_UPDATED_AT_TRIGGER.format(table=table, primary_key=primary_key),
                    sql.DROP_UPDATED_AT_TRIGGER.format(table=table),
                    replace=sql.REPLACE_UPDATED_AT_TRIGGER.format(
                        table=table, primary_key=primary_key
                    ),
                    adopt=sql.ADOPT_UPDATED_AT_TRIGGER.format(
                        table=table, primary_key=primary_key
                    ),
                )
            elif is_mti_child(model, '_updated_at'):
                owner = column_owner(model, '_updated_at')
                mti = {
                    'child_table': table,
                    'child_pk': model._meta.pk.column,
                    'parent_table': owner._meta.db_table,
                    'parent_pk': owner._meta.pk.column,
                }
                self._append_if_stale(
                    operations,
                    self.existing.mti_triggers,
                    table,
                    HEADER_MTI_UPDATED_AT.format(**mti),
                    sql.CREATE_PARENT_UPDATED_AT_TRIGGER.format(**mti),
                    sql.DROP_PARENT_UPDATED_AT_TRIGGER.format(child_table=table),
                    replace=sql.REPLACE_PARENT_UPDATED_AT_TRIGGER.format(**mti),
                    adopt=sql.ADOPT_PARENT_UPDATED_AT_TRIGGER.format(**mti),
                )

            # --- soft-delete rule: own table vs. MTI redirect-to-owner ---
            # Rules need no replace or adopt form: they are created ``OR REPLACE``, which is
            # not defensiveness but the only safe way to redefine one -- an instant without a
            # ``soft_delete`` rule is an instant in which DELETE destroys rows.
            if owns_column(model, '_deleted_at'):
                self._append_if_stale(
                    operations,
                    self.existing.soft_deletes,
                    table,
                    HEADER_SOFT_DELETE.format(table=table),
                    sql.CREATE_SOFT_DELETE_RULE.format(table=table, primary_key=primary_key),
                    sql.DROP_SOFT_DELETE_RULE.format(table=table),
                )
            elif is_mti_child(model, '_deleted_at'):
                owner = column_owner(model, '_deleted_at')
                mti = {
                    'child_table': table,
                    'child_pk': model._meta.pk.column,
                    'parent_table': owner._meta.db_table,
                    'parent_pk': owner._meta.pk.column,
                }
                self._append_if_stale(
                    operations,
                    self.existing.mti_soft_deletes,
                    table,
                    HEADER_MTI_SOFT_DELETE.format(**mti),
                    sql.CREATE_MTI_SOFT_DELETE_RULE.format(**mti),
                    sql.DROP_MTI_SOFT_DELETE_RULE.format(child_table=table),
                )

            # --- cascade rules for CASCADE FKs pointing at this model (deferred so they
            #     always follow the owner's own soft-delete rule) ---
            if has_column(model, '_deleted_at'):
                deferred.extend(self._cascade_operations(model))

        # Tenant policies last: they are independent of the triggers and rules above (a
        # policy references neither), so they sort to the end where they read as a group.
        return operations + deferred + self._tenant_operations(app, force_rls=False)

    @staticmethod
    def _is_cascade_candidate(related_model, fk_field, on_delete) -> bool:
        """Whether this reverse relation is one a cascade soft-delete rule is written for.

        Shared by :meth:`_cascade_operations`, which writes the rules, and
        :meth:`_scoped_cascade_gap_notes`, which reports the ones a scoped run left out.
        Shared rather than restated, for the same reason ``tenancy.discovery`` is: the two
        had drifted, and a gap note for a relation the generator would never write a rule
        for promises the reader that naming the parent's app closes it, which is false.
        Only the remaining ``owns_column`` test is left to the callers, because the writer
        warns about it and the reporter must simply stay quiet.
        """
        return (
            on_delete == models.CASCADE
            and has_column(related_model, '_deleted_at')
            # The MTI parent-link (a CASCADE OneToOne) is structural, not a user cascade FK.
            and not getattr(fk_field.remote_field, 'parent_link', False)
            # An FK reached through MTI is not a second FK: it is the *same physical column*
            # on the ancestor's table, which appears in the caller's loop in its own right.
            and fk_field.model is related_model
        )

    def _cascade_candidates(
        self, model: type[models.Model], owner_table: str
    ) -> list[tuple[type[models.Model], models.ForeignKey, bool]]:
        """CASCADE FKs pointing at *model*, each flagged whether it is the *primary* one
        for its related_table.

        Shared by :meth:`_cascade_operations` (which writes the rules) and
        :meth:`_scoped_cascade_gap_notes` (which reports the ones an out-of-scope run
        leaves out), for the same reason :meth:`_is_cascade_candidate` is shared: the two
        must agree not just on *which* FKs count but on *which dedupe key* each one uses,
        or a report and a write disagree about what is already covered.

        Two or more CASCADE FKs from the same related_table to the same owner table need
        distinct dedupe keys -- and, when the rules are actually written, distinct rule
        names (see ``sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA``'s comment for why a
        shared name silently orphans one FK's cascade). The first one, in the
        deterministic ``(related_table, column)`` sort order below, is primary and keeps
        the plain, historical form -- so every already-migrated project's lone cascade
        rule for a pair is untouched and never re-emitted. Only a genuine second-or-later
        FK on the same pair is not primary.
        """
        seen_related_tables: set[str] = set()
        candidates: list[tuple[type[models.Model], models.ForeignKey, bool]] = []
        for related_model, fk_field, on_delete in sorted(
            self.reverse_relations_mapping[model],
            key=lambda t: (t[0]._meta.db_table, t[1].column),
        ):
            # Structural parent-links and MTI-inherited FKs are both excluded there: the MTI
            # redirect rule already ties a child's deletion to the owner, and an inherited FK
            # is the ancestor's own column, whose rule this same loop emits -- and because
            # every table in an MTI chain shares one ``_deleted_at``, that rule already
            # archives the children.
            if not self._is_cascade_candidate(related_model, fk_field, on_delete):
                continue
            related_table = related_model._meta.db_table
            # The flat cascade rule does ``UPDATE related_table SET _deleted_at`` -- only valid
            # when the related child owns ``_deleted_at`` on the very table its FK lives on.
            # An FK declared on an MTI child's *own* table while its ``_deleted_at`` lives on a
            # farther ancestor needs a join form we don't emit yet; surface it instead of
            # writing a rule that references a missing column.
            if not owns_column(related_model, '_deleted_at'):
                self._mti_cascade_warnings.append(
                    f"Cascade rule for '{related_table}' -> '{owner_table}' skipped: "
                    f"'{related_model.__name__}' declares this foreign key on its own table "
                    'but inherits _deleted_at from a multi-table-inheritance ancestor, which '
                    'needs a join form the generator does not emit yet.'
                )
                continue
            is_primary = related_table not in seen_related_tables
            seen_related_tables.add(related_table)
            candidates.append((related_model, fk_field, is_primary))
        return candidates

    def _cascade_operations(self, model: type[models.Model]) -> list[str]:
        """Cascade soft-delete rules for ``on_delete=CASCADE`` FKs pointing at *model*.

        The rule is an ``ON UPDATE`` rule that must live on the table whose ``_deleted_at``
        column actually flips: *model*'s own table for the single-table case, or the owning
        ancestor when *model* is an MTI child (an ``ON UPDATE TO child_table`` rule would never
        fire, since the child table's ``_deleted_at`` is never written). The related child's FK
        column holds the shared MTI pk value, so matching it against the owner pk still works.
        """
        owner = column_owner(model, '_deleted_at')
        owner_table = owner._meta.db_table
        owner_pk = owner._meta.pk.column

        ops: list[str] = []
        for related_model, fk_field, is_primary in self._cascade_candidates(model, owner_table):
            related_table = related_model._meta.db_table
            if is_primary:
                key = (related_table, owner_table, None)
                header = HEADER_SOFT_DELETE_RELATED.format(
                    related_table=related_table, table=owner_table
                )
                forward = sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
                    table=owner_table,
                    related_table=related_table,
                    primary_key=owner_pk,
                    foreign_key=fk_field.column,
                )
                reverse = sql.DROP_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
                    table=owner_table, related_table=related_table
                )
            else:
                key = (related_table, owner_table, fk_field.column)
                header = HEADER_SOFT_DELETE_RELATED_VIA.format(
                    related_table=related_table, table=owner_table, foreign_key=fk_field.column
                )
                forward = sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA.format(
                    table=owner_table,
                    related_table=related_table,
                    primary_key=owner_pk,
                    foreign_key=fk_field.column,
                )
                reverse = sql.DROP_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA.format(
                    table=owner_table, related_table=related_table, foreign_key=fk_field.column
                )
            self._append_if_stale(
                ops, self.existing.soft_delete_related, key, header, forward, reverse
            )
        return ops

    def _scoped_cascade_gap_notes(self, requested: set[str]) -> list[str]:
        """Describe cross-app CASCADE soft-delete rules this scoped run will not create.

        A rule like "deleting Band cascades to Album" is generated while
        ``_build_operations`` processes Band's app (the *parent* holding
        ``_deleted_at``), not Album's — so if the parent's app is scoped out,
        the rule is skipped even when the child's app is in scope. This is the
        intended "pragmatic scope" tradeoff (mirrors Django, which also only
        touches the apps you name), not a bug; it's closed by a later run that
        includes the parent's app label (or no labels at all).

        Only reported when the *child* (related) model's app is itself part of
        this scoped run — otherwise the gap is about two apps neither of which
        the caller asked to generate migrations for, which is just noise.
        """
        if not requested:
            return []

        model_app_label = {
            model: app.label
            for app in django_apps.get_app_configs()
            if _generator.is_local(app)
            for model in app.get_models()
        }

        notes: list[str] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app) or app.label in requested:
                continue
            for model in app.get_models():
                if not has_column(model, '_deleted_at'):
                    continue
                # The rule lives on the table that owns _deleted_at (the model itself, or its
                # MTI ancestor), matching where `_cascade_operations` places it.
                table = column_owner(model, '_deleted_at')._meta.db_table
                # Shared with _cascade_operations, which is what makes "closed by a later run
                # naming the parent's app" a promise this check can actually verify: the two
                # must agree on both which FKs count and which dedupe key each one uses.
                for related_model, fk_field, is_primary in self._cascade_candidates(model, table):
                    if model_app_label.get(related_model) not in requested:
                        continue
                    related_table = related_model._meta.db_table
                    key = (related_table, table, None if is_primary else fk_field.column)
                    if key in self.existing.soft_delete_related:
                        continue
                    notes.append(
                        f"Cascade rule on '{related_table}' related to '{table}' skipped: "
                        f"parent app '{app.label}' is not in this scoped run."
                    )
        return notes

    def _function_dependencies_for(self, operations_blob: str) -> list[tuple[str, str]]:
        """Function-migration dependencies an app's operations actually require.

        Only ``updated_at`` triggers call a shared trigger function: own-table triggers use
        ``set_updated_at`` (the base function migration), MTI parent-propagation triggers use
        ``set_parent_updated_at`` (the parent function migration). Soft-delete and cascade rules
        call no function, so an app emitting only those needs neither dependency. Keying off the
        operation headers (rather than appending both deps unconditionally) keeps an app's
        migration from being coupled to a function migration -- and its host app's ordering --
        it never uses.
        """
        deps: list[tuple[str, str]] = []
        if self.trigger_function_dependency and _RE_UPDATED_AT.search(operations_blob):
            deps.append(self.trigger_function_dependency)
        if self.parent_trigger_function_dependency and _RE_MTI_UPDATED_AT.search(operations_blob):
            deps.append(self.parent_trigger_function_dependency)
        return deps

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def handle(self, *app_labels, **options):
        check_only: bool = options['check_only']
        force_rls: bool = options.get('force_rls', False)
        self._adopt = options.get('adopt', False)
        # Positional app labels scope generation; empty => all local apps.
        requested: set[str] = set(app_labels)

        _generator.validate_app_labels(requested)

        if force_rls and self._adopt:
            raise CommandError(
                '--adopt and --force-rls cannot be combined. --force-rls is the second stage '
                'of a retrofit and acts only on tables whose policies this command already '
                'recorded; --adopt exists precisely because that record is missing. Run '
                '--adopt first, then --force-rls.'
            )

        if force_rls:
            return self._handle_force_rls_stage(requested, check_only=check_only)

        # Step 1: Ensure the singleton function migration(s) exist, so all subsequent app
        # migrations can safely depend on them. Scoped to the requested apps, but still hosted
        # in TRIGGER_FUNCTION_APP (a hard prerequisite) even if that host app wasn't named.
        # The base ``set_updated_at`` function is needed by own-table triggers; the MTI
        # ``set_parent_updated_at`` function only by MTI children that inherit ``_updated_at``.
        in_scope_models = [
            model
            for app in django_apps.get_app_configs()
            if _generator.is_in_scope(app, requested)
            for model in app.get_models()
        ]
        needs_trigger_function = any(owns_column(m, '_updated_at') for m in in_scope_models)
        needs_parent_function = any(is_mti_child(m, '_updated_at') for m in in_scope_models)

        changes_made = needs_trigger_function and self._ensure_trigger_function_migration(
            check_only=check_only
        )
        if needs_parent_function:
            changes_made = (
                self._ensure_parent_trigger_function_migration(check_only=check_only)
                or changes_made
            )
        check_missing: list[tuple[str, list[str]]] = []

        # Step 2: per-app trigger / soft-delete migrations, scoped to `requested`.
        # Intentionally skips cross-app CASCADE rules whose parent app isn't in
        # scope (see `_scoped_cascade_gap_notes`) -- surfaced below, not silent.
        for app in django_apps.get_app_configs():
            if not _generator.is_in_scope(app, requested):
                continue

            operations = self._build_operations(app)
            if not operations:
                continue

            operations_blob = '\n'.join(operations)
            operations_digest = _generator.digest_of(operations)
            if _generator.migration_with_digest_exists(app, operations_digest):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = _generator.create_empty_migration_file(app, 'auto_enforcement')
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
                dependencies=self._function_dependencies_for(operations_blob),
            )

            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Enforcement migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

        # Step 3: surface cross-app cascade rules this scoped run intentionally
        # did not create, so the "pragmatic scope" tradeoff is never silent.
        for note in self._scoped_cascade_gap_notes(requested):
            self.stdout.write(self.style.WARNING(note))

        # Surface MTI cascade rules skipped because cascading INTO an MTI child is unsupported.
        for note in self._mti_cascade_warnings:
            self.stderr.write(self.style.WARNING(note))

        # Tables tenancy could not cover, and why. Skips are design, never silent.
        for note in self._tenancy_notes:
            self.stdout.write(self.style.WARNING(note))

        if check_missing:
            self._report_missing(check_missing)

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')

    def _report_missing(self, check_missing: list[tuple[str, list[str]]]) -> None:
        """Print what ``--check`` found and exit non-zero.

        "or outdated" is not padding: an operation here may be a *replacement* for a policy
        whose shape no longer matches the models, which is a migration the app needs despite
        already having one for that table.
        """
        for app_label, operations in check_missing:
            self.stderr.write(
                self.style.ERROR(f"Missing or outdated enforcement migrations for '{app_label}':")
            )
            for operation in operations:
                self.stderr.write(textwrap.indent(operation, '    '))
        raise CommandError('Run `manage.py makeguitarmigrations` to create missing migrations.')

    def _handle_force_rls_stage(self, requested: set[str], *, check_only: bool) -> None:
        """Emit only ``FORCE ROW LEVEL SECURITY`` migrations, for tables already policied.

        A separate stage rather than part of the main run, because it exists for exactly one
        situation: a retrofit onto a populated database that set ``GUITARS_RLS_FORCE = False``
        so policies could be shipped inert, soaked, and only then made binding. Mixing it
        into the normal run would defeat the staging it exists to provide.
        """
        if self._rls_force_enabled():
            # Not an error, and not redundant either: this is the shape of a *finished*
            # retrofit. Policies shipped inert under GUITARS_RLS_FORCE = False, the setting
            # was then flipped to True, and those already-policied tables still need their
            # FORCE. New policies get it inline, so the run below finds only the backlog --
            # commonly nothing, which is why it says so rather than failing.
            self.stdout.write(
                self.style.WARNING(
                    'GUITARS_RLS_FORCE is True, so new tenant policies already emit FORCE. '
                    '--force-rls only covers tables whose policies shipped before it was '
                    'turned on; expect no changes if there are none.'
                )
            )
        if not self._tenant_policies_enabled():
            self.stdout.write(
                self.style.WARNING(
                    'GUITARS_TENANT_POLICIES is False, so no tenant policies exist to force.'
                )
            )
            return

        changes_made = False
        check_missing: list[tuple[str, list[str]]] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_in_scope(app, requested):
                continue

            operations = self._tenant_operations(app, force_rls=True)
            if not operations:
                continue

            operations_digest = _generator.digest_of(operations)
            if _generator.migration_with_digest_exists(app, operations_digest):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = _generator.create_empty_migration_file(app, 'auto_tenant_force')
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
            )
            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Enforcement migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

        for note in self._tenancy_notes:
            self.stdout.write(self.style.WARNING(note))

        if check_missing:
            self._report_missing(check_missing)

        if not changes_made and not check_only:
            self.stdout.write('No changes detected')
