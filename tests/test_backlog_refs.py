"""BACKLOG.md's and CHANGELOG.md's file:line references must point at the code they claim.

Line numbers drift whenever anything above them changes. On 2026-08-05 a third
of this file's references pointed at unrelated code, and the re-anchoring pass
that fixed them missed three more because it only checked references in the
`[source] file:line` position. A stale reference is worse than no reference:
it sends you somewhere wrong without saying so.

CLAUDE.md already carries the rule ("when a change shifts line numbers in a
file the backlog references, re-anchor those entries in the same commit").
This is that rule with a check behind it, so it stops depending on memory.

What this can and cannot prove: it verifies the file exists, the line is in
range, and — for a `.py` path — that the line sits inside the named function.
It CANNOT tell that the line still points at the statement the finding
describes: a reference can drift within a function and still pass. Naming the
symbol is what keeps the entry findable when that happens.

A **template** reference was checked for existence and nothing else until
2026-08-20, because an HTML file has no enclosing symbol. That is a documented
reason, not a safe one — measured that day, **six of the nine** template
references across the two files had drifted, one of them by 35 lines, and the
suite was green on every one. So a template reference carries a literal ANCHOR
in its parenthetical instead of a symbol, and the anchor has to be ON the cited
line. Stricter than the Python side, deliberately: there is no symbol span to
absorb a few lines of drift, so exact is the only thing left that means anything.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# Both, and CHANGELOG is not optional. The resolved write-ups moved there on
# 2026-08-07 — 81% of what BACKLOG.md used to hold — and this module went
# quiet-green the moment they left, still passing while checking a fraction of
# what it had. A line number in a changelog entry rots exactly like one in an
# open finding, and the changelog is what someone reads when they are lost.
DOCS = [REPO / "BACKLOG.md", REPO / "CHANGELOG.md"]

# Any `path.py:123`, optionally followed by a parenthetical — an enclosing
# function for a `.py` path, a literal anchor string for a template. Optional in
# the REGEX and required by the test, so a reference that omits it is a loud
# failure rather than an unmatched line that vanishes from the parametrization.
#
# Deliberately NOT anchored to the `[source] ` prefix — references buried in an
# entry's prose go stale exactly like the ones in the anchor position, and
# missing them is the bug this module exists to prevent.
_REF = re.compile(r"([\w./\-]+\.(?:py|html)):(\d+)(?:\s*\(([^)]+)\))?")


def _squash(text: str) -> str:
    """Collapse whitespace and drop markdown backticks, for anchor comparison.

    Both halves earn their place. Normalising whitespace is what answers the
    objection that killed this idea for three days — "pinning a snippet of the
    line's text makes the backlog fail on a whitespace edit" — since a re-indent
    or a re-wrap no longer moves the anchor. Dropping backticks lets an entry
    write the anchor as inline code, which is how every other identifier in these
    two files is written.
    """
    return re.sub(r"\s+", " ", text.replace("`", "")).strip()


def _anchor_lines(source: str, anchor: str) -> list[int]:
    """Every 1-based line whose squashed text contains the squashed anchor.

    Returns all of them rather than the first, because the failure message is the
    point: it hands you the line to re-anchor to instead of making you grep. An
    anchor need not be unique in its file — `table-scroll-x` is on three lines of
    `team_panel.html` — since the assertion is "on the cited line", not "in the
    file somewhere".
    """
    want = _squash(anchor)
    return [i for i, line in enumerate(source.splitlines(), 1) if want in _squash(line)]


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


def _references() -> list[tuple[str, str, int, str | None]]:
    """Every (doc, path, line, symbol) across both files.

    The doc name rides along so a failure says WHICH file to re-anchor — with
    two sources, "main.py:1402 is not in move_to_minors" otherwise sends you
    grepping.
    """
    return [
        (doc.name, m.group(1), int(m.group(2)), m.group(3))
        for doc in DOCS
        for m in _REF.finditer(doc.read_text())
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
    # A template anchor is a literal string, not an identifier, so it may carry
    # spaces, quotes and angle brackets. Only `)` is off limits, which is why
    # the anchors in use avoid it.
    assert _REF.findall('x/y.html:43 (hx-trigger="change") — z') == [
        ("x/y.html", "43", 'hx-trigger="change"')
    ]


def test_both_docs_are_still_being_read():
    """The other way this module can go quiet-green.

    `_REF` breaking is guarded above; `DOCS` losing an entry is not, and it is
    the likelier accident now that there are two files. **CHANGELOG.md
    contributes zero references today** — resolved write-ups cite commits, not
    line numbers — so nothing in the parametrized cases would notice if it were
    dropped from the list. Verified by planting a stale ref in it: with the file
    listed the suite fails naming CHANGELOG.md, without it the suite passes.

    Checked on existence and size rather than on a reference count, because a
    count would make this fail the day the changelog happens to cite no lines,
    which is its normal state.
    """
    for doc in DOCS:
        assert doc.exists(), f"{doc.name} is in DOCS but not on disk"
        assert len(doc.read_text()) > 500, f"{doc.name} is suspiciously empty"
    assert {d.name for d in DOCS} == {"BACKLOG.md", "CHANGELOG.md"}, (
        f"DOCS is {[d.name for d in DOCS]} — open work lives in BACKLOG.md and "
        f"resolved work in CHANGELOG.md, and references in either one rot"
    )


@pytest.mark.parametrize("doc,path,line,symbol", _references(), ids=lambda v: str(v))
def test_reference_resolves(doc: str, path: str, line: int, symbol: str | None):
    resolved = _resolve(path)
    assert resolved is not None, f"{doc} cites {path}, which no longer exists"

    source = resolved.read_text()
    total = len(source.splitlines())
    assert line <= total, f"{doc}: {path}:{line} is past end of file ({total} lines)"

    if not path.endswith(".py"):
        # No enclosing symbol, so the parenthetical is a literal anchor instead:
        # a distinctive string that must appear on the cited line.
        assert symbol, (
            f"{doc}: {path}:{line} needs an anchor in parentheses — a distinctive "
            f"string from the line itself, e.g. {path}:{line} (table-scroll-x). "
            f"A template has no symbol to anchor to, and without one this "
            f"reference is checked for existence and nothing else"
        )
        # `_squash("   ")` and `_squash("``")` are both "", and `"" in line` is
        # true for every line — so a blank anchor made the check below vacuous
        # while `assert symbol` above waved it through, "   " being truthy. That
        # is this rule's own failure mode, so it gets its own assertion.
        assert _squash(symbol), (
            f"{doc}: {path}:{line} anchors on ({symbol!r}), which is nothing once "
            f"whitespace and backticks are stripped — an empty anchor matches "
            f"every line in the file, so it would pass against any of them"
        )
        hits = _anchor_lines(source, symbol)
        assert line in hits, (
            f"{doc}: {path}:{line} anchors on ({symbol}), which is on "
            f"{hits or 'no line in the file'} — re-anchor the entry"
        )
        return

    # "a / b" means the finding spans both; the line need only be in one.
    names = [s.strip() for s in (symbol or "").split("/")]
    # A Python reference MUST name its enclosing function. Without one, only
    # the in-range check above applies — and every reference this module was
    # written to catch was in range, just pointing at the wrong code. A prose
    # parenthetical would silently opt out of the check that matters.
    assert symbol and all(n.isidentifier() for n in names), (
        f"{doc}: {path}:{line} needs its enclosing function in parentheses, e.g. "
        f"{path}:{line} (some_function). Got: {symbol!r}"
    )

    ranges = _symbol_ranges(source)
    known = [n for n in names if n in ranges]
    assert known, f"{doc}: {path}:{line} names ({symbol}), which no longer exists in the file"

    assert any(
        start <= line <= end
        for n in known
        for start, end in ranges[n]
    ), (
        f"{doc}: {path}:{line} claims to be in ({symbol}) but that symbol spans "
        f"{[ranges[n] for n in known]} — re-anchor the entry"
    )


def _one_true_reference() -> tuple[str, int, str]:
    """A (path, line, anchor) triple that is correct RIGHT NOW, derived not written.

    A hardcoded line number here would rot exactly like the ones this module
    exists to catch, and a self-test that has quietly stopped describing the
    file is the failure mode being guarded against. So the triple is read off
    the template: the first line long enough to be distinctive whose squashed
    text appears on exactly ONE line. Uniqueness is what makes the off-by-one
    case below a real discriminator — an anchor sitting on two adjacent lines
    would pass a wrong citation.
    """
    path = "templates/partials/bid_limits.html"
    assert (REPO / path).exists(), (
        f"{path} is gone, so this self-test has nothing to build on — point it at "
        f"another template rather than deleting it"
    )
    source = (REPO / path).read_text()
    lines = source.splitlines()
    for i, text in enumerate(lines, 1):
        anchor = _squash(text)
        if len(anchor) > 20 and i < len(lines) and _anchor_lines(source, anchor) == [i]:
            return path, i, anchor
    raise AssertionError(f"no unique line in {path} to build the self-test on")


def test_the_anchor_rule_can_actually_fail():
    """The template half of `test_reference_resolves`, exercised on purpose.

    The parametrized cases run over LIVE references, which are all correct once
    a re-anchoring pass lands — so deleting the anchor check entirely leaves the
    suite green, which is the "reads as coverage and isn't" shape this whole
    module is about. Until 2026-08-20 the template branch was a bare `return`
    and nothing here would have noticed it coming back.

    Three assertions, because they die to different mutants: the true reference
    must PASS (a check that rejects everything is not a check), a citation one
    line off must fail AND name the real line (that message is the re-anchor),
    and a missing parenthetical must fail (the rule that stops the next entry
    opting out).
    """
    path, line, anchor = _one_true_reference()

    test_reference_resolves("self-test", path, line, anchor)

    with pytest.raises(AssertionError, match=rf"\[{line}\]"):
        test_reference_resolves("self-test", path, line + 1, anchor)

    with pytest.raises(AssertionError, match="needs an anchor"):
        test_reference_resolves("self-test", path, line, None)

    # A parenthetical that squashes to nothing is the rule's own blind spot:
    # `(   )` and `` (``) `` both parse as a symbol, both pass `assert symbol`
    # because a space is truthy, and both then match every line in the file.
    for blank in ("   ", "``", " ` ` "):
        with pytest.raises(AssertionError, match="is nothing once"):
            test_reference_resolves("self-test", path, line, blank)
