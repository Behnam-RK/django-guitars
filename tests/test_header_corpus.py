"""Frozen-corpus guard for deriving ``_RE_*`` scanners from their ``HEADER_*`` templates.

``HEADER_SCANNERS`` in ``test_enforcement_identity.py`` already proves each scanner reads
back what its own template just emitted -- but that fixture is synthetic, one hand-typed
example per kind. It cannot catch a derivation bug that only shows up against the messier,
real header text a project actually accumulates over many releases (varying table names,
multiple FKs between the same two tables, a policy that has been replaced, and so on).

This test sources its fixture from ``tests/testapp/migrations/*.py`` instead: the project's
own committed migration history. For every scanner, it pins the exact regex source that
existed before any of them were mechanically derived from their ``HEADER_*`` template, and
asserts the current (partly derived) scanner still matches that real corpus identically --
same spans, same captured groups. A derivation that silently narrows or widens what a
scanner recognises would otherwise only surface as a stale or duplicated migration in
someone else's project.
"""

import re
from pathlib import Path

import pytest

from guitars.management.enforcement import headers as headers_module
from guitars.management.enforcement import identity as identity_module

_CORPUS_DIR = Path(__file__).parent / 'testapp' / 'migrations'

#: The hand-written regex source each ``_RE_*`` had before commit 4 (issue #10) derived
#: some of them from their ``HEADER_*`` template. Pinned here, independent of the module
#: under test, as the baseline the current scanners must still reproduce match-for-match.
_BASELINE = {
    '_RE_TRIGGER_FUNCTION': re.compile(r'# Define function for updated at triggers!'),
    '_RE_PARENT_TRIGGER_FUNCTION': re.compile(
        r'# Define function for MTI parent updated at triggers!'
    ),
    '_RE_UPDATED_AT': re.compile(r'# Updated at Trigger on "([^"]+)" table!'),
    '_RE_SOFT_DELETE': re.compile(r'# Soft Delete Rule on "([^"]+)" table!'),
    '_RE_SOFT_DELETE_RELATED': re.compile(
        r'# Soft Delete Related Rule on "([^"]+)" that is related to "([^"]+)"'
        r'(?: via "(?P<foreign_key>[^"]+)")?'
    ),
    '_RE_MTI_UPDATED_AT': re.compile(r'# MTI Updated at Trigger on "([^"]+)" table'),
    '_RE_MTI_SOFT_DELETE': re.compile(r'# MTI Soft Delete Rule on "([^"]+)" table'),
    '_RE_TENANT_POLICY': re.compile(
        r'# Tenant RLS (?:replaced )?on "([^"]+)" table! \[POLICY:(?P<identity>\w+)\]'
    ),
    '_RE_TENANT_FORCE': re.compile(r'# Tenant FORCE RLS on "([^"]+)" table!'),
}

#: No committed migration has ever gone through the ``--force-rls`` retrofit stage, so this
#: is the one scanner with nothing to match in the real corpus -- both baseline and current
#: agreeing on zero matches is the whole assertion for it, not a weaker check.
_EXPECTED_EMPTY = {'_RE_TENANT_FORCE'}

#: ``_RE_TENANT_POLICY``'s own baseline still captures ``[POLICY:...]`` inline as a named
#: group (that was the *other* half of commit 4's "3 need explicit handling" list); commit 5
#: moved reading it out to a separate tail search (``_recorded_policy_identity``), the same
#: mechanism ``[SQL:...]`` already used, so the two tokens are read the same way even though
#: they mean different things. Comparing raw ``groupdict()`` here would fail for exactly the
#: reason that move is safe: the *shape* of the match changed on purpose, the *identity
#: value it recovers* did not -- so this scanner is checked by round-tripping the identity
#: each mechanism recovers, not by diffing capture groups.
_IDENTITY_VIA_TAIL_SEARCH = {'_RE_TENANT_POLICY': identity_module._recorded_policy_identity}


def _corpus_text() -> str:
    return '\n'.join(path.read_text() for path in sorted(_CORPUS_DIR.glob('*.py')))


@pytest.mark.parametrize('name', sorted(_BASELINE))
def test_scanner_matches_the_real_migration_corpus_like_its_pre_derivation_baseline(name):
    content = _corpus_text()
    baseline = _BASELINE[name]
    current = getattr(headers_module, name)

    baseline_matches = list(baseline.finditer(content))
    current_matches = list(current.finditer(content))

    if name not in _EXPECTED_EMPTY:
        assert baseline_matches, f'test fixture problem: {name} matched nothing in the corpus'

    assert [m.span() for m in current_matches] == [m.span() for m in baseline_matches], (
        f'{name} matches different text in the real migration corpus than its '
        f'pre-derivation baseline -- derivation changed matching behaviour'
    )

    identity_extractor = _IDENTITY_VIA_TAIL_SEARCH.get(name)
    for base_match, cur_match in zip(baseline_matches, current_matches, strict=True):
        if identity_extractor is None:
            assert cur_match.groups() == base_match.groups()
            assert cur_match.groupdict() == base_match.groupdict()
        else:
            # The table name (group 1) is still positionally captured by both; only the
            # identity token's own extraction mechanism moved.
            assert cur_match.group(1) == base_match.group(1)
            assert identity_extractor(content, cur_match) == base_match.group('identity')
