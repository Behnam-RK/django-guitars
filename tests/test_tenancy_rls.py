"""Row-level-security policies, exercised against real PostgreSQL.

Deliberately model-free: the tables here are built with raw DDL, so this proves the *SQL*
independently of the ORM layer that will generate it. The whole claim of the database half
is that it covers statements no Django manager ever sees, and a test that went through a
manager could not distinguish the policy working from the manager filtering.

The MTI case is the one worth the trouble. It is tempting to believe an MTI child is
protected transitively -- every ORM query joins the parent, so the parent's policy filters.
That is false for a *child-only* statement, which is why these tests assert against the
child table directly, and why each MTI assertion has a negative control: the leak is
demonstrated with the child's policy dropped, then shown closed with it in place. Without
the control, "no rows came back" cannot be distinguished from "the query happened not to
match anything".

Requires the connecting role to be a non-superuser that owns these tables -- see
scripts/postgres-init.sql. A superuser, or a role with BYPASSRLS, would make every
assertion below pass vacuously, so that precondition is asserted first.
"""

import pytest
from django.db import connection, transaction

from guitars import sql
from guitars.tenancy import TenantScopeError, tenancy_bypassed, tenant


TENANT_A, TENANT_B = 1, 2

_OWNER_TABLE = 'rls_probe_owner'
_CHILD_TABLE = 'rls_probe_child'

_DDL = f"""
CREATE TABLE {_OWNER_TABLE} (
    id serial PRIMARY KEY,
    tenant_id integer NOT NULL,
    name text
);
CREATE TABLE {_CHILD_TABLE} (
    owner_ptr_id integer PRIMARY KEY REFERENCES {_OWNER_TABLE}(id),
    note text
);
"""


def _execute(*statements: str) -> None:
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


def _scalar(query: str, params: list | None = None):
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        row = cursor.fetchone()
    return row[0] if row else None


def _rows(query: str, params: list | None = None) -> list:
    with connection.cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall()


@pytest.fixture
def probe_tables(db):
    """Two tables in an MTI shape, both policied and FORCE'd, seeded for two tenants.

    ``owner`` holds the tenant column; ``child`` shares its primary key and holds none --
    exactly the shape Django multi-table inheritance produces.
    """
    _execute(_DDL)
    _execute(
        *sql.create_table_rls(table=_OWNER_TABLE, columns={'tenant': 'tenant_id'}, force=True),
        *sql.create_table_rls(
            table=_CHILD_TABLE,
            columns={},
            owner_table=_OWNER_TABLE,
            owner_pk='id',
            child_pk='owner_ptr_id',
            owner_columns={'tenant': 'tenant_id'},
            force=True,
        ),
    )
    # Seeding is itself subject to the policies now, so it needs the bypass -- which is also
    # a first check that tenancy_bypassed() reaches the database and not just Python.
    with tenancy_bypassed(), connection.cursor() as cursor:
        for tenant_id, name in ((TENANT_A, 'a'), (TENANT_B, 'b')):
            cursor.execute(
                f'INSERT INTO {_OWNER_TABLE} (tenant_id, name) VALUES (%s, %s) RETURNING id',  # noqa: S608
                [tenant_id, name],
            )
            owner_id = cursor.fetchone()[0]
            cursor.execute(
                f'INSERT INTO {_CHILD_TABLE} (owner_ptr_id, note) VALUES (%s, %s)',  # noqa: S608
                [owner_id, f'note-{name}'],
            )
    yield
    _execute(f'DROP TABLE IF EXISTS {_CHILD_TABLE}', f'DROP TABLE IF EXISTS {_OWNER_TABLE}')


def test_the_connecting_role_cannot_bypass_rls(db):
    """Precondition for every other test in this module.

    A superuser, or any role holding BYPASSRLS, ignores policies entirely -- so if this
    fails, every assertion below is vacuous rather than merely wrong.
    """
    can_bypass = _scalar(
        'SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user'
    )

    assert can_bypass is False, (
        'the test role can bypass RLS, so these tests would pass without enforcing '
        'anything -- see scripts/postgres-init.sql'
    )


