# MOSAIC web console

The MOSAIC administrator console is a React 19 and TypeScript application built with Vite. It uses
Fluent UI, React Router, TanStack Query, MSAL, and runtime browser configuration.

## Data-source boundaries

- **Live data:** users, workload identities, groups, memberships, service readiness, and
  deterministic APIM policy preview.
- **Sample data:** model deployment, telemetry, cost, policy metadata, and profile activity views.
- **Local preview:** entitlement editing, integration settings, and support-ticket drafts.

Sample and local-preview panels are labeled in the UI. They never replace a failed API response or
claim that Azure resources were changed.

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
