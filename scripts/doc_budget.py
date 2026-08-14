"""Enforce the doc-budget caps agreed in the 2.0.3 shrink pass: a docstring or
comment block that outgrew its cap should have been trimmed or relocated
already, and a markdown file over its cap should have been split or cut.

    uv run python scripts/doc_budget.py

Scope is `src/` and `tests/` for Python prose, plus root-level markdown and
`docs/**/*.md` -- the surfaces the shrink pass actually covered. `core/`,
`noxfile.py`, `manage.py`, and `scripts/*.md` sit outside that scope and are
not checked here.

Every `**/migrations/*.py` file is skipped entirely: its comment headers are
either the frozen enforcement interface (`HEADER_*` in
`guitars.management.enforcement.headers`, guarded by
`tests/test_enforcement_identity.py` and `tests/test_header_corpus.py`) or a
fixture modelling one -- rewriting either for a line count would falsify what
it exists to prove.

Two markdown files (`CONTEXT.md`, `docs/api-reference.md`) are enumerations
whose length tracks the API surface, not verbosity; they opt out of the
markdown cap with an in-file marker:

    <!-- doc-budget: exempt — <reason> -->

Axis 5 (dangling references) only checks what the shrink pass actually had to
protect: `docs/*.md` path references from source, and the three section
citations quoted as `"Title" section` near such a path. It is a targeted
check, not a general prose parser -- new citation shapes need a matching new
check, not a wider regex.
"""

from __future__ import annotations

import ast
import re
import sys
import tokenize
from pathlib import Path


DOCSTRING_CAP = 3
COMMENT_BLOCK_CAP = 3
MARKDOWN_CAP = 100

REPO_ROOT = Path(__file__).resolve().parent.parent
PY_ROOTS = ('src', 'tests')
MARKDOWN_ROOT_FILES = ('README.md', 'CLAUDE.md', 'CONTEXT.md', 'CHANGELOG.md')

EXEMPT_RE = re.compile(r'<!--\s*doc-budget:\s*exempt\s*(?:—|--)\s*(.*?)\s*-->')
DOC_PATH_RE = re.compile(r'docs/([\w][\w\-]*\.md)')
SECTION_CITE_RE = re.compile(r'"([A-Z][^"\n]{2,60})"\s+section')
HEADING_RE = re.compile(r'^#+\s*(.+?)\s*$')


def _out(msg: str) -> None:
    sys.stdout.write(f'{msg}\n')


def _iter_py_files():
    for root in PY_ROOTS:
        for path in (REPO_ROOT / root).rglob('*.py'):
            if 'migrations' in path.relative_to(REPO_ROOT).parts:
                continue
            yield path


def _iter_markdown_files():
    for name in MARKDOWN_ROOT_FILES:
        path = REPO_ROOT / name
        if path.exists():
            yield path
    yield from sorted((REPO_ROOT / 'docs').rglob('*.md'))


def _docstring_violations(path: Path, tree: ast.Module) -> list[str]:
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        body = node.body[0]
        span = (body.end_lineno or body.lineno) - body.lineno + 1
        if span > DOCSTRING_CAP:
            name = getattr(node, 'name', '<module>')
            violations.append(
                f'{path}:{body.lineno}  docstring on {name!r} is {span} lines (cap {DOCSTRING_CAP})'
            )
    return violations


def _comment_block_violations(path: Path, source: str) -> list[str]:
    lines = source.splitlines()
    violations = []
    run_start = None
    run_len = 0
    tokens = tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__)
    prev_line = -2
    try:
        for tok in tokens:
            if tok.type != tokenize.COMMENT:
                continue
            row, col = tok.start
            standalone = lines[row - 1][:col].strip() == ''
            if not standalone:
                continue
            if row == prev_line + 1:
                run_len += 1
            else:
                if run_len > COMMENT_BLOCK_CAP:
                    violations.append(
                        f'{path}:{run_start}  comment block is {run_len} lines (cap {COMMENT_BLOCK_CAP})'
                    )
                run_start = row
                run_len = 1
            prev_line = row
    except tokenize.TokenError:
        pass
    if run_len > COMMENT_BLOCK_CAP:
        violations.append(
            f'{path}:{run_start}  comment block is {run_len} lines (cap {COMMENT_BLOCK_CAP})'
        )
    return violations


def _markdown_violations(path: Path, text: str) -> list[str]:
    lines = text.splitlines()
    if len(lines) <= MARKDOWN_CAP:
        return []
    match = EXEMPT_RE.search(text)
    if match is None:
        return [
            f'{path}  is {len(lines)} lines (cap {MARKDOWN_CAP}), no doc-budget exemption marker'
        ]
    if not match.group(1):
        return [f'{path}  doc-budget exemption marker has an empty reason']
    return []


def _reference_violations() -> list[str]:
    violations = []
    for path in _iter_py_files():
        text = path.read_text()
        for m in DOC_PATH_RE.finditer(text):
            target = REPO_ROOT / 'docs' / m.group(1)
            if not target.exists():
                violations.append(f'{path}  references missing docs/{m.group(1)}')
        for m in SECTION_CITE_RE.finditer(text):
            window = text[max(0, m.start() - 200) : m.end() + 200]
            doc_match = DOC_PATH_RE.search(window)
            if doc_match is None:
                continue
            target = REPO_ROOT / 'docs' / doc_match.group(1)
            if not target.exists():
                continue
            title = m.group(1)
            headings = {
                h.group(1).strip('*').strip()
                for line in target.read_text().splitlines()
                if (h := HEADING_RE.match(line))
            }
            if title not in headings:
                violations.append(
                    f'{path}  cites "{title}" section of docs/{doc_match.group(1)}, '
                    f'no such heading found there'
                )
    return violations


def main() -> int:
    violations: list[str] = []

    for path in _iter_py_files():
        source = path.read_text()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f'{path}  failed to parse: {exc}')
            continue
        violations += _docstring_violations(path, tree)
        violations += _comment_block_violations(path, source)

    for path in _iter_markdown_files():
        violations += _markdown_violations(path, path.read_text())

    violations += _reference_violations()

    if violations:
        _out(f'doc-budget: {len(violations)} violation(s)\n')
        for v in sorted(violations):
            _out(f'  {v}')
        return 1

    _out('doc-budget: clean')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
