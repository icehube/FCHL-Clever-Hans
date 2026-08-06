"""The app must not need the public internet to run.

Every panel, every Assign button, and every bid check is an htmx request. If
htmx itself is loaded from a CDN, a network blip during a multi-hour live
auction doesn't degrade the cockpit — it kills it. DaisyUI and Tailwind going
missing is milder (unstyled but usable); htmx going missing is fatal.

So the front-end dependencies are vendored under static/vendor/ and these tests
keep them that way. Vendoring is a one-time act; STAYING vendored is what needs
enforcement — one `<script src="https://...">` pasted in from a tutorial
re-opens the hole with no visible symptom until the night it matters.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATES = REPO / "templates"
VENDOR = REPO / "static" / "vendor"

# Three ways a page reaches another origin, all of them things people actually
# add: an asset tag, a CSS @import (how webfonts almost always arrive), and a
# url() inside an inline <style> — base.html has such a block, so those last two
# are not hypothetical. Checking only src=/href= would wave a webfont straight
# through, and a render-blocking @import is worse offline than a missing script.
#
# Each branch accepts `//host` as well as `https://host`: a protocol-relative
# URL is still a third-party fetch, and it's a common way to write one.
#
# The tag branch is attribute-scoped so xmlns="http://www.w3.org/2000/svg"
# (player_chart.html) is not a false positive — a namespace is an identifier,
# not a request, and matching it would make this test unpassable and invite an
# allow-list that hides real regressions. Single-slash paths like
# "/static/vendor/x.js" don't match either branch: `//` needs both slashes.
_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["']((?:https?:)?//[^"']+)["']"""
    r"""|@import\s+(?:url\(\s*)?["']?((?:https?:)?//[^"')\s;]+)"""
    r"""|url\(\s*["']?((?:https?:)?//[^"')\s]+)""",
    re.IGNORECASE,
)
_VENDORED = re.compile(r"""(?:src|href)\s*=\s*["'](/static/vendor/[^"']+)["']""")

# Narrow on purpose: a bare URL scan over JS would flag one in a comment and
# invite an allow-list. A literal cross-origin fetch is the regression worth
# catching, and shortcuts.js already only ever fetches relative paths.
_CROSS_ORIGIN_FETCH = re.compile(r"""fetch\(\s*["'](?:https?:)?//""")


def _external_urls(text: str) -> list[str]:
    """Every cross-origin reference in a chunk of markup, whatever form it takes."""
    return [g for m in _EXTERNAL.finditer(text) for g in m.groups() if g]


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _vendor_refs() -> list[str]:
    """Every /static/vendor/... path referenced by any template."""
    seen: dict[str, None] = {}
    for tpl in _templates():
        for ref in _VENDORED.findall(tpl.read_text()):
            seen[ref] = None
    return list(seen)


def test_templates_exist():
    """Guard the guard.

    parametrize over an empty list is a silent SKIP, not a failure. If the
    glob or either regex ever stops matching, the cases below evaporate and
    this module goes quiet-green while checking nothing.
    """
    assert len(_templates()) > 1, "template glob found nothing — check TEMPLATES"
    assert _vendor_refs(), "no template references static/vendor/ — nothing is vendored"

    # Every one of these bypassed the first version of _EXTERNAL, which only
    # looked at src=/href= with an explicit scheme.
    must_catch = [
        '<script src="https://unpkg.com/htmx.org@1.9.10"></script>',
        "<script src='https://unpkg.com/x.js'></script>",
        '<script src="//unpkg.com/htmx.org@1.9.10"></script>',
        '<style>@import url("https://fonts.googleapis.com/css?family=Inter");</style>',
        "<style>@import '//fonts.googleapis.com/css';</style>",
        "<style>body{background:url(https://evil.example/bg.png)}</style>",
        '<style>@font-face{src:url("https://cdn.example/f.woff2")}</style>',
        '<img src="https://example.com/tracker.gif">',
    ]
    for markup in must_catch:
        assert _external_urls(markup), f"_EXTERNAL would wave through: {markup}"

    must_ignore = [
        '<svg xmlns="http://www.w3.org/2000/svg">',  # a namespace, not a request
        '<script src="/static/vendor/htmx-1.9.10.min.js"></script>',
        '<link href="/static/style.css" rel="stylesheet">',
        "<style>background:url(data:image/gif;base64,R0lGOD)</style>",
    ]
    for markup in must_ignore:
        assert not _external_urls(markup), f"_EXTERNAL false-positives on: {markup}"


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_third_party_asset_urls(template: Path):
    """No template may pull a script, stylesheet, font or image from another origin."""
    external = _external_urls(template.read_text())
    assert not external, (
        f"{template.relative_to(REPO)} loads {external} from a third-party host. "
        f"Vendor it under static/vendor/ instead — see static/vendor/README.md."
    )