class TestOwnTablePolicy:
    """The ordinary case: the tenant column lives on the table being filtered."""

    def test_unscoped_reads_return_nothing(self, probe_tables):
        """Fail-closed: an unset GUC yields NULL, and the predicate denies."""
        assert _rows(f'SELECT * FROM {_OWNER_TABLE}') == []  # noqa: S608

    def test_a_scoped_read_sees_only_its_own_tenant(self, probe_tables):
        with tenant(tenant=TENANT_A):
            names = _rows(f'SELECT name FROM {_OWNER_TABLE}')  # noqa: S608

        assert names == [('a',)]

    def test_bypass_sees_every_tenant(self, probe_tables):
        with tenancy_bypassed():
            count = _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}')  # noqa: S608

        assert count == 2

    def test_a_collection_scope_sees_all_of_its_tenants(self, probe_tables):
        """One policy form serves scalar and collection scopes -- the GUC is always a list."""
        with tenant(tenant=[TENANT_A, TENANT_B]):
            count = _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}')  # noqa: S608

        assert count == 2

    def test_an_update_cannot_reach_another_tenants_rows(self, probe_tables):
        with tenant(tenant=TENANT_A):
            _execute(f"UPDATE {_OWNER_TABLE} SET name = 'touched'")  # noqa: S608

        with tenancy_bypassed():
            assert sorted(_rows(f'SELECT name FROM {_OWNER_TABLE}')) == [  # noqa: S608
                ('b',),
                ('touched',),
            ]

    def test_a_delete_cannot_reach_another_tenants_rows(self, probe_tables):
        with tenant(tenant=TENANT_A):
            _execute(f'DELETE FROM {_CHILD_TABLE}', f'DELETE FROM {_OWNER_TABLE}')  # noqa: S608

        with tenancy_bypassed():
            assert _rows(f'SELECT tenant_id FROM {_OWNER_TABLE}') == [(TENANT_B,)]  # noqa: S608

    def test_an_insert_into_another_tenant_is_rejected(self, probe_tables):
        """``WITH CHECK`` -- the half a read-only policy would miss.

        Wrapped in ``atomic()`` because a policy violation aborts the PostgreSQL
        transaction: without a savepoint to roll back to, every later statement in this
        test -- including the fixture's teardown -- fails with "current transaction is
        aborted" and the real assertion is lost behind that noise.
        """
        with tenant(tenant=TENANT_A), pytest.raises(TenantScopeError, match='tenant policy'):
            with transaction.atomic():
                _execute(
                    f'INSERT INTO {_OWNER_TABLE} (tenant_id, name) '  # noqa: S608
                    f"VALUES ({TENANT_B}, 'sneaky')"
                )

        # The row was refused, not merely reported.
        with tenancy_bypassed():
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 2  # noqa: S608

    def test_an_insert_into_the_active_tenant_is_allowed(self, probe_tables):
        """The complement: WITH CHECK must not refuse a legitimate write."""
        with tenant(tenant=TENANT_A):
            _execute(
                f'INSERT INTO {_OWNER_TABLE} (tenant_id, name) '  # noqa: S608
                f"VALUES ({TENANT_A}, 'legitimate')"
            )
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 2  # noqa: S608


