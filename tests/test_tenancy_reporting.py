"""The audit sink used by ``'audit'`` enforcement mode.

Two properties matter. It must **deduplicate**, because the alternative is a hot query path
emitting thousands of identical events. And it must **never raise**, because the entire
point of audit mode is to observe a live path without breaking it -- a reporter that threw
would convert "report and proceed" into a 500, which is the behaviour audit mode exists to
avoid.
"""

import logging

import pytest

from guitars.tenancy import reporting


@pytest.fixture(autouse=True)
def _isolated_reporter():
    """Restore the module-level reporter and dedupe set around each test."""
    original = reporting._reporter
    reporting.reset_reported()
    yield
    reporting.set_reporter(original)
    reporting.reset_reported()


def test_report_once_sends_the_first_finding():
    seen = []
    reporting.set_reporter(lambda message, /, **context: seen.append((message, context)))

    assert reporting.report_once('key', 'a leak', mode='audit') is True
    assert seen == [('a leak', {'mode': 'audit'})]


def test_report_once_suppresses_a_repeat_of_the_same_key():
    seen = []
    reporting.set_reporter(lambda message, /, **context: seen.append(message))

    assert reporting.report_once('key', 'first') is True
    assert reporting.report_once('key', 'second') is False

    # Deduped on the key, not the message: the same cause reported twice is one finding.
    assert seen == ['first']


def test_distinct_keys_are_reported_separately():
    seen = []
    reporting.set_reporter(lambda message, /, **context: seen.append(message))

    reporting.report_once(('Model', 'shop', 'missing'), 'one')
    reporting.report_once(('Model', 'shop', 'mismatch'), 'two')

    assert seen == ['one', 'two']


def test_reset_reported_allows_the_same_key_again():
    seen = []
    reporting.set_reporter(lambda message, /, **context: seen.append(message))

    reporting.report_once('key', 'first')
    reporting.reset_reported()
    reporting.report_once('key', 'again')

    assert seen == ['first', 'again']


def test_a_raising_reporter_does_not_break_the_caller(caplog):
    """The guarantee: an audit sink failure must not take down the query it observes."""

    def broken(message, /, **context):
        raise RuntimeError('sentry is down')

    reporting.set_reporter(broken)

    with caplog.at_level(logging.ERROR, logger='guitars.tenancy'):
        assert reporting.report_once('key', 'a leak') is True

    assert 'tenancy audit reporter failed' in caplog.text
    assert 'sentry is down' in caplog.text


def test_a_raising_reporter_still_marks_the_key_as_reported():
    """Otherwise a broken reporter turns dedupe off and floods on every query."""
    calls = []

    def broken(message, /, **context):
        calls.append(message)
        raise RuntimeError('down')

    reporting.set_reporter(broken)

    reporting.report_once('key', 'first')
    assert reporting.report_once('key', 'second') is False
    assert calls == ['first']


def test_the_default_reporter_writes_to_the_guitars_tenancy_logger(caplog):
    """A project with no reporter configured still gets the finding somewhere."""
    with caplog.at_level(logging.WARNING, logger='guitars.tenancy'):
        reporting.report_once('key', 'a leak', mode='audit')

    assert 'a leak' in caplog.text
    assert 'audit' in caplog.text


def test_set_reporter_replaces_rather_than_chains():
    first, second = [], []
    reporting.set_reporter(lambda message, /, **context: first.append(message))
    reporting.set_reporter(lambda message, /, **context: second.append(message))

    reporting.report_once('key', 'finding')

    assert first == []
    assert second == ['finding']
