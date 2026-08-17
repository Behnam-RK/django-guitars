"""Frozen-corpus guard for deriving ``_RE_*`` scanners from their ``HEADER_*`` templates,
sourcing real header text from ``tests/testapp/migrations/*.py`` and pinning the
pre-derivation regex each scanner must still match identically."""

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
    # Born derived/hand-written in 2.1.0 rather than converted, so these two have no
    # pre-derivation history; the baseline is the naive `[^"]+` spelling anyway, which is
    # what pins the escaped-quote and stop-before-the-function behaviour against the corpus.
    '_RE_TENANT_AUTOFILL_FUNCTION': re.compile(r'# Tenant autofill function "([^"]+)"!'),
    '_RE_TENANT_AUTOFILL': re.compile(
        r'# Tenant autofill Trigger on "([^"]+)" table \(function "([^"]+)"\)'
    ),
    '_RE_TENANT_AUTOFILL_RETIRED': re.compile(
        r'# Tenant autofill Trigger retired on "([^"]+)" table \(function "([^"]+)"\)'
    ),
}

#: No committed migration has ever gone through the ``--force-rls`` retrofit stage, nor had
#: an autofill trigger retired, so these are the scanners with nothing to match in the real
#: corpus -- baseline and current agreeing on zero is the whole assertion, not a weaker check.
_EXPECTED_EMPTY = {'_RE_TENANT_FORCE', '_RE_TENANT_AUTOFILL_RETIRED'}

#: ``_RE_TENANT_POLICY``'s baseline captures ``[POLICY:...]`` inline; current reads it via
#: a tail search instead -- shape changed on purpose, so this is checked by round-tripping
#: the recovered identity, not diffing capture groups.
_IDENTITY_VIA_TAIL_SEARCH = {'_RE_TENANT_POLICY': identity_module._recorded_policy_identity}


#: Scanners that read a header's *tail* rather than a header, so the corpus comparison
#: above does not apply to them -- they have no ``HEADER_*`` template of their own.
_NOT_HEADER_SCANNERS = {
    '_RE_SQL_IDENTITY',
    '_RE_POLICY_IDENTITY',
    '_RE_FORCED',
}


def _corpus_text() -> str:
    return '\n'.join(path.read_text() for path in sorted(_CORPUS_DIR.glob('*.py')))


def test_every_header_scanner_has_a_corpus_baseline():
    """Exhaustiveness, the sibling of ``test_the_header_table_lists_every_header_the_module
    _defines``: without it a scanner added with no baseline is simply never compared against
    the real corpus, and reads as covered because the parametrised test still passes."""
    defined = {
        name
        for name in dir(headers_module)
        if name.startswith('_RE_') and name not in _NOT_HEADER_SCANNERS
    }
    assert defined == set(_BASELINE), (
        f'header scanners with no corpus baseline: {defined - set(_BASELINE)}'
    )


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
