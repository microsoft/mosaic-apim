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
environment variables by `40-runtime-config.sh` in the deployed container.
