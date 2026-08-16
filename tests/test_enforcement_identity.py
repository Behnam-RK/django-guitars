"""The operation identity layer: inlined SQL, ``[SQL:...]`` headers, and ``--adopt`` --
silent-failure modes the rest of the suite can't see: a SQL edit shipping no migration, a
header/scanner drift, and ``IF EXISTS``/``OR REPLACE`` where knowledge isn't uncertain."""

from io import StringIO
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.core.management import CommandError, call_command

from guitars import sql
from guitars.management.enforcement import headers as headers_module
from guitars.management.enforcement import identity as identity_module
from guitars.management.enforcement.command import Command
from guitars.tenancy.discovery import app_coverage

from .test_command import _command_with_scaffold, _unforced_policy_tables


# ---------------------------------------------------------------------------
# Headers and the scanners that read them
# ---------------------------------------------------------------------------

#: ``(header template, scanner, placeholder values)`` for every kind of enforcement
#: operation -- proves the round trip for all nine, including the hand-written scanners
#: where nothing else stops emitter and scanner drifting apart.
HEADER_SCANNERS = [
    (headers_module.HEADER_TRIGGER_FUNCTION, headers_module._RE_TRIGGER_FUNCTION, {}),
    (headers_module.HEADER_PARENT_TRIGGER_FUNCTION, headers_module._RE_PARENT_TRIGGER_FUNCTION, {}),
    (headers_module.HEADER_UPDATED_AT, headers_module._RE_UPDATED_AT, {'table': 'shop_order'}),
    (headers_module.HEADER_SOFT_DELETE, headers_module._RE_SOFT_DELETE, {'table': 'shop_order'}),
    (
        headers_module.HEADER_SOFT_DELETE_RELATED,
        headers_module._RE_SOFT_DELETE_RELATED,
        {'related_table': 'shop_line', 'table': 'shop_order'},
    ),
    (
        headers_module.HEADER_SOFT_DELETE_RELATED_VIA,
        headers_module._RE_SOFT_DELETE_RELATED,
        {'related_table': 'shop_line', 'table': 'shop_order', 'foreign_key': 'bonus_order_id'},
    ),
    (
        headers_module.HEADER_MTI_UPDATED_AT,
        headers_module._RE_MTI_UPDATED_AT,
        {'child_table': 'shop_giftorder', 'parent_table': 'shop_order'},
    ),
    (
        headers_module.HEADER_MTI_SOFT_DELETE,
        headers_module._RE_MTI_SOFT_DELETE,
        {'child_table': 'shop_giftorder', 'parent_table': 'shop_order'},
    ),
    (
        headers_module.HEADER_TENANT_POLICY,
        headers_module._RE_TENANT_POLICY,
        {'table': 'shop_order', 'identity': '57ff74989db7'},
    ),
    (
        headers_module.HEADER_TENANT_POLICY_REPLACED,
        headers_module._RE_TENANT_POLICY,
        {'table': 'shop_order', 'identity': '0b7c94cc2edc'},
    ),
    (headers_module.HEADER_TENANT_FORCE, headers_module._RE_TENANT_FORCE, {'table': 'shop_order'}),
    (
        headers_module.HEADER_TENANT_AUTOFILL_FUNCTION,
        headers_module._RE_TENANT_AUTOFILL_FUNCTION,
        {'function': 'guitars_fill_4_shop_shop_id'},
    ),
    (
        headers_module.HEADER_TENANT_AUTOFILL,
        headers_module._RE_TENANT_AUTOFILL,
        {'table': 'shop_order', 'function': 'guitars_fill_4_shop_shop_id'},
    ),
]


@pytest.mark.parametrize(
    ('template', 'scanner', 'values'),
    HEADER_SCANNERS,
    ids=lambda v: v if isinstance(v, str) else '',
)
def test_every_emitted_header_is_matched_by_its_scanner(template, scanner, values):
    """Reword a header without rewording its regex and every already-committed migration
    stops being recognised, duplicating on the next run -- caught here, not at someone
    else's ``migrate``."""
    source, digest = identity_module._operation(template.format(**values), 'SELECT 1', 'SELECT 2')
    header = source.splitlines()[0]

    match = scanner.search(header)
    assert match, f'{scanner.pattern!r} does not match the header it is meant to read: {header!r}'
    assert identity_module._recorded_sql_identity(header, match) == digest


