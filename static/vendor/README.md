# Vendored front-end assets

These three files were loaded from public CDNs until 2026-08-06. They are now
committed here and served from `/static/vendor/` by the mount in `main.py`.

**Why:** every panel, every bid check, and every Assign button in this app is an
htmx request. A CDN outage or a flaky hotel wifi connection during a multi-hour
live auction wouldn't leave the cockpit ugly — it would leave it dead. Vendoring
also *pins* Tailwind, which was previously loaded from the unversioned
`cdn.tailwindcss.com` and could change under us with no commit and no warning.

`tests/test_offline_assets.py` enforces that no template goes back to a CDN.

## Sources

| File | Source URL | Upstream sha256 |
|---|---|---|
| `htmx-1.9.10.min.js` | `https://unpkg.com/htmx.org@1.9.10` | `b3bdcf5c741897a53648b1207fff0469a0d61901429ba1f6e88f98ebd84e669e` |
| `daisyui-4.12.14.full.min.css` | `https://cdn.jsdelivr.net/npm/daisyui@4.12.14/dist/full.min.css` | `bf619937eca81b323ca601ab7347443a3c4c8b6ad3306bc9908ef127d207d0b6` |
| `tailwindcss-play-3.4.17.js` | `https://cdn.tailwindcss.com` (Play CDN, resolved to v3.4.17) | `176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15` |

htmx and Tailwind are committed byte-for-byte as downloaded.

## The one modification

DaisyUI's published CSS ends with:

```
/*# sourceMappingURL=/sm/8db1e6…c679f770.map */
```

That is an **absolute** path, so once the file is served from our own origin it
resolves to `http://localhost:8000/sm/…map` and 404s in the uvicorn log whenever
devtools is open — noise in exactly the log you'd be watching on draft night.
It is stripped. The vendored file is a byte-exact prefix of upstream; those 96
bytes are the only difference.

```bash
sed '/^\/\*# sourceMappingURL=/d' full.min.css > daisyui-4.12.14.full.min.css
```

As-vendored sha256: `08e190900e770fae650e3bb05c818598c4ee4c10d0f5dde25978387c9acd59f7`

## Why the Tailwind *Play* CDN and not a real build

The Play bundle compiles utility classes in the browser. Tailwind's docs call it
dev-only — that warning is about public production sites (payload, flash of
unstyled content), not a single-user localhost tool.

A real Tailwind build would ship far less CSS, but it needs node + npm + the
daisyui plugin and a rebuild on every template edit, and it is *riskier here*: a
static build resolves classes by scanning source, while `static/shortcuts.js`
builds class names at runtime (`'alert-' + type`). That case survives only
because DaisyUI's prebuilt CSS carries every `alert-*` variant. Any future
runtime-built utility would silently lose its styling — and would surface on
draft night, not in a test.

The bundle also runs a `MutationObserver` over `document.documentElement` with
`childList` + `subtree`, which is *why* it suits this app specifically: every
panel arrives as an htmx swap, and the observer restyles each one as it lands.

Verified before vendoring: the Play bundle contains no `fetch(`,
`XMLHttpRequest`, `importScripts`, or `WebSocket`. It needs no network. Its lone
`require(` is esbuild's shim for an unreachable node path — it throws if called.

### The console warning is expected

On every page load the bundle prints:

> cdn.tailwindcss.com should not be used in production. To use Tailwind CSS in
> production, install it as a PostCSS plugin or use the Tailwind CLI…

That is baked into the bundle and has been printing all along, from the CDN too.
Nothing is misconfigured — see the section above for why we accept it. Noted
here because a vendored file feels like ours, so the warning now invites a
"did I break something?" mid-draft. You did not.

## Do not add a Content-Security-Policy without reading this

The Assign form in `templates/partials/auction_control.html` uses
`hx-vals='js:{...}'`, which relies on htmx's eval path (`htmx.config.allowEval`,
default on). A CSP without `unsafe-eval` would silently break the button that
records every sale. "We're serving our own assets now" is exactly the moment
someone reaches for a CSP, so it is called out here.

## Upgrading

1. Download the new version; keep the version in the filename.
2. Update `templates/base.html` and the table above (including checksums).
3. Delete the superseded file — `test_no_orphaned_vendor_files` will fail otherwise.
4. Run `.venv/bin/pytest tests/test_offline_assets.py`, then load the app with
   devtools set to **Offline** and confirm it still works.
