"""BACKLOG.md's file:line references must point at the code they claim.

Line numbers drift whenever anything above them changes. On 2026-08-05 a third
of this file's references pointed at unrelated code, and the re-anchoring pass
that fixed them missed three more because it only checked references in the
`[source] file:line` position. A stale reference is worse than no reference:
it sends you somewhere wrong without saying so.

CLAUDE.md already carries the rule ("when a change shifts line numbers in a
file the backlog references, re-anchor those entries in the same commit").
This is that rule with a check behind it, so it stops depending on memory.

What this can and cannot prove: it verifies the file exists, the line is in
range, and the line sits inside the named function. It CANNOT tell that the
line still points at the statement the finding describes — a reference can
drift within a function and still pass. Naming the symbol is what keeps the
entry findable when that happens.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BACKLOG = REPO / "BACKLOG.md"

# Any `path.py:123`, optionally followed by `(symbol)`. Deliberately NOT
# anchored to the `[source] ` prefix — references buried in an entry's prose
# go stale exactly like the ones in the anchor position, and missing them is
# the bug this module exists to prevent.
_REF = re.compile(r"([\w./\-]+\.(?:py|html)):(\d+)(?:\s*\(([^)]+)\))?")


def _resolve(path: str) -> Path | None:
    """Repo-relative path, or a unique basename match under the repo."""
    direct = REPO / path
    if direct.exists():
        return direct
    matches = [
        p for p in REPO.rglob(Path(path).name)
        if ".venv" not in p.parts and ".git" not in p.parts
    ]
    return matches[0] if len(matches) == 1 else None


def _symbol_ranges(source: str) -> dict[str, list[tuple[int, int]]]:
    """Line span of every def in the file, keyed by name.

    Spans start at the first DECORATOR, not the `def`. An entry may
    legitimately anchor to a decorator line — tests/test_endpoints.py:11 points
    at `@pytest.fixture(scope="module")` because the scope IS the finding — and
    treating that as module-level would fail a correct reference.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        ranges.setdefault(node.name, []).append((start, node.end_lineno or node.lineno))
    return ranges


def _references() -> list[tuple[str, int, str | None]]:
    return [
        (m.group(1), int(m.group(2)), m.group(3))
        for m in _REF.finditer(BACKLOG.read_text())
    ]


def test_reference_pattern_still_matches():
    """Guard the guard.

    parametrize over an empty list is a silent SKIP, not a failure — verified.
    So if _REF ever stops matching, every case below evaporates and the suite
    goes quiet-green while checking nothing.

    Asserted against a literal sample rather than a count of live references:
    a threshold like "at least 10" would start failing as findings get FIXED,
    punishing exactly the progress this file is meant to track.
    """
    sample = "- [2026-01-01] [grill] state.py:246 (add_acquired_player) — x — y"
    assert _REF.findall(sample) == [("state.py", "246", "add_acquired_player")]
    assert _REF.findall("templates/base.html:8 — no symbol here") == [
        ("templates/base.html", "8", "")
    ]


@pytest.mark.parametrize("path,line,symbol", _references(), ids=lambda v: str(v))
def test_reference_resolves(path: str, line: int, symbol: str | None):
    resolved = _resolve(path)
    assert resolved is not None, f"BACKLOG cites {path}, which no longer exists"

    source = resolved.read_text()
    total = len(source.splitlines())
    assert line <= total, f"{path}:{line} is past end of file ({total} lines)"

    if not path.endswith(".py"):
        return  # templates have no symbols to anchor to

    # "a / b" means the finding spans both; the line need only be in one.
    names = [s.strip() for s in (symbol or "").split("/")]
    # A Python reference MUST name its enclosing function. Without one, only
    # the in-range check above applies — and every reference this module was
    # written to catch was in range, just pointing at the wrong code. A prose
    # parenthetical would silently opt out of the check that matters.
    assert symbol and all(n.isidentifier() for n in names), (
        f"{path}:{line} needs its enclosing function in parentheses, e.g. "
        f"{path}:{line} (some_function). Got: {symbol!r}"
    )

    ranges = _symbol_ranges(source)
    known = [n for n in names if n in ranges]
    assert known, f"{path}:{line} names ({symbol}), which no longer exists in the file"

    assert any(
        start <= line <= end
        for n in known
        for start, end in ranges[n]
    ), (
        f"{path}:{line} claims to be in ({symbol}) but that symbol spans "
        f"{[ranges[n] for n in known]} — re-anchor the entry"
    )
