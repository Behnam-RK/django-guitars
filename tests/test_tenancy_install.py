"""Activation, and the GUC names that are baked into generated SQL.

``install()`` is reached two ways -- ``GuitarsConfig.ready()`` and ``tenanted_manager()`` --
so it has to be genuinely idempotent rather than merely usually-called-once. A
double-connected ``pre_save`` receiver would run the write guard twice per save, which is
harmless today but would double-report in audit mode.

The names are pinned because they are not just runtime strings: ``guitars.sql.policy``
embeds them in ``CREATE POLICY`` statements that get written into migration files. Once a
project has applied such a migration, changing a name here means the policies in its
database read a session setting nothing publishes any more -- and a policy comparing
against an unset GUC matches no rows, so the failure is a silent, total denial.
"""

import ast
import subprocess
import sys
import weakref
from pathlib import Path

from django.db.models.signals import pre_save

import guitars
from guitars import gucs as names
from guitars import tenancy
from guitars.tenancy.enforcement import _WRITE_GUARD_UID, _on_pre_save


def _write_guard_receiver_count() -> int:
    """How many times our ``pre_save`` receiver is connected.

    Indexed rather than unpacked, because Django's receiver entries are
    ``(lookup_key, receiver)`` on older versions and ``(lookup_key, receiver, is_async)``
    on 5.0+ -- unpacking would pin this test to one Django. Receivers may be stored either
    as a weakref or directly, so both are resolved.
    """
    count = 0
    for entry in pre_save.receivers:
        receiver = entry[1]
        if isinstance(receiver, weakref.ReferenceType):
            receiver = receiver()
        if receiver is _on_pre_save:
            count += 1
    return count


class TestActivation:
    def teardown_method(self):
        # Leave enforcement installed: it is the resting state for the rest of the suite,
        # and GuitarsConfig.ready() already installed it once at startup.
        tenancy.install()

    def test_install_connects_the_write_guard(self):
        tenancy.uninstall()
        assert _write_guard_receiver_count() == 0

        tenancy.install()

        assert _write_guard_receiver_count() == 1

    def test_install_is_idempotent(self):
        """Both entry points call this, so a second call must not double-connect."""
        tenancy.install()
        tenancy.install()
        tenancy.install()

        assert _write_guard_receiver_count() == 1

    def test_uninstall_removes_the_write_guard(self):
        tenancy.install()
        tenancy.uninstall()

        assert _write_guard_receiver_count() == 0

    def test_uninstall_is_idempotent(self):
        tenancy.uninstall()
        tenancy.uninstall()

        assert _write_guard_receiver_count() == 0

    def test_the_write_guard_is_connected_with_a_namespaced_dispatch_uid(self):
        """A bare uid could collide with another app's receiver and silently displace it."""
        assert _WRITE_GUARD_UID.startswith('guitars_')


class TestGucNames:
    """These strings are compiled into migration files. Treat them as frozen."""

    def test_the_namespace_matches_the_kits_existing_convention(self):
        # The kit already uses 'rules.hard_deletion': prefix names the mechanism, key names
        # the knob.
        assert names.GUC_PREFIX == 'tenant.'

    def test_bypass_guc_name(self):
        assert names.BYPASS_GUC == 'tenant.bypass'

    def test_guc_name_prefixes_a_dimension(self):
        assert names.guc_name('shop') == 'tenant.shop'
        assert names.guc_name('tenant') == 'tenant.tenant'

    def test_bypass_guc_is_derived_from_the_prefix(self):
        """So changing the namespace cannot leave the bypass key behind in the old one."""
        assert names.BYPASS_GUC == f'{names.GUC_PREFIX}bypass'

    def test_value_separator(self):
        # A single character, and one that cannot appear in a primary key rendered by str().
        assert names.VALUE_SEPARATOR == ','

    def test_the_bypass_key_cannot_collide_with_a_dimension_named_bypass(self):
        """``tenant(bypass=...)`` must not be able to forge a bypass.

        The in-Python reserved key is ``_bypass`` while the published GUC is
        ``tenant.bypass``, so a dimension literally named ``bypass`` publishes to the same
        GUC. Worth knowing about explicitly rather than discovering it as a leak.
        """
        from guitars.tenancy.scope import BYPASS

        assert BYPASS == '_bypass'
        assert names.guc_name(BYPASS) != names.BYPASS_GUC


