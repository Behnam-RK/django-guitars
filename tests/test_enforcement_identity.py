"""The operation identity layer: inlined SQL, ``[SQL:...]`` headers, and ``--adopt``.

Three things are tested here that the rest of the suite cannot see, because each of them is
about a *silent* failure -- a generator that emits nothing while reporting success:

* A change to a SQL constant must produce a migration. Before the header carried a content
  digest it could not: the per-table ``_RE_*`` scan reported the table covered, so no
  operation was built and the file-level ``[DIGEST:...]`` was never reached. That is how the
  1.0.0 soft-delete guard rewrite reached every existing database as a no-op.
* The comment headers the scan keys on must agree with the templates that emit them. The
  two are hand-written copies in one module and nothing derived one from the other;
  ``CLAUDE.md`` and ``docs/migrations.md`` both claimed ``tests/test_sql_interface.py``
  guarded this, and it never did.
* ``IF EXISTS`` and ``OR REPLACE`` must appear only where the generator genuinely does not
  know what the database holds, which is under ``--adopt`` and nowhere else. Used on a path
  where the answer is known they turn "your database has diverged from its migration
  history" into silence.
"""

from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.core.management import CommandError, call_command

from guitars import sql
from guitars.management.commands import makeguitarmigrations as gen
from guitars.management.commands.makeguitarmigrations import Command
from guitars.tenancy.discovery import app_coverage

from .test_command import _command_with_scaffold


# ---------------------------------------------------------------------------
# Headers and the scanners that read them
# ---------------------------------------------------------------------------

#: ``(header template, scanner, placeholder values)`` for every kind of enforcement
#: operation. The emitter and the scanner are independent hand-written strings in one
#: module, so nothing but this table stops them drifting apart -- and a drift is invisible
#: until a consuming project's next run silently duplicates every operation it already has.
HEADER_SCANNERS = [
    (gen.HEADER_TRIGGER_FUNCTION, gen._RE_TRIGGER_FUNCTION, {}),
    (gen.HEADER_PARENT_TRIGGER_FUNCTION, gen._RE_PARENT_TRIGGER_FUNCTION, {}),
    (gen.HEADER_UPDATED_AT, gen._RE_UPDATED_AT, {'table': 'shop_order'}),
    (gen.HEADER_SOFT_DELETE, gen._RE_SOFT_DELETE, {'table': 'shop_order'}),
    (
        gen.HEADER_SOFT_DELETE_RELATED,
        gen._RE_SOFT_DELETE_RELATED,
        {'related_table': 'shop_line', 'table': 'shop_order'},
    ),
    (
        gen.HEADER_SOFT_DELETE_RELATED_VIA,
        gen._RE_SOFT_DELETE_RELATED,
        {'related_table': 'shop_line', 'table': 'shop_order', 'foreign_key': 'bonus_order_id'},
    ),
    (
        gen.HEADER_MTI_UPDATED_AT,
        gen._RE_MTI_UPDATED_AT,
        {'child_table': 'shop_giftorder', 'parent_table': 'shop_order'},
    ),
    (
        gen.HEADER_MTI_SOFT_DELETE,
        gen._RE_MTI_SOFT_DELETE,
        {'child_table': 'shop_giftorder', 'parent_table': 'shop_order'},
    ),
    (
        gen.HEADER_TENANT_POLICY,
        gen._RE_TENANT_POLICY,
        {'table': 'shop_order', 'identity': '57ff74989db7'},
    ),
    (
        gen.HEADER_TENANT_POLICY_REPLACED,
        gen._RE_TENANT_POLICY,
        {'table': 'shop_order', 'identity': '0b7c94cc2edc'},
    ),
    (gen.HEADER_TENANT_FORCE, gen._RE_TENANT_FORCE, {'table': 'shop_order'}),
]


@pytest.mark.parametrize(
    ('template', 'scanner', 'values'),
    HEADER_SCANNERS,
    ids=lambda v: v if isinstance(v, str) else '',
)
def test_every_emitted_header_is_matched_by_its_scanner(template, scanner, values):
    """The round trip the docs have always promised and no test performed.

    Reword a header without rewording its regex and every already-committed migration stops
    being recognised: the next run emits a duplicate of every operation the project already
    has. The failure is at ``migrate`` time in someone else's repository, which is why it
    has to be caught here.
    """
    source, digest = gen._operation(template.format(**values), 'SELECT 1', 'SELECT 2')
    header = source.splitlines()[0]

    match = scanner.search(header)
    assert match, f'{scanner.pattern!r} does not match the header it is meant to read: {header!r}'
    assert gen._recorded_sql_identity(header, match) == digest


