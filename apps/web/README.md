# MOSAIC web console

The MOSAIC administrator console is a React 19 and TypeScript application built with Vite. It uses
Fluent UI, React Router, TanStack Query, MSAL, and runtime browser configuration.

## Administrator workbenches

Models, MCP servers, pools, gateways, identities, cost centers, grants, and access requests use a
compact selectable list with a generous detail workspace. New, edit, import, publish, and review
flows take over that workspace; Back returns to the list and unsaved changes require a discard
decision. Selection and modes support deep links and browser history without storing drafts or
credentials in the URL. Both navigation rails have meaningful icons when collapsed and release
their width to the content.

Analytics keeps applied report scope separate from the draft in its Filters popover. Apply filters
commits the entire draft; Cancel or dismissal leaves the report unchanged. CSV export is an
independent action using the applied scope.

## Data and governance boundaries

Production views render API-backed inventory, configuration, measured telemetry, and recorded
runtime state. Failed or unavailable data is shown as an error or unknown state, never replaced
with sample records. Local development authentication is explicitly disclosed. Fictional estates
belong to tests and the screenshot capture script, not production pages.

Registration records a resource reference; import governs an existing gateway API. Neither creates
Azure resources. Saved entitlement intent and publication settings are distinct from applied
gateway authorization. A reviewed plan describes its effects, and only explicit Apply changes
enforcement. Deterministic policy previews remain read-only.

## Access

In Entra mode the console asks the API which MOSAIC role the caller holds, through
`GET /api/v1/console/me`, before it renders anything. It shows the console only for the `Admin`
role. A caller with only `User`, or with no MOSAIC role, sees a single page that explains why and
offers Sign out; if the check fails, the page offers Try again instead of the console. Local mode
skips the check. Each page keeps its own handling of a 403 response.

## Local commands

```powershell
npm ci
npm run dev
npm run typecheck
npm run lint
npm run test
npm run build
```

Runtime values are loaded from `public/config.js` in local development and generated from
environment variables by `40-runtime-config.sh` in the deployed container. The container's
`nginx.conf` serves `config.js` with `Cache-Control: no-store`, so browsers never reuse an old copy.
