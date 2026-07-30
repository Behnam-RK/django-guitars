"""Activation, and the GUC names that are baked into generated SQL.

``install()`` is reached two ways -- ``GuitarsConfig.ready()`` and ``TenantedManager()`` --
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
import weakref
from pathlib import Path

from django.db.models.signals import pre_save

import guitars
from guitars import tenancy
from guitars.tenancy import names
from guitars.tenancy.manager import _WRITE_GUARD_UID, _on_pre_save


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


def test_names_module_imports_only_the_standard_library():
    """The constraint that justifies this module existing separately.

    ``guitars.sql`` is imported by every generated migration, and it needs these names. If
    they lived with the runtime, ``from guitars import sql`` would drag connection
    handling, signal receivers and a ContextVar into every ``migrate`` -- so the split is
    load-bearing, and this test is what keeps it that way.
    """
    source = Path(guitars.__file__).parent / 'tenancy' / 'names.py'
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
        f'guitars/tenancy/names.py must import nothing at all, but imports {sorted(imported)}. '
        f'Anything imported here is pulled into every generated migration via guitars.sql.'
    )