def test_app_js_makes_no_cross_origin_requests():
    """Our own JS must not reach off-origin either.

    A vendored bundle that then fetches from a CDN would defeat the whole
    exercise, and it wouldn't show up in the markup scan above.
    """
    for js in sorted((REPO / "static").glob("*.js")):  # vendor/ is a subdir, so excluded
        offending = _CROSS_ORIGIN_FETCH.findall(js.read_text())
        assert not offending, f"{js.name} fetches across origins: {offending}"


@pytest.mark.parametrize("ref", _vendor_refs())
def test_vendored_asset_is_served(ref: str):
    """Each referenced bundle resolves through the real /static mount.

    Checking the file exists on disk would miss a mount misconfiguration; going
    through the app catches the typo, the rename, and the wrong directory.
    """
    from fastapi.testclient import TestClient
    import main

    resp = TestClient(main.app).get(ref)
    assert resp.status_code == 200, f"{ref} is referenced but returns {resp.status_code}"
    # A truncated or LFS-pointer file would still 200. The smallest real bundle
    # here is htmx at ~47KB, so anything under 10KB is a broken download.
    assert len(resp.content) > 10_000, f"{ref} served only {len(resp.content)} bytes"


def test_no_orphaned_vendor_files():
    """A version bump must not leave the superseded bundle behind as dead weight."""
    assert VENDOR.is_dir(), f"{VENDOR.relative_to(REPO)} is missing entirely"
    referenced = {Path(ref).name for ref in _vendor_refs()}
    on_disk = {p.name for p in VENDOR.iterdir() if p.suffix in {".js", ".css"}}
    assert on_disk == referenced, (
        f"static/vendor/ and the templates disagree. "
        f"Unreferenced files: {sorted(on_disk - referenced)}. "
        f"Missing files: {sorted(referenced - on_disk)}."
    )


@pytest.mark.parametrize(
    "filename,marker",
    [
        ("htmx-1.9.10.min.js", "1.9.10"),
        ("tailwindcss-play-3.4.17.js", "3.4.17"),
        ("daisyui-4.12.14.trimmed.min.css", ".btn{"),
    ],
)
def test_vendored_bundle_is_the_advertised_version(filename: str, marker: str):
    """The filename claims a version; the bytes have to back it up.

    Also catches a truncated download that happens to clear the size floor —
    the marker for DaisyUI is a rule from the middle of the file.
    """
    path = VENDOR / filename
    assert path.exists(), f"{filename} is missing from static/vendor/"
    assert marker in path.read_text(errors="replace"), (
        f"{filename} does not contain {marker!r} — wrong version or bad download"
    )


# Colour utilities carry an opacity suffix: bg-error/10, border-success/30.
# DaisyUI generates ~21,600 of these (one per colour x opacity x variant x
# property) and the vendored copy keeps only the handful the app uses — see
# trim_daisyui.py. A missing one does not error, it just renders unstyled, so
# these two tests are the only thing standing between a trim and a silently
# colourless panel mid-draft.
_OPACITY_CLASS = re.compile(r"\b((?:bg|text|border|ring|outline|from|via|to|divide|shadow|fill|stroke)-[a-z0-9-]+/\d+)\b")

# Interpolated at render time, so no static scan of the templates can see the
# values. Each entry names the template and the expression that produces it.
_INTERPOLATED_COLOUR_PATTERNS = {
    # templates/partials/buyout_panel.html — verdict_class is 'error'|'success'
    # templates/partials/trade_panel.html   — verdict_class is 'success'|'error'
    "bg-{{ verdict_class }}/10",
    "border-{{ verdict_class }}/30",
}


