"""Tests for the ``makemigrations`` override that also runs guitar generation.

These exercise the dispatch logic in isolation: the parent (real Django
``makemigrations``) ``handle`` is patched to a no-op and the module-level
``call_command`` is mocked, so no migration files are written and no database is
touched. The guitar generation itself is covered by ``test_command.py``.
"""

from unittest import mock

from django.core.management.commands.makemigrations import Command as DjangoMakeMigrations
from django.test import override_settings

from guitars.management.commands.makemigrations import Command


def _run(**options):
    """Invoke the override's ``handle`` with the parent stubbed out.

    Returns the mocked ``call_command`` so callers can assert on the guitar step.
    """
    with (
        mock.patch.object(DjangoMakeMigrations, 'handle') as parent_handle,
        mock.patch('guitars.management.commands.makemigrations.call_command') as call_command,
    ):
        Command().handle(**options)
        # The real makemigrations always runs first.
        parent_handle.assert_called_once()
    return call_command


def test_generates_guitar_migrations_by_default():
    call_command = _run()

    call_command.assert_called_once()
    assert call_command.call_args.args[0] == 'makeguitarmigrations'
    assert call_command.call_args.kwargs['check_only'] is False


@override_settings(GUITARS_AUTO_MAKE_MIGRATIONS=False)
def test_opt_out_skips_guitar_migrations():
    call_command = _run()

    call_command.assert_not_called()


def test_check_flag_is_passed_through():
    call_command = _run(check_changes=True)

    call_command.assert_called_once()
    assert call_command.call_args.kwargs['check_only'] is True


def test_empty_skips_guitar_migrations_no_recursion():
    # makeguitarmigrations scaffolds via `makemigrations --empty`; the override
    # must not re-run guitar generation for it, or it would recurse forever.
    call_command = _run(empty=True)

    call_command.assert_not_called()


def test_dry_run_skips_guitar_migrations():
    call_command = _run(dry_run=True)

    call_command.assert_not_called()
