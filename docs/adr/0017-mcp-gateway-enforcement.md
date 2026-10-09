# ADR 0017: Enforce grants on MCP servers published through API Management

**Status:** Accepted. Amended by [ADR 0025](0025-mcp-model-calls-on-a-persons-behalf.md): an MCP
server that names the application it calls models as receives each call's reference, so those model
calls can be attributed to the call's caller, and every MCP server removes a reference a caller
sent.

The 2026-10-08 amendment below replaces the per-publication metadata API with operations on one
shared, blank-path API, because API Management services created since about October 2026 refuse an
API path that starts with a dot.

## Context

ADR 0007 let MOSAIC register MCP servers directly and record the tools they declare. ADR 0005 let
administrators import MCP servers that already exist in API Management. ADR 0016 then let MCP
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
  ownership across publications and make rollback/unpublish unsafe under ADR 0010. Reversed by the
  2026-10-08 amendment: new API Management services refuse the per-publication API's path, and the
  amendment confines the sharing to one API with no policy of its own.
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

## Amendment 2026-09-30: Environment rules, bindings, and usage tags

MCP publishing now matches model publishing in three more ways. MOSAIC is a proof of concept, so
existing MCP publications aren't grandfathered.

- **Environment rules.** Create, plan, and apply judge the gateway against the registered MCP
  server, exactly as [ADR 0014](0014-environments.md) does for models. A blocked pairing is refused,
  a warning is shown in the plan, and the plan digest pins the environment fingerprint. A
  re-classification or rule change that would block an applied MCP publication is refused.
  Unpublish and recovery still never check environments.
- **Bindings.** A successful apply projects an orchestrated binding onto each grant in the applied
  snapshot, and a successful unpublish clears them, as model publishing does. An MCP grant has no
  APIM subscription, so its binding records the grant's attribution key instead. Bindings are
  written after the publication's state is saved, so a storage error is logged rather than failing
  the run. Failing it would put a correctly published server behind the deny-all fragment. The
  next apply writes the bindings again.
- **Usage tags.** After authentication and before call limits, the enforcement fragment emits a
  `trace` naming the matched grant, and for a security-group grant, the caller's validated `oid`.
  The trace reads only policy variables, never a body, so streaming is unaffected.
  [ADR 0015](0015-end-user-usage-report.md) describes how usage is attributed.

Live verification should also confirm that the trace's message reaches
`ApiManagementGatewayLogs.TraceRecords` for an MCP API.

## Amendment 2026-09-30: Reviewed unpublish, and the catalog

- **Unpublish is planned and reviewed**, as the ADR 0010 amendment records for models.
  `POST /api/v1/mcp-publications/{id}/unpublish-plan` returns the plan, and
  `POST /api/v1/mcp-publications/{id}/unpublish?plan={planId}` runs exactly that plan. It refuses a
  missing, foreign, or stale plan. The review lists every enabled grant in the applied snapshot,
  whose Entra tokens the gateway stops accepting.
- **The portal lists a published MCP server only while its MCP API is in API Management.** The
  server record exists from the moment the publication is created, so grants can be recorded before
  the first apply. Until that apply succeeds, and after an unpublish, the portal leaves the server
  out of the catalog, refuses requests for it with `409`, and gives no connection details. Before,
  a draft's server was listed as "Recorded, not enforced", which describes an adopted server whose
  own policy decides, not one with no API at all.

## Amendment 2026-10-06: The backend URL

- **The backend points at the registered URL without its final `/mcp`.** API Management serves the
  MCP API at its default `/mcp` route and forwards each call to the backend URL with `/mcp` added.
  Phase 11's M5 journey saw it live: with the backend at the full registered URL, the gateway
  forwarded calls to `.../mcp/mcp`, which answered `404`. MOSAIC now keeps the scheme, host, port
  and any path before the final `/mcp` segment. No public documentation describes this, so it's
  recorded as observed behavior.
- **A server whose URL doesn't end in `/mcp`, or has a query string, isn't publishable.** Create,
  plan and apply refuse it rather than guess an `endpoints` map. Unpublish and recovery don't need
  the backend URL.
- **Existing publications are corrected by their next reviewed apply.** The plan shows the backend
  as an update, after the deny that guards every backend change.

## Amendment 2026-10-08: Shared blank-path metadata API