def test_the_header_table_lists_every_header_the_module_defines():
    """Exhaustiveness, so adding a kind without a scanner fails here rather than in the wild."""
    defined = {name for name in dir(headers_module) if name.startswith('HEADER_')}
    covered = {
        name
        for name in defined
        if any(getattr(headers_module, name) is template for template, _, _ in HEADER_SCANNERS)
    }
    assert defined == covered, f'headers with no scanner in HEADER_SCANNERS: {defined - covered}'


def test_the_force_header_cannot_be_read_as_a_policy_header():
    """If the FORCE header ever matched the policy scanner, a table would read as
    policied on the strength of an ``ALTER TABLE`` with no predicate behind it."""
    force = headers_module.HEADER_TENANT_FORCE.format(table='shop_order')
    assert headers_module._RE_TENANT_POLICY.search(force) is None
    assert headers_module._RE_TENANT_FORCE.search(force) is not None


def test_the_two_function_headers_cannot_be_read_as_each_other():
    """Otherwise the MTI parent function migration would satisfy the base one's dependency."""
    base = headers_module.HEADER_TRIGGER_FUNCTION
    parent = headers_module.HEADER_PARENT_TRIGGER_FUNCTION
    assert headers_module._RE_TRIGGER_FUNCTION.search(parent) is None
    assert headers_module._RE_PARENT_TRIGGER_FUNCTION.search(base) is None


def test_the_autofill_headers_cannot_be_read_as_any_other_trigger():
    """``# Tenant autofill Trigger on ...`` sits close enough to the updated-at and MTI
    trigger headers that a careless scanner would fuse them, and an app would then declare a
    dependency on the ``set_updated_at`` migration it never calls -- or skip one it does."""
    autofill = headers_module.HEADER_TENANT_AUTOFILL.format(
        table='shop_order', function='guitars_fill_4_shop_shop_id'
    )
    function = headers_module.HEADER_TENANT_AUTOFILL_FUNCTION.format(
        function='guitars_fill_4_shop_shop_id'
    )

    for scanner in (
        headers_module._RE_UPDATED_AT,
        headers_module._RE_MTI_UPDATED_AT,
        headers_module._RE_SOFT_DELETE,
        headers_module._RE_TENANT_POLICY,
        headers_module._RE_TENANT_FORCE,
        headers_module._RE_TENANT_AUTOFILL_FUNCTION,
    ):
        assert scanner.search(autofill) is None
    assert headers_module._RE_TENANT_AUTOFILL.search(function) is None
    assert headers_module._RE_TENANT_AUTOFILL.search(autofill) is not None


def test_the_autofill_trigger_header_reads_the_table_but_not_the_function():
    """The table is the dedupe key; the function is carried for dependency keying only. A
    renamed function must leave the record *stale* (re-emitted as DROP+CREATE by the
    ``[SQL:...]`` digest), never *absent* -- absent would duplicate the CREATE."""
    before = headers_module.HEADER_TENANT_AUTOFILL.format(
        table='shop_order', function='guitars_fill_4_shop_shop_id'
    )
    after = headers_module.HEADER_TENANT_AUTOFILL.format(
        table='shop_order', function='guitars_fill_5_store_store_id'
    )

    assert headers_module._RE_TENANT_AUTOFILL.search(before).groups() == ('shop_order',)
    assert headers_module._RE_TENANT_AUTOFILL.search(after).groups() == ('shop_order',)
    assert headers_module._RE_TENANT_AUTOFILL_FUNCTION_REF.search(after).group(1) == (
        'guitars_fill_5_store_store_id'
    )


