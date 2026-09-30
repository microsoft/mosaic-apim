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

[ADR 0014](0014-agent-identities-and-security-group-grants.md) adds Entra security-group grants
and agent identities. Bootstrap now configures `groupMembershipClaims: SecurityGroup` on the
model-runtime and control-plane API registrations so validated tokens can carry group object IDs.
The API's managed identity also needs read-only Microsoft Graph application permissions:
`User.ReadBasic.All`, `GroupMember.Read.All` and `AgentIdentity.Read.All`.

The model-runtime registration now exposes the `Mcp.Invoke` delegated scope and
`Mcp.Invoke.Application` application role. MCP grants can be recorded for users, agents and
security groups, and [ADR 0015](0015-mcp-gateway-enforcement.md) uses those permissions for
gateway enforcement on MCP servers MOSAIC publishes.
