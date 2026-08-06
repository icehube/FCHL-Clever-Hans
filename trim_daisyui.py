"""Regenerate the vendored DaisyUI stylesheet, dropping colour utilities we never use.

DaisyUI's published `full.min.css` is 2.93 MB, and 84% of that (21,588 of its
24,181 rules) is a generated matrix of opacity-suffixed colour utilities —
`.focus\\:outline-error-content\\/95`, `.via-base-300\\/45`, one rule per colour
x opacity step x variant x property. This app uses five of them.

Everything else is copied through byte-for-byte: every component, the base
layer, all the stock themes. Only the enumerable family is touched, because
that is the only part we can prove is unused. The result is ~467 KB.

Not `dist/styled.min.css`, which looks like the obvious fix and is not: it
defines 609 classes to full's 24,940 and is missing 19 the app relies on,
including btn-sm, badge-xs, table-xs, tooltip and text-warning.

Usage:
    python3 trim_daisyui.py <upstream-full.min.css> [output.css]

Re-download upstream from the URL recorded in static/vendor/README.md. A
missing utility renders unstyled rather than erroring, so
tests/test_offline_assets.py checks the running app's output against whatever
this produces — run the suite after regenerating.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
DEFAULT_OUT = REPO / "static" / "vendor" / "daisyui-4.12.14.trimmed.min.css"

# Built by Jinja at render time, so the template scan below cannot see them.
# Each is named with the template and expression that produces it; keep in step
# with _INTERPOLATED_COLOUR_PATTERNS in tests/test_offline_assets.py.
DYNAMIC_CLASSES = {
    # partials/buyout_panel.html: bg-{{ verdict_class }}/10   (error | success)
    # partials/trade_panel.html:  bg-{{ verdict_class }}/10   (success | error)
    "bg-error/10",
    "bg-success/10",
    # ...and the matching border-{{ verdict_class }}/30 on the same elements
    "border-error/30",
    "border-success/30",
}

_OPACITY_SUFFIX = re.compile(r"\\/\d+")
_CLASS_IN_SELECTOR = re.compile(r"\.((?:[\w-]|\\.)+)")
_LITERAL_CLASS = re.compile(
    r"\b((?:bg|text|border|ring|outline|from|via|to|divide|shadow|fill|stroke)"
    r"-[a-z0-9-]+/\d+)\b"
)


def used_classes(repo: Path = REPO) -> set[str]:
    """Every opacity-suffixed colour class the app can emit."""
    found = set(DYNAMIC_CLASSES)
    for tpl in sorted((repo / "templates").rglob("*.html")):
        found |= set(_LITERAL_CLASS.findall(tpl.read_text()))
    for js in sorted((repo / "static").glob("*.js")):
        found |= set(_LITERAL_CLASS.findall(js.read_text()))
    return found


def split_rules(css: str) -> list[str]:
    """Top-level rules, in order, each including whatever preceded it.

    Depth-tracked so @media/@supports blocks stay whole — splitting on '}'
    would cut them in half and produce a stylesheet that parses to garbage.
    """
    rules, depth, start = [], 0, 0
    for i, ch in enumerate(css):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                rules.append(css[start : i + 1])
                start = i + 1
    if css[start:].strip():
        rules.append(css[start:])
    return rules


def _selector_classes(selector: str) -> set[str]:
    """Class names in a selector, unescaped, with any variant prefix stripped.

    `.hover\\:bg-error\\/10:hover` yields both `hover:bg-error/10` and
    `bg-error/10`, so a variant form of a class we use is kept too. It costs a
    handful of rules and means writing `hover:bg-error/10` later still works.
    """
    out = set()
    for raw in _CLASS_IN_SELECTOR.findall(selector):
        name = re.sub(r"\\(.)", r"\1", raw)
        out.add(name)
        out.add(name.rsplit(":", 1)[-1])
    return out


def trim(css: str, keep: set[str]) -> tuple[str, int]:
    """Return the trimmed stylesheet and how many rules were dropped."""
    kept, dropped = [], 0
    for rule in split_rules(css):
        selector = rule.split("{", 1)[0]
        if _OPACITY_SUFFIX.search(selector) and not (_selector_classes(selector) & keep):
            dropped += 1
            continue
        kept.append(rule)
    return "".join(kept), dropped


def main(argv: list[str]) -> int:
    if not 2 <= len(argv) <= 3:
        print(__doc__)
        return 2
    source = Path(argv[1])
    out = Path(argv[2]) if len(argv) == 3 else DEFAULT_OUT

    css = source.read_text(errors="replace")
    # Upstream ends with `/*# sourceMappingURL=/sm/....map */`, an ABSOLUTE path
    # that would resolve against our own origin and 404 in the uvicorn log.
    css = re.sub(r"\n?/\*# sourceMappingURL=[^*]*\*/\s*$", "", css)

    keep = used_classes()
    trimmed, dropped = trim(css, keep)

    out.write_text(trimmed)
    print(f"kept {len(keep)} colour utilities: {', '.join(sorted(keep))}")
    print(f"dropped {dropped:,} unused rules")
    print(f"{len(css):,} -> {len(trimmed):,} bytes "
          f"({(1 - len(trimmed) / len(css)) * 100:.1f}% smaller)  ->  {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
