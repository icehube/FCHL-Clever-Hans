---
paths:
  - "static/**"
  - "templates/**"
---

# Tokyo Night Theme Color System

The UI uses the [Tokyo Night](https://github.com/tokyo-night/tokyo-night-vscode-theme) color palette. All colors are CSS custom properties in `static/style.css`. When adding or modifying UI elements, use the correct variable -- never hardcode hex values.

## CSS variables

| Variable | TN role | Hex | Use for |
|---|---|---|---|
| `--bg` | editor bg | `#1a1b26` | Page background |
| `--bg-panel` | storm bg | `#24283b` | Panel/card backgrounds, sticky headers |
| `--bg-hover` | selection | `#292e42` | Hover states, active selections |
| `--text` | variables | `#c0caf5` | Primary body text (brightest) |
| `--text-muted` | foreground | `#a9b1d6` | Secondary text, labels, timestamps |
| `--accent` | functions | `#7aa2f7` | Headings, brand highlights, primary buttons |
| `--accent-secondary` | numbers | `#ff9e64` | Warm emphasis (sparingly) |
| `--green` | strings | `#9ece6a` | Success: BID, trade accept, optimal roster |
| `--red` | error | `#f7768e` | Danger: DROP, trade decline, errors |
| `--yellow` | parameters | `#e0af68` | Warning: CAUTION, RFA markers, buyouts |
| `--blue` | tokens/cyan | `#7dcfff` | Links, secondary buttons, informational |
| `--border` | divider | `#414868` | Panel borders, table dividers, separators |
| `--input-bg` | sidebar bg | `#16161e` | Form input backgrounds (recessed, darker than page) |

## Button text contrast (WCAG AA)

Tokyo Night's accents are pastel-bright -- white text fails WCAG AA on most of them.

- Buttons on `--accent`, `--blue`, `--green`, `--yellow`, `--red`, `--text-muted`: use `color: var(--bg)` (dark text). TN's red `#f7768e` is light enough that dark text reads cleanly on it.
- Primary button hover: bump opacity or use `--accent-secondary` (orange) for warm emphasis.

## Tinted backgrounds for semantic states

For colored row/card backgrounds (bid results, trade outcomes), use the accent color at low opacity:

```css
background: rgba(158, 206, 106, 0.10);  /* green tint -- success */
background: rgba(247, 118, 142, 0.15);  /* red tint -- danger */
background: rgba(224, 175, 104, 0.10);  /* yellow tint -- warning */
background: rgba(125, 207, 255, 0.08);  /* cyan tint -- informational */
background: rgba(122, 162, 247, 0.10);  /* accent tint -- highlight */
```

## SVG colors

Inline SVG `fill` and `stroke` attributes accept CSS custom properties via `var(--name)`. Use them so theme swaps stay one-touch:

```html
<rect fill="var(--green)" opacity="0.18"/>
<line stroke="var(--text-muted)"/>
<text fill="var(--yellow)">Median</text>
```

## New UI element checklist

1. Use CSS variables, not hex codes (including in inline SVG)
2. Check text contrast: light backgrounds (`--accent`, `--green`, `--red`, `--yellow`) need dark text (`var(--bg)`)
3. Tinted backgrounds: use rgba at 0.08-0.15 opacity, not solid colors
4. Borders: use `var(--border)` consistently
5. Sticky headers: set `background: var(--bg-panel)` so content doesn't show through