def test_a_header_without_the_sql_token_reads_as_stale_not_covered():
    """The upgrade path, in one assertion: reading a recognised-but-tokenless header as
    covered is exactly the bug the 1.0.0 soft-delete guard rewrite shipped as a no-op."""
    legacy = '# Soft Delete Rule on "shop_order" table!'
    match = headers_module._RE_SOFT_DELETE.search(legacy)

    assert match is not None
    assert identity_module._recorded_sql_identity(legacy, match) is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_generated_operations_carry_sql_rather_than_naming_it():
    """``from guitars import sql`` makes a migration's meaning a function of the installed
    version, and put the SQL out of reach of every digest -- see ADR-0006."""
    source, _ = identity_module._operation(
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
    _, before = identity_module._operation('# h!', 'CREATE RULE r AS ...', 'DROP RULE r')
    _, after = identity_module._operation('# h!', 'CREATE RULE r AS ... changed', 'DROP RULE r')

    assert before != after


def test_the_digest_ignores_which_form_is_emitted():
    """A create and its replacement describe the same definition, so they digest the
    same -- keying to the emitted form instead would make every run disagree forever."""
    _, created = identity_module._operation('# h!', 'CREATE TRIGGER t ...', 'DROP TRIGGER t')
    replaced_source, replaced = identity_module._operation(
        '# h!', 'CREATE TRIGGER t ...', 'DROP TRIGGER t', emit='DROP TRIGGER t; CREATE TRIGGER t'
    )

    assert created == replaced
    assert 'DROP TRIGGER t; CREATE TRIGGER t' in replaced_source


def test_sql_that_cannot_be_triple_quoted_falls_back_to_repr():
    """Readability is the default, validity is not negotiable -- a trailing double quote
    or embedded triple quote would otherwise close the delimiter early."""
    assert identity_module._sql_string_literal('SELECT 1') == '"""SELECT 1"""'
    for hostile in ('SELECT """x"""', 'SELECT "col"', "SELECT E'\\n'"):
        rendered = identity_module._sql_string_literal(hostile)
        assert eval(rendered) == hostile  # noqa: S307 -- the point is that it parses back


# ---------------------------------------------------------------------------
# Which form gets emitted
# ---------------------------------------------------------------------------


def _emit(command, recorded, key='shop_order', *, is_adopt=False):
    operations: list[str] = []
    command._append_if_stale(
        operations,
        recorded,
        key,
        '# Updated at Trigger on "shop_order" table!',
        'CREATE TRIGGER updated_at_trigger ...',
        'DROP TRIGGER updated_at_trigger ON shop_order',
        is_adopt=is_adopt,
        replace='DROP TRIGGER updated_at_trigger ON shop_order; CREATE TRIGGER ...',
        adopt='DROP TRIGGER IF EXISTS updated_at_trigger ON shop_order; CREATE TRIGGER ...',
    )
    return operations


def test_nothing_recorded_emits_the_plain_create_form():
    """A first definition must collide loudly -- if the create form carried OR REPLACE,
    guitars would silently clobber another extension's function of the same name."""
    (operation,) = _emit(Command(), {})

    assert 'IF EXISTS' not in operation
    assert operation.count('CREATE TRIGGER') == 1
    assert 'DROP TRIGGER updated_at_trigger ON shop_order; CREATE' not in operation


def test_a_current_recorded_digest_emits_nothing():
    command = Command()
    _, digest = identity_module._operation(
        '# Updated at Trigger on "shop_order" table!',
        'CREATE TRIGGER updated_at_trigger ...',
        'DROP TRIGGER updated_at_trigger ON shop_order',
    )

    assert _emit(command, {'shop_order': digest}) == []


@pytest.mark.parametrize('recorded', [None, 'stale0000'], ids=['legacy-header', 'changed-sql'])
def test_a_stale_record_emits_the_replace_form_without_if_exists(recorded):
    """The object is known to be ours, so the drop is unguarded -- IF EXISTS here would
    swallow the one thing worth hearing about: a diverged database."""
    (operation,) = _emit(Command(), {'shop_order': recorded})

    assert 'DROP TRIGGER updated_at_trigger ON shop_order; CREATE' in operation
    assert 'IF EXISTS' not in operation


def test_adopt_emits_the_guarded_form_even_with_nothing_recorded():
    """The one path where the uncertainty is real -- and was opted into by flag."""
    command = Command()

    (operation,) = _emit(command, {}, is_adopt=True)

    assert 'DROP TRIGGER IF EXISTS updated_at_trigger ON shop_order' in operation


def test_adopt_re_emits_even_a_record_that_is_already_current():
    """Otherwise the flag does nothing on the tables an adopter most needs it for."""
    command = Command()
    _, digest = identity_module._operation(
        '# Updated at Trigger on "shop_order" table!',
        'CREATE TRIGGER updated_at_trigger ...',
        'DROP TRIGGER updated_at_trigger ON shop_order',
    )

    assert len(_emit(command, {'shop_order': digest}, is_adopt=True)) == 1


# ---------------------------------------------------------------------------
# Tenant policies
# ---------------------------------------------------------------------------


def test_adopt_emits_the_replacement_policy_form():
    """A database whose policy exists but was never recorded had no route in: Postgres
    has no CREATE POLICY IF NOT EXISTS, so migrate failed with "already exists"."""
    command = Command()
    command.stdout = StringIO()

    operations = command._tenant_policy_operations(
        django_apps.get_app_config('testapp'), adopt=True
    )

    assert operations
    for operation in operations:
        assert operation.startswith('# Tenant RLS replaced on ')
        assert 'DROP POLICY IF EXISTS tenant_scope' in operation


def test_a_changed_policy_sql_emits_a_replacement_even_when_the_identity_is_unchanged():
    """Two independent reasons to replace: ``[POLICY:...]`` covers what the policy
    *says* (``force`` excluded), leaving a change to the SQL itself invisible to it."""
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
        for operation in command._tenant_policy_operations(app)
        for line in operation.splitlines()
        if line.startswith('#')
    ]

    assert any(h.startswith(f'# Tenant RLS replaced on "{table}" table!') for h in headers)


