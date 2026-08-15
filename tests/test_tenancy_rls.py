"""Row-level-security policies, exercised against real PostgreSQL. Model-free: raw DDL
proves the *SQL* independently of the ORM. Every MTI assertion has a negative control,
since "no rows" alone can't prove enforcement. Requires a non-superuser owning the tables."""

import pytest
from django.db import ProgrammingError, connection, transaction

from guitars import sql
from guitars.tenancy import TenantScopeError, tenancy_bypassed, tenant
from tests.conftest import execute as _execute
from tests.conftest import rows as _rows
from tests.conftest import scalar as _scalar


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


@pytest.fixture
def probe_tables(db):
    """Two tables in an MTI shape, both policied and FORCE'd, seeded for two tenants --
    ``owner`` holds the tenant column; ``child`` shares its primary key and holds none."""
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
    """Precondition for every other test in this module -- a superuser or BYPASSRLS role
    ignores policies entirely, so if this fails, every assertion below is vacuous."""
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
        """``WITH CHECK`` -- the half a read-only policy would miss. Wrapped in
        ``atomic()``: a violation aborts the transaction, and without a savepoint every
        later statement (including teardown) fails on "current transaction is aborted"."""
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
        """The owner's own RLS policy also applies inside the child's owner-join subquery
        -- both compare the same GUC, so a correctly scoped read must still succeed."""
        with tenant(tenant=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}') == 1  # noqa: S608

        with tenant(tenant=TENANT_B):
            assert _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}') == 1  # noqa: S608

    def test_a_child_only_update_is_confined_to_the_scoped_tenant(self, probe_tables):
        """The gap this design closes: ``UPDATE child`` touches no ancestor table, so an
        ancestor-only policy never applies to it -- same blind spot as timestamps."""
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
        """Negative control: the ancestor is policied, the child is not, so a child-only
        statement sails past the only policy. Takes ``DISABLE`` too -- RLS enabled with no
        policy is default-**deny**, so dropping the policy alone would look reassuring."""
        _execute(sql.drop_tenant_policy(_CHILD_TABLE), sql.disable_rls(_CHILD_TABLE))

        with tenant(tenant=TENANT_A):
            leaked = _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}')  # noqa: S608

        assert leaked == 2, (
            'expected an unpolicied child to leak every tenant, which is the premise for '
            'covering MTI children explicitly rather than trusting transitivity'
        )

    def test_rls_enabled_with_no_policy_denies_everything(self, probe_tables):
        """A half-applied teardown that drops the policy but leaves ENABLE in place fails
        shut, not open -- the table goes dark rather than leaking."""
        _execute(sql.drop_tenant_policy(_CHILD_TABLE))

        with tenant(tenant=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_CHILD_TABLE}') == 0  # noqa: S608


class TestThreeLevelMTIOwnerJoin:
    """The real, three-level case: ``Tour -> WorldTour -> StadiumTour``, tenant column two
    tables up. Proven directly against the grandchild table, never through
    ``StadiumTour.objects``, so a leak can't be masked by Python-side filtering."""

    @pytest.mark.django_db(transaction=True)
    def test_a_grandchild_only_select_leaks_without_its_own_policy(self, tenants):
        """Negative control: grandchild's policy gone *and RLS disabled* (dropping the
        policy alone is default-deny, the opposite of the leak this demonstrates).
        ``transaction=True``: inserts must commit before ``ALTER TABLE`` below runs."""
        _execute(
            sql.drop_tenant_policy(table='testapp_stadiumtour'),
            sql.disable_rls(table='testapp_stadiumtour'),
        )
        try:
            with tenant(label=tenants.a):
                capacities = _rows('SELECT capacity FROM testapp_stadiumtour')
            assert len(capacities) == 2, (
                'expected both tenants to leak once the grandchild lost its own policy'
            )
        finally:
            _execute(
                sql.create_tenant_policy(
                    table='testapp_stadiumtour',
                    columns={},
                    owner_table='testapp_tour',
                    owner_pk='id',
                    child_pk='worldtour_ptr_id',
                    owner_columns={'label': 'label_id'},
                ),
                sql.enable_rls(table='testapp_stadiumtour'),
                sql.force_rls(table='testapp_stadiumtour'),
            )

    def test_a_grandchild_only_select_is_scoped_with_the_policy_in_place(self, tenants):
        with tenant(label=tenants.a):
            capacities = _rows('SELECT capacity FROM testapp_stadiumtour')

        assert capacities == [(tenants.tour_a.capacity,)]

    def test_a_grandchild_only_update_under_the_wrong_tenant_touches_nothing(self, tenants):
        """The USING half at the grandchild level: naming another tenant's row by pk still
        affects zero rows -- the owner-join predicate, two tables up, filters it first."""
        with tenant(label=tenants.a):
            _execute(
                'UPDATE testapp_stadiumtour SET capacity = 1 '
                f'WHERE worldtour_ptr_id = {tenants.tour_b.pk}'  # noqa: S608
            )

        with tenancy_bypassed():
            unchanged = _scalar(
                'SELECT capacity FROM testapp_stadiumtour WHERE worldtour_ptr_id = %s',
                [tenants.tour_b.pk],
            )
        assert unchanged == tenants.tour_b.capacity

    def test_a_grandchild_only_update_under_its_own_tenant_succeeds(self, tenants):
        with tenant(label=tenants.a):
            _execute(
                'UPDATE testapp_stadiumtour SET capacity = 12345 '
                f'WHERE worldtour_ptr_id = {tenants.tour_a.pk}'  # noqa: S608
            )
            assert _scalar('SELECT capacity FROM testapp_stadiumtour') == 12345


