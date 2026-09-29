# ADR 0008: Separate authentication from authorization and give the portal its own identity

**Status:** Accepted

**Supersedes:** the role-enforcement decision in [ADR 0003](0003-entra-applications-and-local-auth.md).
That ADR's separation of SPA and API registrations, its fail-closed posture, and its treatment of
local authentication all stand.

## Context

MOSAIC was built for one audience. `EntraAuthenticator.authenticate` validated the token *and*
required the `Admin` app role in the same step, so a caller without that role was rejected before
any route was reached. That was the right shape while the administrator console was the only
client.

It is the wrong shape now. MOSAIC needs an end-user experience: people who consume the gateway
need to see what they are entitled to, what else they could request, how much they have consumed,
and which policies restrict them. Those people are not administrators and must never reach an
administrative route. Folding the role check into authentication leaves no way to admit them at
all.

APIM's developer portal is not a substitute. It is catalogued around APIs, has no concept of MCP
servers, cannot explain a caller's rate or token limits, and knows nothing of MOSAIC's entitlement
model.

## Decision

**Authentication establishes identity; authorization is a per-route decision.**

`EntraAuthenticator.authenticate` validates issuer, audience, RS256 signature, time claims, and
tenant, then returns an `AuthContext` carrying the app roles Entra issued. Route access is decided
by explicit dependencies:

- `require_admin` — the `Admin` role. Nothing else satisfies it. Every existing `/api/v1` route
  keeps it.
- `require_portal_user` — the `User` role. `Admin` also satisfies it, so an administrator can open
  the portal without holding a second assignment.
- `require_mosaic_role` — either role. It guards one route, `GET /api/v1/console/me`, which tells
  the administrator console which of MOSAIC's roles the caller holds. It never widens an
  administrative route.

Authentication still fails closed at the edge: a token from the configured tenant carrying **none**
of MOSAIC's app roles is rejected with 403 before a route is reached. Splitting the check widens
who may be admitted; it does not make admission implicit.

**The administrator console asks the API which role the caller holds before it renders.**

Both roles are defined on the API registration, so they appear in the API access token, not in
either SPA's ID token. The console does not decode that token. It calls `GET /api/v1/console/me`,
which answers from the validated token without reading Cosmos, and renders:

- the console, for a caller holding `Admin`;
- for a caller holding `User` alone, one page saying the console is for MOSAIC administrators,
  that the `User` role opens the end-user portal, and that an administrator must grant `Admin`;
- for a caller holding neither, one page saying an administrator must grant them a MOSAIC role;
- if the check itself fails, an error with a retry — never the console.

That page replaces the console's navigation and actions. It is not the authorization boundary:
every administrative route still refuses those callers, and each page keeps its own 403 handling.
Local authentication has no Entra roles, so local mode renders the console without asking.

**The portal gets its own Entra registration and its own app role.**

The API registration exposes a second app role, `User`, alongside `Admin`. It is assignable to
users and to groups, so an operator onboards a population by assigning the role to an Entra group
rather than to each person. It is never granted implicitly: being a member of the tenant is not
enough.

A third registration, `mosaic-<env>-portal`, is created by the same idempotent `azd` hook that
creates the API and SPA registrations, with its own redirect URIs and its own pre-authorization on
the API scope. The administrator console and the portal are therefore independently governable —
either can be disabled, have its redirects changed, or have consent revoked without touching the
other.

All three role and scope identifiers remain deterministic GUIDs derived from stable names, so
reruns converge rather than duplicate.

## Consequences

- One API serves both audiences without a second deployment of the control plane, and both
  audiences authenticate against one resource with one scope.
- Route authorization is now visible in each route's signature rather than hidden in the
  authenticator, which is where a reviewer looks for it.
- Moving a security check is a regression risk, so the change lands with a test that enumerates
  the published admin surface from the OpenAPI schema and asserts every operation refuses a caller
  holding only the portal role. The check covers new routes automatically. `GET /api/v1/console/me`
  is the one exception, listed by exact method and path, so a new route under `/api/v1/console` is
  still checked as administrative.
- An operator must assign the `User` role before anyone can use the portal. That is deliberate:
  a portal that admitted the whole tenant by default would leak the catalog.
- Local development can simulate either audience through `MOSAIC_LOCAL_ROLES`, which remains
  available only in local and test environments.
- The portal's deployed redirect URI is added once the portal web app exists. Until then the
  registration keeps only its localhost redirects; the registration itself is never skipped.
