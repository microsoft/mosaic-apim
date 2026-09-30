# ADR 0015: Enforce grants on MCP servers published through API Management

**Status:** Accepted

## Context

ADR 0007 let MOSAIC register MCP servers directly and record the tools they declare. ADR 0005 let
administrators import MCP servers that already exist in API Management. ADR 0014 then let MCP
server grants name users, agent identities, agent users and Entra security groups, but those grants
were governance intent only.

That gap is now material. Administrators need a MOSAIC-owned path that turns an MCP grant into API
Management runtime enforcement without putting MOSAIC in the traffic path and without editing
customer-owned gateway policy. MCP also has a client discovery requirement that models do not have:
interactive clients such as VS Code need RFC 9728 protected resource metadata so they know which
tenant and scope to use before they can sign in.

ADR 0010's ownership model still applies. A publication can only own the resources it records, and
one publication cannot share mutable APIM resources with another. MCP discovery must therefore be
owned per publication rather than through one shared well-known API.

## Decision

**MOSAIC publishes registered MCP endpoints through a passthrough MCP API.** A publication owns
exactly these API Management resources: a backend, an enforcement policy fragment, the passthrough
MCP API, that API's policy, a per-publication protected-resource-metadata API, its `metadata`
operation, and its policy. The MCP API forwards streamable HTTP requests to the registered server.
The discovery API returns only metadata and never forwards to a backend.

**Each publication owns its discovery API.** The metadata API uses the well-known path for that
published server, `.well-known/oauth-protected-resource/{api_path}/mcp`, because the protected
resource is `{gateway}/{api_path}/mcp`. Sharing one discovery API across publications would make
several publications co-own the same APIM resource, which ADR 0010 rejected. Per-publication
ownership keeps create, apply, rollback, unpublish and delete decisions local to one durable record.

**Runtime callers use Entra tokens only.** A published MCP server accepts no APIM subscription key.
The gateway validates a v2 Entra token for the model-runtime registration and requires either the
delegated `Mcp.Invoke` scope or the `Mcp.Invoke.Application` app role. Bootstrap defines both on the
same runtime registration as model invocation, but model and MCP grants never open each other
because their gateway policies require different permissions: `Models.Invoke` /
`Models.Invoke.Application` for models, and `Mcp.Invoke` / `Mcp.Invoke.Application` for MCP.

**Grant matching follows the model rules, with MCP call limits only.** The policy first looks for an
enabled direct grant to the caller's `oid`. If none matches, it checks enabled Entra security-group
grants against the token's `groups` claim and chooses the most generous grant: unlimited beats
limited, then higher call allowance, with ties going to the lower entitlement ID. Group counters are
per member, keyed by the member `oid`, so every member receives the group's full allowance in the
grant window. MCP grants never use token limits; a token-limited MCP grant is invalid.

**The policy never reads request or response bodies.** MCP streaming breaks when APIM buffers a
body. The enforcement fragment strips and validates headers, checks claims, applies call limits,
and sets backend authentication without reading `context.Request.Body` or `context.Response.Body`.
Operators must configure API Management diagnostics to log 0 response-body bytes for MCP APIs, or
streaming can break outside MOSAIC's policy.

**Caller credentials never reach the MCP server.** The policy removes `Authorization`,
`Ocp-Apim-Subscription-Key`, `api-key` and `subscription-key` before forwarding. A caller's token is
for MOSAIC's runtime audience, not the upstream MCP server. When the registered endpoint uses
managed identity, the gateway attaches its own managed-identity token for the endpoint's configured
audience. Endpoints with no authentication get no backend credential. API-key upstreams are refused
for now because MOSAIC does not yet publish a Key Vault-backed named value for an MCP backend key.

