# Appearance

[Documentation](README.md) | [MOSAIC](../README.md)

The administrator console and the end-user portal share one look: a compact navigation rail, a
frosted top bar, and translucent panels over a backdrop washed with an accent color. This page
covers the four appearance modes, how an administrator sets colors for everyone, how a person
sets their own, and what MOSAIC stores.

The [gallery](gallery.md#light-and-dark) shows each mode.

## Modes

Each person chooses a mode from the appearance menu at the foot of the navigation rail. The
console also offers it under **Settings > Appearance**.

| Mode | What it shows |
| --- | --- |
| **Light** | The organization's light colors, or MOSAIC's own if none are saved |
| **Dark** | The organization's dark colors, or MOSAIC's own if none are saved |
| **System** | Light or Dark, following the browser and operating system. This is the default |
| **Custom** | The person's own colors. Until they have saved some, choosing it opens the editor. Hidden when an administrator has turned custom colors off |

The choice is kept in the browser's local storage, so it applies per browser profile and per app.
The console and the portal are separate sites and keep separate choices.

## A palette is six colors

| Color | Used for |
| --- | --- |
| Accent | Primary buttons, the active navigation item, counters, and the backdrop wash |
| Page background | The page behind every panel |
| Panels | Cards, navigation panels, dialogs, and menus |
| Borders | Outlines and dividers |
| Text | Headings and body text |
| Secondary text | Descriptions and labels |

MOSAIC derives everything else. Two derived colors matter most:

- **Text on the accent** is black or white, whichever reads better. A pale accent gets dark
  button text and a deep one gets light text.
- **The accent as text**, for links and selected items, is moved toward black or white only as
  far as it takes to read on a panel. A lime accent still fills buttons as lime, and its links
  are a darker green.

Status colors for success, warning, and danger are not part of a palette. They keep their
meaning under every palette and are adjusted only enough to stay readable on the chosen panel
color. Graphs and usage bars are drawn in the text color, with the accent marking the latest
point.

### Readability is enforced

A palette cannot be saved while any of these pairs has a contrast below 4.5:1, the WCAG AA level
for body text:

- Text on panels
- Secondary text on panels
- Text on the page background
- Secondary text on the page background

The editors show each pair's ratio as you type and a small preview painted with the draft. The
API applies the same rule to organization colors, so a request that bypasses the console is
refused with the failing pair named.

## Organization colors

An administrator sets these in the console under **Settings > Appearance > Organization colors**.

1. Turn on **Use our own colors instead of MOSAIC's**.
2. Edit the **Light mode colors** and the **Dark mode colors**. Each needs all six colors.
3. Decide whether to **Let people choose their own custom colors**.
4. Select **Save organization colors**.

The administrator sees the result at once. Everyone else gets it the next time their console or
portal loads; each app also keeps the last colors it received, so later visits paint in them
before the API answers. Turning the first switch off and saving returns everyone to MOSAIC's own
colors.

Turning custom colors off does not delete anyone's saved palette. People who had chosen Custom
see System until the setting is turned back on.

Organization colors apply to the whole tenant. There is no per-environment or per-cost-center
scheme, and no logo or banner setting.

## Your own colors

- **Console:** **Settings > Appearance > Your custom colors**.
- **Portal:** open the account menu at the foot of the navigation rail and choose
  **Custom colors**, or choose **Customize colors** in the appearance menu.

Choose **Light** or **Dark** under **Start from** to begin from the organization's colors for
that mode, change any of the six, then select **Save and use my colors**. **Remove my colors**
deletes the palette and returns to System.

A custom palette is stored only in that browser. It is not sent to MOSAIC, does not follow you to
another device, and is separate for the console and the portal.

## What is stored, and where

| What | Where | Who can change it |
| --- | --- | --- |
| Organization colors and the custom-colors switch | One `appearanceSettings` document per tenant in the existing `desired-state` Cosmos container | `Admin` role |
| A person's mode and custom palette | That browser's local storage | That person |

Saving organization colors writes an `appearance.settingsUpdated` audit event that records who
saved and whether a custom scheme and custom colors were on. It does not record the colors.

The feature adds no container, app setting, or permission, so an existing deployment needs no
infrastructure change. Until an administrator saves a scheme, the API reports none and both apps
use MOSAIC's own colors.

### API

| Method and path | Role | Purpose |
| --- | --- | --- |
| `GET /api/v1/portal/appearance` | `User` or `Admin` | The colors both apps paint with |
| `GET /api/v1/appearance` | `Admin` | The stored settings, with their timestamps |
| `PUT /api/v1/appearance` | `Admin` | Replace the settings. Send `"scheme": null` for MOSAIC's own colors |

```json
{
  "scheme": {
    "light": { "accent": "#0f9d8a", "backdrop": "#eef6f4", "panel": "#ffffff",
               "border": "#c9dcd8", "text": "#10231f", "textMuted": "#45605a" },
    "dark":  { "accent": "#0f9d8a", "backdrop": "#07120f", "panel": "#15241f",
               "border": "#2f4a43", "text": "#eef7f4", "textMuted": "#a9c2bb" }
  },
  "allowCustomThemes": true
}
```

Colors are six-digit hex values. Anything else, a missing color, an extra field, or a pair below
4.5:1 returns `422`. Both apps validate what they receive again before using it, so only hex
colors ever reach a style rule.

## Accessibility and performance

- **Reduced transparency** (the operating system's setting), **forced colors**, and browsers
  without backdrop blur get solid surfaces in place of glass.
- **Reduced motion** is honored as before.
- Only the top bar and sticky section menus use backdrop blur. The backdrop is one fixed layer
  that does not repaint while the page scrolls.
- Menus, dialogs, and drawers are always solid.

## For contributors

The palette arithmetic (`src/appearance.ts`), the shell stylesheet and its default token values
(`src/prism.css`), the theme provider, the editors, and the fonts are duplicated in `apps/web`
and `apps/portal`, because the two apps are separate packages. Change both copies together;
`scripts/tests/test_shared_appearance.py` fails when they differ, and each app's
`appearance.test.ts` fails when the stylesheet's defaults drift from the built-in palettes.
[DESIGN.md](../DESIGN.md) has the conventions for using the tokens.