**What we saw.** On 8 October 2026, applying an MCP publication failed on API Management services
created that month. Azure Resource Manager refused the per-publication metadata API with
`400 ValidationError: Invalid value of the Web API URL suffix` (target `path`). Scratch tests showed
that these services refuse any API path starting with `.`, with API versions `2022-08-01` and
`2024-05-01`. A service created in August 2026 still accepted the same path. The same new services
accept an API whose path is blank, and an operation on it whose URL template is
`/.well-known/oauth-protected-resource/{api_path}/mcp`.

**Decision.** New publications serve their protected resource metadata as an operation on one shared
API, `mosaic-mcp-metadata`, whose path is blank. Each publication owns three things on that gateway:

- its `GET` operation on the shared API, named `{api_name}-prm`, whose URL template is the standard
  well-known path;
- that operation's policy, which returns the metadata document;
- a record of the shared API itself.

The URL clients use doesn't change: `https://{gateway}/.well-known/oauth-protected-resource/{api_path}/mcp`.
The `resource_metadata` value in `WWW-Authenticate` and the document's `resource` are therefore
the same in both layouts.

**Why routing elsewhere is unaffected.** API Management chooses the API whose path is the longest
prefix of the request path, then matches the request against that API's operations. A blank-path API
is chosen only when no other API's path matches. Within it, only its defined operations match, and
anything else gets the same `404` it got before. Each operation's URL template is a literal path
with no parameters, so one publication's operation can't answer for another.

**Ownership of the shared API.** The shared API carries no policy and no backend: it is a blank-path
shell with `subscriptionRequired` false. Publications never share a mutable policy, which is what
ADR 0010 guards against.

- The first apply that needs it creates it and records it as created by MOSAIC. Every later apply
  that reuses it records it too, so the fact that MOSAIC created it is carried forward while any
  publication still uses it.
- MOSAIC uses an existing `mosaic-mcp-metadata` only if its path is blank and an MCP publication on
  the gateway has that record. It never infers ownership from the name. Otherwise the plan, or an
  apply that finds the API after planning, refuses without changing that API.
- A plan restores the shared API's required routing settings if they drift: it must be HTTPS-only,
  have no service URL, and not require a subscription. MOSAIC refuses an API-scoped policy rather
  than overwrite it, because it can change or intercept the operation policy's response.
- Unpublishing deletes the publication's operation policy and operation. It deletes the shared API
  only if no other operation remains on it and no other publication records it. Otherwise that step
  is skipped and the unpublish still succeeds. The unpublish plan warns about this.
- The API name `mosaic-mcp-metadata` is reserved. A publication can't use it as its MCP API name.

**Conflicts that refuse a plan.** API Management allows only one API with a blank path. If the
gateway already has another API there, MOSAIC refuses to publish rather than touch it, and names the
API. MOSAIC also refuses if another API's path is a prefix of the metadata path, such as an API at
`.well-known`, because that API would receive the metadata requests. Inventory and live checks
both apply, so a conflicting API added after the last sync is still caught at plan time.
Apply repeats these checks before writing, so a conflict introduced after the review is refused too.

**Existing publications keep the legacy layout.** A publication whose record includes the old
per-publication metadata API, its `metadata` operation or its policy keeps those resources: re-plans,
applies and unpublishes treat them exactly as before. Those publications are on older services that
still accept the path, so migrating them would only add risk. To move one to the shared layout,
unpublish it and publish it again. A publication whose apply failed before it created the old
metadata API, as on 8 October 2026, has no legacy record, so its next apply uses the shared layout.

**Concurrency.** Alongside each publication's lock, MCP apply and unpublish hold a gateway-scoped,
durable, non-expiring metadata write lock for the whole run. This serializes first creation,
ownership recording, adding operations and last-user deletion, including across API processes.
Another MCP writer on the gateway is refused while the lock is held; retry after the first finishes.
An interrupted writer retains both locks until explicit recovery confirms it has stopped and
establishes denial. Recovery also acquires the gateway lock for interrupted pre-upgrade runs.

If someone adds an unrecorded operation to the shared API, unpublish keeps the API to avoid removing
their operation. Once the last publication drops its reference, MOSAIC no longer has ownership
evidence and refuses to adopt that retained API; an operator must resolve it before publishing again.