**Apply is ordered to fail closed.** A new MCP API requires a subscription until its policy is in
place. If an existing backend URL must change, MOSAIC writes a deny policy before changing the
backend so the old fragment cannot attach the old credential to a new host. Unpublish also denies
before deleting the live API. On apply failure MOSAIC establishes denial and records failed access;
it does not automatically restore an earlier allow policy. If denial cannot be confirmed, the run is
interrupted, access state is unknown, and the durable publication lock remains until an
administrator confirms recovery.

**Imported MCP servers remain recorded, not enforced by MOSAIC.** A server imported from an existing
gateway is customer-owned gateway state. MOSAIC can show and grant intent for it, but its policy
decides who can call it. Publishing enforcement applies only to MCP servers whose publication owns
the APIM resources.

**Unsupported upstreams are explicit.** Streamable HTTP is required because API Management's MCP
passthrough API exposes a streamable endpoint. SSE-only registered servers are not publishable in
this phase. API-key upstream authentication is also not publishable. Managed identity without an
audience is refused because the gateway would not know which token to request.

## Consequences

- Administrators can turn MCP grants into real APIM authorization without making MOSAIC a runtime
  proxy.
- MCP publishing adds a new APIM write surface, so the same manage-mode, plan, apply, lock and
  recovery discipline used for model publishing applies here.
- Clients must obtain runtime Entra tokens for `Mcp.Invoke` or `.default`. A MOSAIC login token,
  model token with only `Models.Invoke`, APIM subscription key, or upstream server credential is not
  enough.
- Security-group grants fail closed when Entra omits the `groups` claim because of overage. The
  remedy is a direct grant.
- A failed apply can deliberately leave the server denied until an administrator reviews and applies
  a fresh plan. That is safer than guessing how to restore an older access policy.
- MCP response-body logging in API Management diagnostics is now an operational prerequisite.
  MOSAIC warns about it but does not take ownership of global diagnostics.
- Imported MCP servers still have governance records and catalog entries, but not MOSAIC gateway
  enforcement.

## Alternatives considered

- **Use one shared protected-resource-metadata API.** Rejected because it would create shared
  ownership across publications and make rollback/unpublish unsafe under ADR 0010.
- **Forward the caller's bearer token to the MCP server.** Rejected. The token audience is MOSAIC's
  runtime registration, and the MCP authorization model forbids token passthrough to a different
  resource.
- **Use APIM subscription keys for MCP grants.** Rejected. The MCP authorization flow and resource
  metadata are token-based, and security-group grants have no key path.
- **Expand group members into direct grants.** Rejected for the same reason as model access: it
  drifts from Entra, hides nested-membership behavior and churns gateway policy when membership
  changes.
- **Support API-key upstream MCP servers now.** Deferred. It needs a Key Vault-backed named value and
  a policy shape that can attach the backend key without storing or displaying it.
- **Support SSE upstreams now.** Deferred. The publication and verifier target streamable HTTP, which
  is what API Management's passthrough MCP API exposes for this phase.

## Live verification still required

Unit tests and policy snapshots do not prove the APIM preview contract. Before treating this as
fully verified on a real gateway, test whether API Management routes an API path starting with
`.well-known`, the live shape of `mcpProperties.endpoints`, policy writes and deletes on MCP APIs
with `2025-09-01-preview`, whether `backendId` is required, whether `on-error` sees the 401 after
`validate-azure-ad-token` fails, streaming through the policy, VS Code consent and sign-in, and the
`groups` claim in agent app-only tokens. Also check two apply behaviors. First, if API Management
creates a default policy on a new MCP API, MOSAIC's ownership check refuses the apply, which fails
closed but blocks publishing. Second, if API Management normalizes the backend URL MOSAIC wrote,
every re-apply sees a changed URL and denies traffic briefly before rewriting the backend. That's
safe but disruptive.

A known limit of the fallback deny: when the deny policy can't be written, MOSAIC writes a deny
fragment instead. That only takes effect once the MCP API's policy includes the fragment. A new API
that never got its policy still requires an API Management subscription key, because it keeps
`subscriptionRequired` until the final activation step.
