# MOSAIC interface conventions

## Visual authority

Use the existing Fluent UI React components and the semantic `--mosaic-*` tokens. The tokens, the
fonts, and the shared shell live in each app's `src/prism.css`; page and component styles stay in
`src/index.css` and the CSS modules. Outfit sets the interface and JetBrains Mono sets breadcrumbs,
figures in dense lists, and chart labels; both are vendored, with the system stack as fallback.
Warning and danger colors communicate actual state, not decorative emphasis.

Every color derives from a six-color palette: accent, page background, panels, borders, text, and
secondary text (`src/appearance.ts`). Never write a literal color in a component. Use
`--mosaic-accent` with `--mosaic-on-accent` for filled controls and `--mosaic-primary` where the
accent is text, because a custom accent may be too light or too dark to serve as both. The accent
identifies the active navigation item, primary actions, and counters. Graphs and usage bars are
drawn in the text color, with the accent marking only the latest point, so they read the same
under any palette; status bars keep their status colors.

Both apps support Light, Dark, System, and Custom. Light and Dark show the organization's colors
when an administrator has saved them in **Settings > Appearance**, and MOSAIC's own otherwise.
Custom is a person's own palette, kept in their browser; an administrator can turn it off. A
palette cannot be saved while any text pair falls below 4.5:1, in the editors or through the API.
`appearance.ts`, `prism.css`, the theme provider, and the color editors are identical in both apps,
and a test keeps them so.

## Navigation and hierarchy

A collapsible primary rail contains domains and one bottom account menu. The administrator's
slim header contains page/section search and actionable pending-request notifications.
Section navigation presents one real view at a time, preserving unrelated URL parameters and
browser history. Both rails use meaningful icons, tooltips, accessible names, and persisted
collapse state. Collapsing either rail releases its full width to the workspace; content has no
fixed maximum width. At constrained workspace widths, a labeled selector replaces the section rail.

The primary rail starts compact, as icons with tooltips, and keeps a person's choice to expand it.
While it is compact the top bar carries the MOSAIC wordmark. Both primary rails expose Light,
Dark, System, and Custom next to the account menu, as a menu when compact. They use the same
preference key as administrator Settings.

The page is a backdrop washed with the accent color. The rail is a solid dark panel; the top bar
is frosted glass that blurs what scrolls beneath it; the workspace and its panels are translucent
glass over the backdrop. Only the top bar and sticky section rails use backdrop blur, because
only they have content moving behind them. Menus, dialogs, and drawers stay solid, so their text
never competes with what is behind them. Reduced transparency, forced colors, and browsers
without backdrop blur get solid surfaces throughout.

Primary mobile navigation is modal, traps focus, restores the opener, and makes the underlying
header/content inert. The portal opens on Usage & cost, followed by Catalog, My access,
My cost centers, and My requests in its primary navigation. My cost centers owns membership,
published discovery, and creation/join entry points; My requests tracks the decisions.
Approvals is visible only with a current delegated assignment. Its queue and full-workspace
review are organized by assigned center; evidence, consequences, and decision controls are
separate from the requester's personal tracking view.
Portal details use a modal trailing-edge drawer at every
width, without resizing the underlying list. Closing or switching grant details unmounts
credential controls; secrets never persist in route state.

Administrator resource workbenches keep a compact selectable list beside generous selected detail.
Only concise names, context, and essential state belong in list rows; technical identifiers,
bindings, limits, tool inventories, and secondary actions belong in detail. On narrow screens,
selection opens detail in the same workspace with a labeled return to the list.

New, edit, import, publish, approval, and plan review take over the full available workspace, not
a modal or permanent side form. Editing is explicit; Back, section navigation, browser navigation,
and page unload protect unsaved drafts. Canceling a discard decision retains the draft and focus.
Saving returns with the saved record selected. Destructive confirmations and ephemeral credential
reveals may remain modal. Credentials and drafts never enter URLs, history state, or browser storage.
Directory onboarding starts with Entra search, then selection and an explicit Add action.
Configuration or permission failure has recovery guidance; manual entry is a deliberate advanced
fallback.

## Brand

The mosaic symbol uses seven rounded tiles arranged as an abstract M. Render it in the color of
the surface's text, with the MOSAIC wordmark when space allows. Use the standalone blue
vector for favicons. The mark is decorative when the adjacent wordmark names the application.

Admin people use optional authenticated Entra thumbnails or initials. Other identity kinds use
neutral, recognizable type icons; category alone does not earn a warning or success color.

## Content and state

Page headings name the task; descriptions explain scope and effects on a separate line. Consistent
section toolbars position the primary action. Button labels and status pills never wrap internally;
whole controls reflow rather than crush adjacent content. Prefer one obvious task action and
separate destructive actions. Clearly distinguish read-only inventory, saved intent, reviewed
plans, and applied gateway state. Unknown data is not zero, missing telemetry is
not healthy, and unpriced usage is not free.

Do not label API-backed pages "Live data" or include invented production records, activity,
integrations, or ticket references. Honest local-authentication and read-only preview notices stay.
Notifications show the actual requester name, requested resource, and a specific request link;
failed name lookup has an explicit unavailable/retry state, not an anonymous identifier substitute.
Analytics groups draft controls in Filters with Apply and Cancel; report queries and CSV exports
use only applied scope.

Catalog and My access use aligned, model-first resource rows rather than card grids. Group
offerings only when structured model name and format are known; otherwise retain exact resource
identity. Keep environment and aggregate state in the list, and select the exact offering or
cost-center grant in the drawer. Grouping is presentation only: permissions, requests, connection
details, and accounting remain scoped to their original resources and grants.

## Accessibility and responsiveness

Use semantic landmarks, visible focus, descriptive icon labels, active-navigation announcements,
and explicit loading/error/empty states. Preserve keyboard operation, reduced motion, theme
contrast, and reflow. Essential tasks must work without a wide context pane or hover.
