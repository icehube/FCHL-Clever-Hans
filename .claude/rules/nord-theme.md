---
paths:
  - "static/**"
  - "templates/**"
---

# Nord Theme Color System

The UI uses the [Nord](https://www.nordtheme.com/) color palette. All colors are CSS custom properties in `static/style.css`. When adding or modifying UI elements, use the correct variable -- never hardcode hex values.

## CSS variables

| Variable | Nord token | Hex | Use for |
|---|---|---|---|
| `--bg` | nord0 | `#2e3440` | Page background, recessed input fields |
| `--bg-panel` | nord1 | `#3b4252` | Panel/card backgrounds, sticky headers |
| `--bg-hover` | nord2 | `#434c5e` | Hover states, active selections |
| `--text` | nord6 | `#eceff4` | Primary body text (brightest Snow Storm) |
| `--text-muted` | nord4 | `#d8dee9` | Secondary text, labels, timestamps |
| `--accent` | nord8 | `#88c0d0` | Headings, brand highlights, primary buttons |
| `--accent-secondary` | nord12 | `#d08770` | Warm emphasis (sparingly) |
| `--green` | nord14 | `#a3be8c` | Success: BID, trade accept, optimal roster |
| `--red` | nord11 | `#bf616a` | Danger: DROP, trade decline, errors |
| `--yellow` | nord13 | `#ebcb8b` | Warning: CAUTION, RFA markers, buyouts |
| `--blue` | nord9 | `#81a1c1` | Links, secondary buttons, informational |
| `--border` | nord3 | `#4c566a` | Panel borders, table dividers, separators |
| `--input-bg` | nord0 | `#2e3440` | Form input backgrounds (same as base) |

## Button text contrast (WCAG AA)

Nord's Frost and Aurora colors are pastel -- white text fails WCAG AA on most of them.

- Buttons on `--accent`, `--blue`, `--green`, `--yellow`, `--text-muted`: use `color: var(--bg)` (dark text)
- Buttons on `--red` only: use `color: #fff` (white text)
- Primary button hover: `#8fbcbb` (nord7, a sister Frost color)

## Tinted backgrounds for semantic states

For colored row/card backgrounds (bid results, trade outcomes), use the aurora color at low opacity:

```css
background: rgba(163, 190, 140, 0.1);  /* green tint -- success */
background: rgba(191, 97, 106, 0.15);  /* red tint -- danger */
background: rgba(235, 203, 139, 0.1);  /* yellow tint -- warning */
background: rgba(129, 161, 193, 0.08); /* blue tint -- informational */
background: rgba(136, 192, 208, 0.1);  /* accent tint -- highlight */
```

## New UI element checklist

1. Use CSS variables, not hex codes
2. Check text contrast: light backgrounds (`--accent`, `--green`, etc.) need dark text (`var(--bg)`)
3. Tinted backgrounds: use rgba at 0.08-0.15 opacity, not solid colors
4. Borders: use `var(--border)` consistently
5. Sticky headers: set `background: var(--bg-panel)` so content doesn't show through