def test_the_header_table_lists_every_header_the_module_defines():
    """Exhaustiveness, so adding a kind without a scanner fails here rather than in the wild."""
    defined = {name for name in dir(gen) if name.startswith('HEADER_')}
    covered = {
        name
        for name in defined
        if any(getattr(gen, name) is template for template, _, _ in HEADER_SCANNERS)
    }
    assert defined == covered, f'headers with no scanner in HEADER_SCANNERS: {defined - covered}'


def test_the_force_header_cannot_be_read_as_a_policy_header():
    """A documented invariant of the header vocabulary, not an accident of the strings.

    The FORCE header carries an extra token before "RLS". If it ever matched the policy
    scanner, a table would read as policied on the strength of a migration that only forces
    row-level security -- an ``ALTER TABLE`` with no predicate behind it.
    """
    force = gen.HEADER_TENANT_FORCE.format(table='shop_order')
    assert gen._RE_TENANT_POLICY.search(force) is None
    assert gen._RE_TENANT_FORCE.search(force) is not None


def test_the_two_function_headers_cannot_be_read_as_each_other():
    """Otherwise the MTI parent function migration would satisfy the base one's dependency."""
    base = gen.HEADER_TRIGGER_FUNCTION
    parent = gen.HEADER_PARENT_TRIGGER_FUNCTION
    assert gen._RE_TRIGGER_FUNCTION.search(parent) is None
    assert gen._RE_PARENT_TRIGGER_FUNCTION.search(base) is None


def test_a_header_without_the_sql_token_reads_as_stale_not_covered():
    """The upgrade path, in one assertion.

    Every migration written before the SQL was inlined carries a recognised header and no
    ``[SQL:...]``. Reading that as covered is exactly the bug: it is how the 1.0.0
    soft-delete guard rewrite -- which turned a rolled-back ``hard_delete()`` into permanent
    deletes -- reached every existing database as nothing at all.
    """
    legacy = '# Soft Delete Rule on "shop_order" table!'
    match = gen._RE_SOFT_DELETE.search(legacy)

    assert match is not None
    assert gen._recorded_sql_identity(legacy, match) is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_generated_operations_carry_sql_rather_than_naming_it():
    """The whole reason a SQL change used to ship nothing.

    ``from guitars import sql`` in a migration makes the file's meaning a function of the
    installed version: a fresh ``migrate`` and an incrementally-migrated database end up
    differing while sharing an identical history. It also put the SQL out of reach of every
    digest, since the source named the constant instead of containing it.
    """
    source, _ = gen._operation(
        '# Soft Delete Rule on "shop_order" table!',
        sql.CREATE_SOFT_DELETE_RULE.format(table='shop_order', primary_key='id'),
        sql.DROP_SOFT_DELETE_RULE.format(table='shop_order'),
    )

    assert 'sql.' not in source
    assert 'CREATE OR REPLACE RULE soft_delete' in source
    # The guard whose rewrite started all of this. `= 'off'` fails toward destroying data,
    # because a rolled-back `set_config` reads back as the empty string.
    assert "<> 'on'" in source


def test_a_changed_sql_constant_changes_the_operation_digest():
    """The property that makes regeneration generated rather than hand-written."""
    _, before = gen._operation('# h!', 'CREATE RULE r AS ...', 'DROP RULE r')
    _, after = gen._operation('# h!', 'CREATE RULE r AS ... changed', 'DROP RULE r')

    assert before != after


def test_the_digest_ignores_which_form_is_emitted():
    """A create and its replacement describe the same definition, so they digest the same.

    Keying the digest to the emitted form instead makes successive runs disagree forever: a
    fresh database records the create form's digest, the next run builds the replace form to
    compare against it, the two differ, and a replacement migration is written every run.
    """
    _, created = gen._operation('# h!', 'CREATE TRIGGER t ...', 'DROP TRIGGER t')
    replaced_source, replaced = gen._operation(
        '# h!', 'CREATE TRIGGER t ...', 'DROP TRIGGER t', emit='DROP TRIGGER t; CREATE TRIGGER t'
    )

    assert created == replaced
    assert 'DROP TRIGGER t; CREATE TRIGGER t' in replaced_source


