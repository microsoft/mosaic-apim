# MOSAIC interface conventions

## Visual authority

Use the existing Fluent UI React components and semantic `--mosaic-*` tokens in each app's
`src/index.css`. System typography follows Segoe UI, Aptos, and the platform system stack. Blue
identifies navigation and primary actions; warning and danger colors communicate actual state,
not decorative emphasis. Both apps support system, light, and dark appearance.

## Navigation and hierarchy

A collapsible primary rail contains domains and one bottom account menu. The administrator's
slim header contains page/section search and actionable pending-request notifications.
Section navigation presents one real view at a time, preserving unrelated URL parameters and
browser history. Both rails use meaningful icons, tooltips, accessible names, and persisted
collapse state. Collapsing either rail releases its full width to the workspace; content has no
fixed maximum width. At constrained workspace widths, a labeled selector replaces the section rail.

The administrator rail exposes Light, Dark, and System next to the account menu, with a compact
menu when collapsed. It shares the browser preference with Settings. The admin chrome uses
soft blue-tinted glass, rounded edges, and quiet borders; data and forms stay opaque. Unsupported blur, reduced
transparency, and forced colors use solid surfaces.

Primary mobile navigation is modal, traps focus, restores the opener, and makes the underlying
header/content inert. The portal opens on Usage & cost, followed by Catalog, My access, and My
requests in its primary navigation. Portal details use a modal trailing-edge drawer at every
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

The mosaic symbol uses seven rounded tiles arranged as an abstract M. Render it in the theme's
primary foreground color, with the MOSAIC wordmark when space allows. Use the standalone blue
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