# ---------------------------------------------------------------------------
# The singleton trigger functions
# ---------------------------------------------------------------------------


def test_a_changed_function_body_emits_a_replacement_migration(monkeypatch, tmp_path):
    """Singletons by *existence*, which is why an edited body once shipped nothing --
    comparing the recorded digest too is what turns an edit into a migration."""
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
    """A function with nothing recorded at all is exactly what ``--adopt`` is for -- a
    plain CREATE FUNCTION there fails migrate, so OR REPLACE must be selected regardless."""
    command, _, filename = _command_with_scaffold(monkeypatch, tmp_path)
    command.trigger_function_dependency = None
    command.trigger_function_sql = None

    assert command._ensure_trigger_function_migration(adopt=True) is True

    content = (tmp_path / 'migrations' / filename).read_text()
    assert 'CREATE OR REPLACE FUNCTION set_updated_at()' in content


# ---------------------------------------------------------------------------
# Flag combinations
# ---------------------------------------------------------------------------


def test_adopt_and_force_rls_cannot_be_combined():
    """They contradict: ``--force-rls`` acts only on already-recorded policies;
    ``--adopt`` exists precisely because that record is missing."""
    with pytest.raises(CommandError, match='cannot be combined'):
        call_command('makeguitarmigrations', '--adopt', '--force-rls', stdout=StringIO())


def test_the_generated_migrations_import_nothing_from_the_kit():
    """Every migration written since inlining must stand on its own -- one doing ``from
    guitars import sql`` changes meaning when the package is upgraded."""
    migrations = sorted((Path(__file__).parent / 'testapp' / 'migrations').glob('0*.py'))
    inlined = [path for path in migrations if headers_module._RE_SQL_IDENTITY.search(path.read_text())]

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
        match.group(1): identity_module._recorded_sql_identity(content, match)
        for match in headers_module._RE_SOFT_DELETE.finditer(content)
    }

    assert recorded == {'shop_order': 'bbbbbbbbbbbb'}


def test_unforced_policy_tables_reads_the_inlined_form():
    """``force`` isn't a keyword anymore, so it's read off the SQL -- the lookbehind isn't
    optional, or every ``reverse_sql``'s NO FORCE text would read every table as forced."""
    forced = (
        '# Tenant RLS on "a" table! [POLICY:aaaaaaaaaaaa] [SQL:1111]\n'
        'migrations.RunSQL(sql=["""ALTER TABLE a ENABLE ROW LEVEL SECURITY""",'
        '"""ALTER TABLE a FORCE ROW LEVEL SECURITY"""],'
        'reverse_sql=["""ALTER TABLE a NO FORCE ROW LEVEL SECURITY"""]),\n'
        '# Tenant RLS on "b" table! [POLICY:bbbbbbbbbbbb] [SQL:2222]\n'
        'migrations.RunSQL(sql=["""ALTER TABLE b ENABLE ROW LEVEL SECURITY"""],'
        'reverse_sql=["""ALTER TABLE b NO FORCE ROW LEVEL SECURITY"""]),\n'
    )

    assert _unforced_policy_tables(forced) == {'b'}


def test_unforced_policy_tables_still_reads_the_legacy_keyword_form():
    """Pre-inlining migrations record the decision as a keyword argument -- reading only
    the SQL form would put already-forced tables back on the backlog."""
    legacy = (
        '# Tenant RLS on "a" table! [POLICY:aaaaaaaaaaaa]\n'
        'migrations.RunSQL(sql=sql.create_table_rls(table=\'a\', force=True)),\n'
        '# Tenant RLS on "b" table! [POLICY:bbbbbbbbbbbb]\n'
        'migrations.RunSQL(sql=sql.create_table_rls(table=\'b\', force=False)),\n'
    )

    assert _unforced_policy_tables(legacy) == {'b'}


def test_the_sql_identity_pattern_only_reads_its_own_header_line():
    """A digest belongs to the header it sits on, not to whatever follows in the file."""
    content = '# Soft Delete Rule on "shop_order" table!\n# something else [SQL:cccccccccccc]\n'
    match = headers_module._RE_SOFT_DELETE.search(content)

    assert identity_module._recorded_sql_identity(content, match) is None
