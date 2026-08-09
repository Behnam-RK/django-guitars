"""Direct tests for guitars.tenancy.messages -- the remediation sentence extracted (M5,
#12) from four independently-typed copies (guc.py and what are now
enforcement.py/querysets.py) that had already drifted into two different wordings.
"""

from guitars.tenancy.messages import remediation


def test_default_form_offers_both_escape_hatches():
    assert remediation('write') == (
        'wrap it in tenant(...), or tenancy_bypassed() for a deliberate cross-tenant write.'
    )


def test_default_form_names_the_action():
    assert 'read' in remediation('read')
    assert 'bulk_create' in remediation('bulk_create')


def test_scope_is_active_form_offers_only_the_bypass():
    message = remediation('write', scope_is_active=True)

    assert message == 'Use tenancy_bypassed() if that write is genuinely intended.'
    assert 'tenant(...)' not in message


def test_scope_is_active_form_is_capitalised_to_follow_a_period():
    """The default form is lowercase (meant to follow an em dash); this one is not --
    it is meant to follow a period instead, in a scope-already-active message."""
    assert remediation('write', scope_is_active=True).startswith('Use')
    assert remediation('write').startswith('wrap')