class TestRecoveryFromARejectedWrite:
    """A rejected write must leave the connection usable -- Postgres refuses every
    statement until rollback, including the recovery ROLLBACK TO SAVEPOINT itself, which
    reaches the GUC publisher's execute wrapper first. Not a test-only concern."""

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
        """Skipping the publish is only safe if the cache is left untouched -- if it
        recorded state, the next block would trust it and read as the previous tenant."""
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
        """Why FORCE is emitted by default: ENABLE alone is inert against the table's
        owner (the application role, since it runs migrations) -- silently, no error."""
        _execute(sql.no_force_rls(_OWNER_TABLE))

        with tenant(tenant=TENANT_A):
            unfiltered = _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}')  # noqa: S608

        assert unfiltered == 2

        _execute(sql.force_rls(_OWNER_TABLE))
        with tenant(tenant=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 1  # noqa: S608


class TestReplacingAPolicyInPlace:
    """A changed coverage shape can't simply be ``create_table_rls``'d again -- Postgres
    has no CREATE OR REPLACE POLICY. The generator emits the replacement form instead."""

    def test_re_creating_a_policy_that_exists_fails(self, probe_tables):
        """The reason ``replace_table_rls`` exists -- pinned so a future Postgres gaining
        CREATE OR REPLACE POLICY says so. Its own ``atomic()`` contains the abort."""
        with pytest.raises(ProgrammingError, match='already exists'), transaction.atomic():
            _execute(sql.create_tenant_policy(table=_OWNER_TABLE, columns={'tenant': 'tenant_id'}))

    def test_replacing_swaps_the_predicate_and_keeps_the_table_enforced(self, probe_tables):
        """ENABLE/FORCE are never dropped on the way -- cycling them would leave the table
        momentarily unprotected or, worse, enabled with no policy (default-DENY)."""
        _execute(
            *sql.replace_table_rls(table=_OWNER_TABLE, columns={'other': 'tenant_id'}, force=True)
        )

        # Still enabled and still forced, without an intervening drop.
        assert _rows(
            'SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = %s',
            [_OWNER_TABLE],
        ) == [(True, True)]
        # Exactly one tenant_scope policy -- a replacement, not an accumulation.
        assert (
            _scalar(
                'SELECT count(*) FROM pg_policies WHERE tablename = %s AND policyname = %s',
                [_OWNER_TABLE, sql.TENANT_POLICY],
            )
            == 1
        )
        # And it is the NEW predicate that binds: the old dimension no longer grants access.
        with tenant(tenant=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 0  # noqa: S608
        with tenant(other=TENANT_A):
            assert _scalar(f'SELECT count(*) FROM {_OWNER_TABLE}') == 1  # noqa: S608

    def test_replacing_drops_an_exemption_for_a_role_no_longer_configured(self, probe_tables):
        """A role removed from ``GUITARS_RLS_EXEMPT_ROLES`` must lose its exemption --
        discovered from ``pg_policy``, so the replacement converges instead of accumulating."""
        _execute(sql.create_exempt_policy(table=_OWNER_TABLE, role='guitars'))
        assert (
            _scalar(
                'SELECT count(*) FROM pg_policies WHERE tablename = %s AND policyname LIKE %s',
                [_OWNER_TABLE, f'{sql.EXEMPT_POLICY_PREFIX}%'],
            )
            == 1
        )

        # Replaced with no exempt_roles at all, as if the setting had been emptied.
        _execute(
            *sql.replace_table_rls(table=_OWNER_TABLE, columns={'tenant': 'tenant_id'}, force=True)
        )

        assert (
            _scalar(
                'SELECT count(*) FROM pg_policies WHERE tablename = %s AND policyname LIKE %s',
                [_OWNER_TABLE, f'{sql.EXEMPT_POLICY_PREFIX}%'],
            )
            == 0
        )

    def test_dropping_all_exemptions_leaves_the_tenant_policy_alone(self, probe_tables):
        """It is prefix-scoped, so it must not take the policy that does the actual work."""
        _execute(sql.drop_all_exempt_policies(table=_OWNER_TABLE))

        assert (
            _scalar(
                'SELECT count(*) FROM pg_policies WHERE tablename = %s AND policyname = %s',
                [_OWNER_TABLE, sql.TENANT_POLICY],
            )
            == 1
        )


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
    """Identifiers reaching policy SQL are proven bare or quoted, never assumed. Not an
    injection boundary (nothing here is untrusted) -- what's pinned is *correctness*."""

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
        """Loudly, naming the culprit -- not emitted as SQL that fails as a syntax error
        inside `migrate`, pointing at a generated file rather than the real cause."""
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
        """Round-tripped through Postgres: the ``DO`` block's ``EXECUTE`` nests escaping
        twice, easy to get wrong in a way only the parser catches. Also proves the
        ``pg_roles`` guard: none of these roles exist, so each block finds nothing."""
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
        """The BI-exemption feature, end to end. Uses the connecting role -- creating one
        needs CREATEROLE, deliberately absent (pre-PG16 it could grant BYPASSRLS). The
        count proves the binding, not a ``pg_policy`` lookup that would pass either way."""
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