def test_gucs_module_imports_nothing_at_all():
    """Half of the constraint that justifies this module existing separately."""
    source = Path(guitars.__file__).parent / 'gucs.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split('.')[0])
        elif isinstance(node, ast.ImportFrom):  # relative import
            imported.add('.')

    assert imported == set(), (
        f'guitars/gucs.py must import nothing at all, but imports {sorted(imported)}. '
        f'Anything imported here is pulled into every generated migration via guitars.sql.'
    )


def test_gucs_lives_outside_the_tenancy_package():
    """The other half, and the one that is easy to get wrong.

    Being import-free is not sufficient. Importing a submodule executes its package's
    ``__init__`` first, so while these names lived at ``guitars.tenancy.names`` every
    generated migration's ``from guitars import sql`` pulled in the entire tenancy
    runtime -- connection handling, a signal receiver and a ContextVar -- in order to read
    four strings. Moving the module to the top level is what actually delivers the
    property; this test is what stops it drifting back.
    """
    # A subprocess, not a sys.modules purge. Purging would make every later test file
    # re-import guitars and get *different* class objects -- a second TenantScopeError,
    # a re-registered signal receiver -- so the test would corrupt the session it runs in.
    # A clean interpreter is the only honest way to observe a first import.
    probe = (
        'import sys; from guitars import sql; '
        "print(','.join(sorted(m for m in sys.modules if m.startswith('guitars'))))"
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, '-c', probe],
        capture_output=True,
        text=True,
        check=True,
    )
    imported = set(result.stdout.strip().split(','))

    leaked = sorted(m for m in imported if m.startswith('guitars.tenancy'))
    assert not leaked, (
        f'`from guitars import sql` pulled in {leaked}. Generated migrations do exactly '
        f'this import, so the tenancy runtime must not be reachable from it.'
    )
    assert 'guitars.gucs' in imported, 'expected guitars.sql to reach the GUC names'


# ────────────────────────── trimmed public surface (M5, #12) ────────────────────────── #


class TestTheGucNamesAreNotReExported:
    """``guitars.gucs`` exists precisely so a generated migration can reach these four
    names without pulling in the tenancy runtime -- re-exporting them from
    ``guitars.tenancy`` too would defeat that, so M5 removed the re-export outright."""

    def test_bypass_guc_is_not_on_the_tenancy_package(self):
        assert not hasattr(tenancy, 'BYPASS_GUC')

    def test_none_of_the_four_names_are_in_tenancys_all(self):
        leaked = {'BYPASS_GUC', 'GUC_PREFIX', 'VALUE_SEPARATOR', 'guc_name'} & set(tenancy.__all__)
        assert not leaked


class TestTenantSpecIsGeneratorFacingNotApplicationFacing:
    """Moved to ``guitars.tenancy.spec`` -- read by the RLS policy generator and by
    ``discovery``/``checks``, not something application code is documented to import."""

    def test_tenant_spec_is_not_on_the_tenancy_package(self):
        assert not hasattr(tenancy, 'tenant_spec')

    def test_local_tenant_fields_is_not_on_the_tenancy_package(self):
        assert not hasattr(tenancy, 'local_tenant_fields')

    def test_both_are_reachable_from_spec_directly(self):
        from guitars.tenancy import spec

        assert callable(spec.tenant_spec)
        assert callable(spec.local_tenant_fields)


class TestLifecycleHooksMovedToTesting:
    """The seven test-only lifecycle hooks: ``install()`` stays the one application-facing
    entry point on ``guitars.tenancy`` itself; everything that installs or uninstalls one
    layer in isolation lives in ``guitars.tenancy.testing`` instead."""

    def test_install_is_still_the_one_public_entry_point(self):
        assert 'install' in tenancy.__all__
        assert callable(tenancy.install)

    def test_uninstall_is_not_in_tenancys_all(self):
        assert 'uninstall' not in tenancy.__all__
        # Still callable directly -- guitars' own suite uses it constantly -- just not
        # advertised alongside tenant()/tenanted_manager() as application-facing.
        assert callable(tenancy.uninstall)

    def test_testing_module_exports_the_six_lifecycle_hooks(self):
        from guitars.tenancy import testing

        assert set(testing.__all__) == {
            'install_tenant_guc',
            'install_write_guards',
            'register_checks',
            'uninstall',
            'uninstall_tenant_guc',
            'uninstall_write_guards',
        }
        for name in testing.__all__:
            assert callable(getattr(testing, name)), name

    def test_testings_uninstall_is_the_same_object_as_tenancys(self):
        """A re-export, not a reimplementation -- one behaviour, reachable two ways."""
        from guitars.tenancy import testing

        assert testing.uninstall is tenancy.uninstall