def _daisyui_css() -> str:
    """The vendored DaisyUI stylesheet, found by glob so a rename can't stale this."""
    matches = [p for p in VENDOR.glob("daisyui-*.css")]
    assert len(matches) == 1, f"expected exactly one vendored DaisyUI file, got {matches}"
    return matches[0].read_text(errors="replace")


def _defines(css: str, cls: str) -> bool:
    """Is `cls` defined as a class selector in `css`? Handles CSS escaping."""
    escaped = re.sub(r"([/:.\[\]])", r"\\\1", cls)
    return re.search(r"\." + re.escape(escaped) + r"(?![\w\\-])", css) is not None


def test_interpolated_colour_patterns_are_declared():
    """A new `bg-{{ x }}/NN` in a template must be declared before it is trimmed in.

    The rendered scan below can only cover states a test actually reaches. This
    one is state-independent: it fails the moment a template grows an
    interpolated colour class nobody has told trim_daisyui.py about.
    """
    found = set()
    for tpl in _templates():
        found |= set(
            re.findall(
                r"((?:bg|text|border|ring|outline|from|via|to)-\{\{[^}]+\}\}/\d+)",
                tpl.read_text(),
            )
        )
    assert found == _INTERPOLATED_COLOUR_PATTERNS, (
        f"templates and the declared inventory disagree.\n"
        f"  undeclared (would be trimmed away, rendering unstyled): "
        f"{sorted(found - _INTERPOLATED_COLOUR_PATTERNS)}\n"
        f"  declared but gone from templates: "
        f"{sorted(_INTERPOLATED_COLOUR_PATTERNS - found)}\n"
        f"Update _INTERPOLATED_COLOUR_PATTERNS *and* trim_daisyui.py, then regenerate."
    )


def test_every_rendered_colour_class_is_defined():
    """Drive the real app and check what it actually emits.

    Stronger than scanning source: it sees `bg-error/10` as the interpolation
    produces it. Both branches of each verdict are exercised, because covering
    only one would leave the other's colour untested — and they are different
    classes.
    """
    from fastapi.testclient import TestClient
    import main

    css = _daisyui_css()
    with TestClient(main.app) as client:
        client.post("/reset")
        pages = [client.get("/").text]

        bot = main.auction_state.teams[main.MY_TEAM]
        verdicts: dict[str, str] = {}
        for p in (q for q in bot.all_players if q.can_be_bought_out):
            html = client.get(f"/buyout-check/{p.name}").text
            for verdict in ("BUYOUT", "KEEP"):
                if f">{verdict}</span>" in html:
                    verdicts.setdefault(verdict, html)
            if len(verdicts) == 2:
                break
        assert len(verdicts) == 2, (
            f"only reached {sorted(verdicts)} — both colour branches must render "
            f"or half the trimmed classes go unchecked"
        )
        pages.extend(verdicts.values())

    emitted = set()
    for html in pages:
        for value in re.findall(r'class="([^"]*)"', html):
            emitted |= set(_OPACITY_CLASS.findall(value))

    assert emitted, "no colour utilities rendered at all — the scan is checking nothing"
    missing = sorted(c for c in emitted if not _defines(css, c))
    assert not missing, (
        f"the app renders {missing}, which the vendored DaisyUI does not define — "
        f"those elements are unstyled. Add them to trim_daisyui.py and regenerate."
    )


def test_no_source_map_references():
    """Vendored assets must not point at source maps we don't host.

    DaisyUI's published CSS ends with `sourceMappingURL=/sm/<hash>.map` — an
    ABSOLUTE path, so once vendored it resolves against our own origin and
    404s in the uvicorn log with devtools open. That's noise in exactly the log
    you'd be watching on draft night, so the comment is stripped at vendor time
    (see static/vendor/README.md). This keeps a future re-vendor from quietly
    putting it back.
    """
    for path in sorted(VENDOR.iterdir()):
        if path.suffix not in {".js", ".css"}:
            continue
        assert "sourceMappingURL" not in path.read_text(errors="replace"), (
            f"{path.name} references a source map we don't serve — strip it"
        )