def test_sql_that_cannot_be_triple_quoted_falls_back_to_repr():
    """Readability is the default, validity is not negotiable.

    A trailing double quote would run into the closing delimiter and an embedded triple
    quote would close it early -- either way the generated migration is a syntax error that
    only shows up in someone else's repository.
    """
    assert gen._sql_string_literal('SELECT 1') == '"""SELECT 1"""'
    for hostile in ('SELECT """x"""', 'SELECT "col"', "SELECT E'\\n'"):
        rendered = gen._sql_string_literal(hostile)
        assert eval(rendered) == hostile  # noqa: S307 -- the point is that it parses back


# ---------------------------------------------------------------------------
# Which form gets emitted
# ---------------------------------------------------------------------------


def _emit(command, recorded, key='shop_order'):
    operations: list[str] = []
    command._append_if_stale(
        operations,
        recorded,
        key,
        '# Updated at Trigger on "shop_order" table!',
        'CREATE TRIGGER updated_at_trigger ...',
        'DROP TRIGGER updated_at_trigger ON shop_order',
        replace='DROP TRIGGER updated_at_trigger ON shop_order; CREATE TRIGGER ...',
        adopt='DROP TRIGGER IF EXISTS updated_at_trigger ON shop_order; CREATE TRIGGER ...',
    )
    return operations


def test_nothing_recorded_emits_the_plain_create_form():
    """A first definition must collide loudly.

    ``set_updated_at()`` is an unqualified name in the public schema. If the create form
    carried ``OR REPLACE``, guitars would silently clobber another extension's function of
    the same name and the breakage would surface at runtime, somewhere else, much later.
    """
    (operation,) = _emit(Command(), {})

    assert 'IF EXISTS' not in operation
    assert operation.count('CREATE TRIGGER') == 1
    assert 'DROP TRIGGER updated_at_trigger ON shop_order; CREATE' not in operation


def test_a_current_recorded_digest_emits_nothing():
    command = Command()
    _, digest = gen._operation(
        '# Updated at Trigger on "shop_order" table!',
        'CREATE TRIGGER updated_at_trigger ...',
        'DROP TRIGGER updated_at_trigger ON shop_order',
    )

    assert _emit(command, {'shop_order': digest}) == []


@pytest.mark.parametrize('recorded', [None, 'stale0000'], ids=['legacy-header', 'changed-sql'])
def test_a_stale_record_emits_the_replace_form_without_if_exists(recorded):
    """The object is known to be ours, so the drop is unguarded.

    ``IF EXISTS`` here would swallow the one thing worth hearing about: a database that no
    longer matches the migration history that supposedly built it.
    """
    (operation,) = _emit(Command(), {'shop_order': recorded})

    assert 'DROP TRIGGER updated_at_trigger ON shop_order; CREATE' in operation
    assert 'IF EXISTS' not in operation


def test_adopt_emits_the_guarded_form_even_with_nothing_recorded():
    """The one path where the uncertainty is real -- and was opted into by flag."""
    command = Command()
    command._adopt = True

    (operation,) = _emit(command, {})

    assert 'DROP TRIGGER IF EXISTS updated_at_trigger ON shop_order' in operation


def test_adopt_re_emits_even_a_record_that_is_already_current():
    """Otherwise the flag does nothing on the tables an adopter most needs it for."""
    command = Command()
    command._adopt = True
    _, digest = gen._operation(
        '# Updated at Trigger on "shop_order" table!',
        'CREATE TRIGGER updated_at_trigger ...',
        'DROP TRIGGER updated_at_trigger ON shop_order',
    )

    assert len(_emit(command, {'shop_order': digest})) == 1


# ---------------------------------------------------------------------------
# Tenant policies
# ---------------------------------------------------------------------------


