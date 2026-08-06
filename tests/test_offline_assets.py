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

# Scoped to src=/href= rather than any http:// in the file, so
# xmlns="http://www.w3.org/2000/svg" (player_chart.html) isn't a false positive:
# a namespace declaration is an identifier, not a request.
_EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](https?://[^"']+)["']""")
_VENDORED = re.compile(r"""(?:src|href)\s*=\s*["'](/static/vendor/[^"']+)["']""")


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
    assert _EXTERNAL.findall('<script src="https://unpkg.com/htmx.org@1.9.10">') == [
        "https://unpkg.com/htmx.org@1.9.10"
    ]
    assert _EXTERNAL.findall('<svg xmlns="http://www.w3.org/2000/svg">') == [], (
        "xmlns is a namespace, not a request — matching it would make this "
        "test unpassable and invite an allow-list that hides real regressions"
    )
    assert _vendor_refs(), "no template references static/vendor/ — nothing is vendored"


@pytest.mark.parametrize("template", _templates(), ids=lambda p: p.name)
def test_no_third_party_asset_urls(template: Path):
    """No template may load a script or stylesheet from another origin."""
    external = _EXTERNAL.findall(template.read_text())
    assert not external, (
        f"{template.relative_to(REPO)} loads {external} from a third-party host. "
        f"Vendor it under static/vendor/ instead — see static/vendor/README.md."
    )


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
        ("daisyui-4.12.14.full.min.css", ".btn{"),
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