class TestMtiOwnerJoinPolicy:
    """The child table holds no tenant column; its policy reaches the owner by shared PK."""

    def test_unscoped_reads_of_the_child_return_nothing(self, probe_tables):
        assert _rows(f'SELECT * FROM {_CHILD_TABLE}') == []  # noqa: S608

    def test_a_scoped_read_of_the_child_sees_only_its_own_tenant(self, probe_tables):
        with tenant(tenant=TENANT_A):
            notes = _rows(f'SELECT note FROM {_CHILD_TABLE}')  # noqa: S608

        assert notes == [('note-a',)]

    def test_the_ancestors_policy_inside_the_subquery_does_not_over_deny(self, probe_tables):
        """The subtle one, called out in planning as needing proof rather than assumption.

        The child's policy runs a subquery against the owner, and the *owner's* own RLS
        policy applies to that subquery too. Both compare the same GUC, so a correctly
        scoped read must still succeed -- if the nesting denied, this returns nothing and
        the whole MTI approach is unsound.
        """
        with tenant(tenant=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}') == 1  # noqa: S608

        with tenant(tenant=TENANT_B):
            assert _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}') == 1  # noqa: S608

    def test_a_child_only_update_is_confined_to_the_scoped_tenant(self, probe_tables):
        """The gap this design closes.

        ``UPDATE child SET note = ...`` touches no ancestor table, so an ancestor-only
        policy never applies to it. This is the same blind spot ``set_parent_updated_at``
        exists to cover for timestamps.
        """
        with tenant(tenant=TENANT_A):
            _execute(f"UPDATE {_CHILD_TABLE} SET note = 'rewritten'")  # noqa: S608

        with tenancy_bypassed():
            assert sorted(_rows(f'SELECT note FROM {_CHILD_TABLE}')) == [  # noqa: S608
                ('note-b',),
                ('rewritten',),
            ]

    def test_a_child_only_delete_is_confined_to_the_scoped_tenant(self, probe_tables):
        with tenant(tenant=TENANT_A):
            _execute(f'DELETE FROM {_CHILD_TABLE}')  # noqa: S608

        with tenancy_bypassed():
            assert _rows(f'SELECT note FROM {_CHILD_TABLE}') == [('note-b',)]  # noqa: S608

    def test_an_unpolicied_child_leaks_every_tenant(self, probe_tables):
        """Negative control, without which the tests above prove nothing.

        This reproduces the arrangement guitars rejects: the ancestor is policied, the child
        is not. A child-only statement then sails straight past the only policy there is.

        Note it takes ``DISABLE`` as well as dropping the policy, and that is worth knowing:
        a table with RLS *enabled* and no policies at all is default-**deny**, not
        default-allow. Dropping the policy alone made the child return zero rows, which
        looks reassuring and is the opposite of the situation being guarded against.
        """
        _execute(sql.drop_tenant_policy(_CHILD_TABLE), sql.disable_rls(_CHILD_TABLE))

        with tenant(tenant=TENANT_A):
            leaked = _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}')  # noqa: S608

        assert leaked == 2, (
            'expected an unpolicied child to leak every tenant, which is the premise for '
            'covering MTI children explicitly rather than trusting transitivity'
        )

    def test_rls_enabled_with_no_policy_denies_everything(self, probe_tables):
        """The behaviour discovered by the control above, pinned in its own right.

        It matters for rollback safety: a half-applied teardown that drops the policy but
        leaves ENABLE in place does not fail open, it fails shut -- so the table goes dark
        rather than leaking. ``drop_table_rls`` orders its statements to avoid either.
        """
        _execute(sql.drop_tenant_policy(_CHILD_TABLE))

        with tenant(tenant=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}') == 0  # noqa: S608


class TestRecoveryFromARejectedWrite:
    """A rejected write must leave the connection usable.

    Regression test. PostgreSQL fails the whole transaction on a policy violation and then
    refuses every statement until rollback -- including the ``ROLLBACK TO SAVEPOINT`` Django
    issues to recover, which travels through ``connection.cursor()`` and therefore reaches
    the GUC publisher's execute wrapper first. When that wrapper insisted on publishing, it
    blocked the one statement able to clear the error: the connection wedged permanently and
    the original ``TenantScopeError`` was buried under "current transaction is aborted".

    Any application catching a TenantScopeError inside ``atomic()`` hits this, so it is not
    a test-only concern.
    """

    def test_the_connection_survives_a_policy_violation(self, probe_tables):
        with tenant(tenant=TENANT_A):
            with pytest.raises(TenantScopeError):
                with transaction.atomic():
                    _execute(
                        f'INSERT INTO {_OWNER_TABLE} (tenant_id, name) '  # noqa: S608
                        f"VALUES ({TENANT_B}, 'nope')"
                    )

            # The savepoint rollback landed, so the connection still works and the scope is
            # still in force.
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 1  # noqa: S608

    def test_a_tenant_switch_after_a_violation_still_republishes(self, probe_tables):
        """The recovery path must not leave a stale frame cached.

        Skipping the publish is only safe because the cache is left untouched; if the
        skipped attempt had recorded state, the next block would trust it and read as the
        *previous* tenant -- failing open, which is the one outcome this module exists to
        rule out.
        """
        with tenant(tenant=TENANT_A):
            with pytest.raises(TenantScopeError), transaction.atomic():
                _execute(
                    f'INSERT INTO {_OWNER_TABLE} (tenant_id, name) '  # noqa: S608
                    f"VALUES ({TENANT_B}, 'nope')"
                )

        with tenant(tenant=TENANT_B):
            names = _rows(f'SELECT name FROM {_OWNER_TABLE}')  # noqa: S608

        assert names == [('b',)], 'expected tenant B rows, so the frame was republished'