def test_adopt_emits_the_replacement_policy_form():
    """The gap this flag was added to close.

    ``create_tenant_policy`` is a bare ``CREATE POLICY`` -- PostgreSQL has no
    ``CREATE POLICY IF NOT EXISTS`` -- so a database whose policy exists but was never
    recorded here had no supported route in at all: the generator saw no ``[POLICY:...]``
    header, emitted the CREATE form, and ``migrate`` failed with "policy tenant_scope
    already exists".
    """
    command = Command()
    command.stdout = StringIO()
    command._adopt = True

    operations = command._tenant_operations(django_apps.get_app_config('testapp'), force_rls=False)

    assert operations
    for operation in operations:
        assert operation.startswith('# Tenant RLS replaced on ')
        assert 'DROP POLICY IF EXISTS tenant_scope' in operation


def test_a_changed_policy_sql_emits_a_replacement_even_when_the_identity_is_unchanged():
    """Two independent reasons to replace, and the identity cannot see the second.

    ``[POLICY:...]`` covers what the policy *says*, with ``force`` deliberately excluded so
    that flipping ``GUITARS_RLS_FORCE`` cannot defeat the staged ``--force-rls`` retrofit.
    That leaves a change to the policy SQL itself invisible to it.
    """
    app = django_apps.get_app_config('testapp')
    table = 'testapp_release'
    command = Command()
    command.stdout = StringIO()
    coverage = app_coverage(app).tables[table]

    # Record the policy as covered with the identity the models imply *now*, so the identity
    # check passes and only the SQL digest can be what triggers the replacement.
    command.existing.tenant_policy_identities[table] = command._policy_identity(table, coverage)
    command.existing.tenant_policy_sql[table] = 'stale0000'

    headers = [
        line
        for operation in command._tenant_operations(app, force_rls=False)
        for line in operation.splitlines()
        if line.startswith('#')
    ]

    assert any(h.startswith(f'# Tenant RLS replaced on "{table}" table!') for h in headers)


# ---------------------------------------------------------------------------
# The singleton trigger functions
# ---------------------------------------------------------------------------


def test_a_changed_function_body_emits_a_replacement_migration(monkeypatch, tmp_path):
    """These are singletons by *existence*, which is why an edited body shipped nothing.

    The first thing the ensure methods did was return early on "a migration mentioning this
    function exists somewhere". Comparing the recorded digest as well is what turns an edit
    into a migration.
    """
    command, _, filename = _command_with_scaffold(monkeypatch, tmp_path)
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    command.trigger_function_sql = 'stale0000'

    assert command._ensure_trigger_function_migration() is True

    content = (tmp_path / 'migrations' / filename).read_text()
    # OR REPLACE rather than DROP + CREATE, and that is forced rather than defensive:
    # DROP FUNCTION refuses while any trigger depends on it, and CASCADE would take every
    # table's trigger with it.
    assert 'CREATE OR REPLACE FUNCTION set_updated_at()' in content


def test_check_reports_a_changed_function_body_rather_than_passing(monkeypatch, tmp_path):
    """``--check`` is the CI gate, so silence here is the whole failure mode."""
    command, _, _ = _command_with_scaffold(monkeypatch, tmp_path)
    command.trigger_function_dependency = ('testapp', '0001_pretend')
    command.trigger_function_sql = 'stale0000'

    with pytest.raises(CommandError, match='has changed since the migration'):
        command._ensure_trigger_function_migration(check_only=True)


def test_adopt_emits_or_replace_for_a_function_with_nothing_recorded(monkeypatch, tmp_path):
    """The gap ``--adopt`` exists to close, for a function rather than a trigger or policy.

    A function with no recorded migration at all -- not even a stale one -- is exactly the
    database ``--adopt`` is for: created by hand, or by a generator whose headers this one
    cannot read. A plain ``CREATE FUNCTION`` there fails migrate with "function already
    exists". There is no separate adopt form for a function, because ``OR REPLACE`` is
    already correct whether or not it exists -- so ``--adopt`` must select it even though
    nothing is recorded, which is the one case the create/replace choice used to get wrong.
    """
    command, _, filename = _command_with_scaffold(monkeypatch, tmp_path)
    command.trigger_function_dependency = None
    command.trigger_function_sql = None
    command._adopt = True

    assert command._ensure_trigger_function_migration() is True

    content = (tmp_path / 'migrations' / filename).read_text()
    assert 'CREATE OR REPLACE FUNCTION set_updated_at()' in content


# ---------------------------------------------------------------------------
# Flag combinations
# ---------------------------------------------------------------------------


