# ADR 0003: Separate SPA/API registrations and fail closed

**Status:** Accepted

## Context

The browser is a public client while the API is a protected resource. Local development needs a
fast path, but production must never fall back to a permissive mode.

## Decision

Use separate single-tenant Entra registrations:

- SPA public client using authorization code + PKCE
- API resource exposing `access_as_user` and an `Admin` app role

Only health endpoints are anonymous. The API validates tenant, issuer, audience, RS256 signature,
time claims, and the `Admin` role. Local authentication and in-memory persistence require explicit
local/test environment settings; startup rejects either when the environment is Azure.

`azd` hooks own registration updates idempotently and fail with precise directory remediation.

## Consequences

- Browser and resource permissions remain independently governable.
- A deployment cannot silently become unauthenticated because identity setup failed.
- Local behavior is intentionally different and visibly opt-in.

## Amendment 2026-09-29: Runtime group claims, Graph lookup and MCP permissions

[ADR 0016](0016-agent-identities-and-security-group-grants.md) adds Entra security-group grants
and agent identities. Bootstrap now configures `groupMembershipClaims: SecurityGroup` on the
model-runtime and control-plane API registrations so validated tokens can carry group object IDs.
The API's managed identity also needs read-only Microsoft Graph application permissions:
`User.ReadBasic.All`, `GroupMember.Read.All` and `AgentIdentity.Read.All`.

The model-runtime registration now exposes the `Mcp.Invoke` delegated scope and
`Mcp.Invoke.Application` application role. MCP grants can be recorded for users, agents and
security groups, and [ADR 0017](0017-mcp-gateway-enforcement.md) uses those permissions for
gateway enforcement on MCP servers MOSAIC publishes.

## Amendment 2026-10-05: The root answers App Service's Always On ping

App Service's Always On sends `GET /` to the API every five minutes to keep it loaded. The API
served nothing there, so each ping got `404`, and Application Insights recorded it as a failed
request once the API recorded its requests. The root is now anonymous too, beside the health
endpoints. It answers `GET` and `HEAD` with an empty `200`: no name, version or configuration.
It isn't in the OpenAPI document, and its requests aren't recorded. Every route under `/api/v1`
still requires a token.