class TestForceIsWhatMakesItBind:
    def test_without_force_the_owning_role_bypasses_the_policy(self, probe_tables):
        """Why FORCE is emitted by default.

        ENABLE alone is inert against the table's owner -- and the application role owns
        its tables, because it runs the migrations. Silently: no error, no log, rows simply
        come back unfiltered.
        """
        _execute(sql.no_force_rls(_OWNER_TABLE))

        with tenant(tenant=TENANT_A):
            unfiltered = _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}')  # noqa: S608

        assert unfiltered == 2

        _execute(sql.force_rls(_OWNER_TABLE))
        with tenant(tenant=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 1  # noqa: S608


class TestPolicyTeardown:
    def test_drop_table_rls_removes_the_policy_and_the_enforcement(self, probe_tables):
        """The reverse operation must actually reverse, or a rollback leaves a dead table."""
        _execute(*sql.drop_table_rls(table=_OWNER_TABLE))

        state = _rows(
            'SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s',
            [_OWNER_TABLE],
        )
        assert state == [(False, False)]

        policies = _scalar(
            'SELECT count(*) FROM pg_policies WHERE tablename = %s AND policyname = %s',
            [_OWNER_TABLE, sql.TENANT_POLICY],
        )
        assert policies == 0

        # And with enforcement gone the rows are readable again, proving the drop took
        # effect rather than the assertions above merely reading stale catalog state.
        assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 2  # noqa: S608


class TestIdentifierSafety:
    """Identifiers reaching policy SQL are proven bare, or quoted -- never assumed.

    Nothing untrusted arrives here: tables and columns come from Django's ``model._meta``
    and role names from ``settings``. So this is not an injection boundary, and these tests
    do not pretend to be penetration tests. What they pin is *correctness* -- SQL that is
    valid and binds the object actually named -- and the loudness of the failure when it
    cannot be.
    """

    #: One case per interpolation site, so a new site added without a guard shows up here
    #: rather than at some consumer's `migrate`.
    BARE_SITES = [
        pytest.param(lambda name: sql.enable_rls(table=name), id='enable_rls'),
        pytest.param(lambda name: sql.disable_rls(table=name), id='disable_rls'),
        pytest.param(lambda name: sql.force_rls(table=name), id='force_rls'),
        pytest.param(lambda name: sql.no_force_rls(table=name), id='no_force_rls'),
        pytest.param(lambda name: sql.drop_tenant_policy(table=name), id='drop_tenant_policy'),
        pytest.param(
            lambda name: sql.create_tenant_policy(table=name, columns={'tenant': 'tenant_id'}),
            id='create_tenant_policy-table',
        ),
        pytest.param(
            lambda name: sql.create_tenant_policy(table='t', columns={'tenant': name}),
            id='create_tenant_policy-column',
        ),
        pytest.param(
            lambda name: sql.create_tenant_policy(
                table='t',
                columns={},
                owner_table=name,
                owner_pk='id',
                child_pk='t_ptr_id',
                owner_columns={'tenant': 'tenant_id'},
            ),
            id='create_tenant_policy-owner_table',
        ),
        pytest.param(
            lambda name: sql.create_tenant_policy(
                table='t',
                columns={},
                owner_table='o',
                owner_pk='id',
                child_pk='t_ptr_id',
                owner_columns={'tenant': name},
            ),
            id='create_tenant_policy-owner_column',
        ),
        pytest.param(lambda name: sql.create_exempt_policy(table=name, role='r'), id='exempt'),
        pytest.param(lambda name: sql.drop_exempt_policy(table=name, role='r'), id='drop_exempt'),
    ]

    @pytest.mark.parametrize('build', BARE_SITES)
    @pytest.mark.parametrize(
        'name',
        [
            'Order Items',  # a legal db_table that is not a bare identifier
            'MixedCase',  # would silently fold to lower case and bind another table
            'has-a-hyphen',  # a syntax error, unquoted
            'tenant_id; DROP TABLE x',  # the shape a linter worries about
        ],
    )
    def test_an_unquotable_identifier_is_refused_at_build_time(self, build, name):
        """Loudly, and naming the culprit -- not emitted as SQL that fails later.

        Emitting it would surface as a PostgreSQL syntax error inside `migrate`, pointing at
        a generated file rather than at the field or setting that caused it.
        """
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            build(name)

    def test_an_empty_table_name_is_refused(self):
        """Kept separate: on the owner-join sites an empty name trips the
        join-arguments guard in ``_predicate`` first, which is its own correct error."""
        with pytest.raises(ValueError, match='not a plain lower-case SQL identifier'):
            sql.enable_rls(table='')

    @pytest.mark.parametrize(
        'role',
        [
            'metabase_ro',  # ordinary
            'metabase-ro',  # legal role, syntax error unquoted
            'BI_Reader',  # legal role, silently folds to bi_reader unquoted
            "quote'in'name",  # escaping, at both nesting levels
        ],
    )
    def test_a_role_name_needing_quotes_still_produces_valid_sql(self, role, db):
        """Round-tripped through PostgreSQL, because the escaping is nested twice.

        The ``DO`` block's ``EXECUTE`` takes a *string literal* containing a statement that
        itself quotes the role, so every quote is escaped once at each level. That is easy
        to get wrong in a way no string assertion would catch -- only the parser will.
        Running it also proves the ``pg_roles`` guard works: none of these roles exist here,
        so each block must compile, find nothing, and do nothing.
        """
        _execute('CREATE TABLE IF NOT EXISTS guitars_ident_probe (id serial PRIMARY KEY)')
        try:
            _execute(sql.create_exempt_policy(table='guitars_ident_probe', role=role))
            _execute(sql.drop_exempt_policy(table='guitars_ident_probe', role=role))
        finally:
            _execute('DROP TABLE IF EXISTS guitars_ident_probe')

    def test_the_create_and_drop_agree_on_the_policy_name(self):
        """Or a dropped exemption would leave a policy nothing knows how to remove."""
        role = 'BI_Reader'

        created = sql.create_exempt_policy(table='t', role=role)
        dropped = sql.drop_exempt_policy(table='t', role=role)

        assert f'"{sql.EXEMPT_POLICY_PREFIX}{role}"' in created
        assert f'"{sql.EXEMPT_POLICY_PREFIX}{role}"' in dropped

    def test_an_exempt_role_actually_reads_across_tenants(self, probe_tables):
        """The BI-exemption feature, end to end.

        Uses the connecting role, because creating one would need CREATEROLE -- which this
        suite's role deliberately lacks: before PostgreSQL 16, CREATEROLE could grant
        BYPASSRLS, and a role able to hand itself that would undermine every other assertion
        in this file.

        Functional rather than structural. A mis-quoted role name, or one attached to the
        folded spelling of the name, would leave this read at zero rows -- so the count is
        what proves the binding, not a ``pg_policy`` lookup that would pass either way.
        """
        role = _scalar('SELECT current_user')

        # Precondition: unscoped, the tenant policy shows nothing.
        assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 0  # noqa: S608

        _execute(sql.create_exempt_policy(table=_OWNER_TABLE, role=role))
        try:
            # Policies are permissive, so the exemption ORs with tenant_scope: full read.
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 2  # noqa: S608
        finally:
            _execute(sql.drop_exempt_policy(table=_OWNER_TABLE, role=role))

        # And the drop puts it back -- SELECT-only exemptions must not outlive their removal.
        assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 0  # noqa: S608