def test_adopt_and_force_rls_cannot_be_combined():
    """They contradict each other, and the failure would otherwise be a quiet no-op.

    ``--force-rls`` acts only on tables whose policies this command already recorded;
    ``--adopt`` exists precisely because that record is missing.
    """
    with pytest.raises(CommandError, match='cannot be combined'):
        call_command('makeguitarmigrations', '--adopt', '--force-rls', stdout=StringIO())


def test_the_generated_migrations_import_nothing_from_the_kit():
    """Every enforcement migration written since inlining must stand on its own.

    A migration that does ``from guitars import sql`` is a migration whose meaning changes
    when the package is upgraded -- and the ones already committed here, from before the
    change, are what the upgrade path has to keep recognising.
    """
    migrations = sorted((Path(__file__).parent / 'testapp' / 'migrations').glob('0*.py'))
    inlined = [path for path in migrations if gen._RE_SQL_IDENTITY.search(path.read_text())]

    assert inlined, 'no inlined enforcement migrations found -- has generation been run?'
    for path in inlined:
        assert 'from guitars import sql' not in path.read_text(), path.name


def test_the_scan_recovers_the_most_recent_digest_across_files():
    """Last write wins, and only filename order makes that the currently-applied answer."""
    content = (
        '# Soft Delete Rule on "shop_order" table! [SQL:aaaaaaaaaaaa]\n'
        'migrations.RunSQL(sql="""x"""),\n'
        '# Soft Delete Rule on "shop_order" table! [SQL:bbbbbbbbbbbb]\n'
        'migrations.RunSQL(sql="""y"""),\n'
    )
    recorded = {
        match.group(1): gen._recorded_sql_identity(content, match)
        for match in gen._RE_SOFT_DELETE.finditer(content)
    }

    assert recorded == {'shop_order': 'bbbbbbbbbbbb'}


def test_unforced_policy_tables_reads_the_inlined_form():
    """``force`` is no longer a keyword in the migration, so it is read off the SQL.

    The lookbehind is not optional: every policy operation's ``reverse_sql`` carries
    ``NO FORCE ROW LEVEL SECURITY`` in the same slice of text, and without it every table
    reads as forced and ``--force-rls`` has nothing left to do.
    """
    forced = (
        '# Tenant RLS on "a" table! [POLICY:aaaaaaaaaaaa] [SQL:1111]\n'
        'migrations.RunSQL(sql=["""ALTER TABLE a ENABLE ROW LEVEL SECURITY""",'
        '"""ALTER TABLE a FORCE ROW LEVEL SECURITY"""],'
        'reverse_sql=["""ALTER TABLE a NO FORCE ROW LEVEL SECURITY"""]),\n'
        '# Tenant RLS on "b" table! [POLICY:bbbbbbbbbbbb] [SQL:2222]\n'
        'migrations.RunSQL(sql=["""ALTER TABLE b ENABLE ROW LEVEL SECURITY"""],'
        'reverse_sql=["""ALTER TABLE b NO FORCE ROW LEVEL SECURITY"""]),\n'
    )

    assert gen.unforced_policy_tables(forced) == {'b'}


def test_unforced_policy_tables_still_reads_the_legacy_keyword_form():
    """Migrations written before inlining record the decision as a keyword argument.

    Reading only the SQL form would make every one of them look unforced and put
    already-forced tables back on the FORCE backlog.
    """
    legacy = (
        '# Tenant RLS on "a" table! [POLICY:aaaaaaaaaaaa]\n'
        'migrations.RunSQL(sql=sql.create_table_rls(table=\'a\', force=True)),\n'
        '# Tenant RLS on "b" table! [POLICY:bbbbbbbbbbbb]\n'
        'migrations.RunSQL(sql=sql.create_table_rls(table=\'b\', force=False)),\n'
    )

    assert gen.unforced_policy_tables(legacy) == {'b'}


def test_the_sql_identity_pattern_only_reads_its_own_header_line():
    """A digest belongs to the header it sits on, not to whatever follows in the file."""
    content = '# Soft Delete Rule on "shop_order" table!\n# something else [SQL:cccccccccccc]\n'
    match = gen._RE_SOFT_DELETE.search(content)

    assert gen._recorded_sql_identity(content, match) is None
