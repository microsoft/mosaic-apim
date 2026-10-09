# Publish MCP servers through API Management

This guide is for administrators who want MOSAIC to publish a registered MCP server through a
managed API Management gateway and enforce MOSAIC grants at the gateway. Publishing is an APIM write
operation. MOSAIC records intent first, then changes APIM only when you review and apply a plan.

## Prerequisites

You need:

- a gateway registered in MOSAIC, switched to **Manage**, writable by the API managed identity, and
  on an API Management tier that supports MCP passthrough APIs;
- gateway MCP capability available from synchronization, with a gateway URL recorded;
- `MOSAIC_MODEL_RUNTIME_CLIENT_ID` configured, because MCP runtime tokens use that registration's
  `Mcp.Invoke` delegated scope and `Mcp.Invoke.Application` app role;
- a registered MCP server whose transport is streamable HTTP, and whose URL ends in `/mcp` with no
  query string, as [The backend URL](#the-backend-url) explains;
- a gateway and MCP server whose environments the [environment rules](#environment-rules) allow
  together;
- an upstream authentication mode of **None**, or **Managed identity** with a configured audience;
- API Management diagnostics configured so response-body logging is 0 bytes for MCP traffic.

MOSAIC does not publish SSE-only MCP servers or upstream MCP servers that require an API key in this
phase.

## Console flow

1. Open **MCP servers** and register or select the server.
2. Choose **Publish** and select a managed gateway. The dialog shows the gateway's environment and
   each server's. A server the environment rules block can't be selected, and the dialog says why.
   A pairing that involves an unclassified resource shows a warning.
3. Review the generated publication names and API path, then create the publication. This records
   desired state only.
4. Choose **Plan**. The plan checks live APIM state, builds a grant snapshot and shows every APIM
   resource and policy facet MOSAIC will write. The access review names each grant's principal
   and its kind, such as Person, Agent, Agent user, or Security group.
5. Review warnings. Common warnings include the diagnostics requirement, the need for VS Code or
   another interactive client to have consent for `api://<runtime-client-id>/Mcp.Invoke`, the
   gateway managed identity's required upstream access, and an environment pairing that involves
   an unclassified resource.
6. Choose **Apply**. MOSAIC runs the reviewed plan asynchronously and records each step. Once the
   MCP API is in API Management, the portal lists the server in its catalog.
7. Create or update MCP entitlements for users, agent identities, agent users or Entra security
   groups.
8. Re-plan and re-apply the publication so the gateway receives the new grant snapshot. Saving a
   grant does not change APIM until this apply succeeds.

## Environment rules

MCP publications follow the same [environment rules](../README.md#environments) as model
publications. Creating, planning, and applying a publication each judge the gateway against the
registered MCP server:

- a blocked pairing is refused, and the console shows **Environment rules block this
  publication** with the reason;
- a pairing that involves an unclassified resource is allowed with a warning, unless **Require
  classification** is on;
- the plan records the verdict. If either environment or the rules change before apply, apply asks
  for a fresh plan.

Once a publication is applied, MOSAIC refuses any re-classification or environment change that
would block it, and names the publication in the refusal. To move a gateway and its MCP server to
a new environment together, classify them in one batch. Unpublish and recovery never check
environments, so a rule change can't trap a publication in place.

## What MOSAIC creates

One MCP publication owns seven APIM resources. Three of them share one API across the gateway's MCP
servers:

| Order | Resource | Purpose |
| --- | --- | --- |
| 1 | Backend | Points at the registered MCP server's URL without its final `/mcp`, as [The backend URL](#the-backend-url) explains |
| 2 | Policy fragment | Validates Entra tokens, matches grants, tags each authorized call with its grant, applies call limits, strips caller credentials and attaches backend managed identity when configured. With a model caller, it also passes each call's reference to the server |
| 3 | MCP API | Exposes the streamable MCP endpoint at `{gateway}/{api_path}/mcp` |
| 4 | MCP API policy | Includes the enforcement fragment and adds the resource metadata challenge on validation failures |
| 5 | Shared metadata API | `mosaic-mcp-metadata`, at the gateway's blank path. Every MCP server MOSAIC publishes on the gateway uses it. It has no policy or backend of its own |
| 6 | Metadata operation | `{api_name}-prm` on the shared API. Handles `GET /.well-known/oauth-protected-resource/{api_path}/mcp` |
| 7 | Metadata operation policy | Returns the RFC 9728 JSON document with the resource URL, tenant authorization server and `Mcp.Invoke` scope |

MOSAIC never takes over an APIM resource it did not create or already record as its own. A name or
path collision with customer-owned APIM state stops creation or planning.

### The shared metadata API

API Management services created since about October 2026 refuse an API path that starts with a dot,
such as `.well-known/...`. So MOSAIC serves each server's protected resource metadata as an
operation on one API whose path is blank. The URL clients use is the standard well-known URL either
way. API Management sends a request to the blank-path API only when no other API's path matches it,
and then only to the operations defined on it, so routing to your other APIs doesn't change.

- The first publication's apply creates the shared API. Later publications add only their operation
  and its policy.
- Unpublishing a server removes its operation and policy. MOSAIC deletes the shared API only with
  the last MCP server that uses it, and only because MOSAIC created it.
- MCP apply and unpublish runs share a durable gateway write lock. If another run is active, wait
  for it to finish and retry. An interrupted run keeps the lock until explicit recovery.
- If another API already uses the gateway's blank path, MOSAIC refuses to plan and names that API.
  API Management allows only one API there, and MOSAIC won't change yours. Give that API a path, or
  publish to another gateway.
- MOSAIC also refuses if an API's path is a prefix of the metadata URL, for example an API at
  `.well-known`, because that API would receive the requests.
- If an API named `mosaic-mcp-metadata` exists but no MOSAIC publication recorded creating it, or it
  isn't at the blank path, MOSAIC refuses rather than adopt it.
- The shared API stays HTTPS-only, has no backend service URL, and doesn't require a subscription.
  MOSAIC restores those settings when they drift. An API-scoped policy is refused rather than
  overwritten, because it can alter the metadata response.
- If someone adds their own operation to the shared API, MOSAIC keeps the API on unpublish. Once
  no publication records it, MOSAIC refuses to adopt that retained API; resolve it before publishing
  again.

**Servers published before this change** keep their own metadata API at
`.well-known/oauth-protected-resource/{api_path}`, with its `metadata` operation and policy. Those
gateways still accept the path. Re-planning and applying keeps that layout, and unpublishing
removes it. To move a server to the shared API, unpublish it and publish it again.
[ADR 0017](adr/0017-mcp-gateway-enforcement.md#amendment-2026-10-08-shared-blank-path-metadata-api)
records the decision.

## The backend URL

Clients call a published server at `{gateway}/{api_path}/mcp`. API Management forwards each call to
the backend's URL with the part of the path after the API's path, `/mcp`, added. So MOSAIC sets the
backend's URL to the registered server's URL without its final `/mcp`, and keeps the scheme, host,
port and any path before it:

| Registered MCP server URL | Backend URL | What API Management calls |
| --- | --- | --- |
| `https://tools.example.test/mcp` | `https://tools.example.test` | `https://tools.example.test/mcp` |
| `https://func.example.test/runtime/webhooks/mcp` | `https://func.example.test/runtime/webhooks` | `https://func.example.test/runtime/webhooks/mcp` |
| `https://tools.example.test:8443/api/mcp` | `https://tools.example.test:8443/api` | `https://tools.example.test:8443/api/mcp` |

A trailing slash is ignored, as it is when you register the server, so
`https://tools.example.test/mcp/` also gets the backend `https://tools.example.test`. MOSAIC won't
create, plan or apply a publication for a server whose URL doesn't end in `/mcp`, or has a query
string, and it says why. It doesn't guess another route for such a server: register the server by
its streamable HTTP URL that ends in `/mcp`. Unpublishing doesn't use the backend URL, so it still
works for a server MOSAIC can no longer publish.

This is behavior observed live, not a documented contract. In Phase 11's M5 journey, a published
server's backend pointed at its full registered URL, `https://<server-host>/mcp`. The gateway's own
request log showed each call to `https://<gateway>/mosaic/mcp/<server>/mcp` forwarded to
`https://<server-host>/mcp/mcp`, which answered `404`, while the registered URL worked when called
directly. MOSAIC writes the MCP API without an `endpoints` map, so API Management serves its default
`/mcp` route. Microsoft's guide to
[expose an existing MCP server](https://learn.microsoft.com/en-us/azure/api-management/expose-existing-mcp-server)
asks for a full endpoint, such as `https://learn.microsoft.com/api/mcp`, as the portal's **MCP
server base URL**, so the portal evidently configures its MCP APIs differently.

A publication applied before MOSAIC set the backend this way still points its backend at the full
registered URL, so calls through it reach `.../mcp/mcp`. Its next plan replaces the backend, after
the step that denies the MCP API first, as for any backend change. Plan, review and apply each
published server again to correct it.

## Apply and fail-closed behavior

MOSAIC orders the plan so access is never opened before policy exists:

- a new MCP API is created with subscription required until its policy is installed;
- when the backend URL changes, MOSAIC denies the MCP API before changing the backend;
- unpublish denies access before deleting the live MCP API;
- a failed apply establishes deny access and records the publication as failed;
- if deny cannot be confirmed, the run is interrupted, the access state is unknown, and the lock is
  retained for explicit recovery.

There is no automatic restore to a previous allow policy after failure. Review a fresh plan and
apply it after you understand the failure.

## Unpublish and delete

**Unpublish** opens a review before anything changes. MOSAIC plans the unpublish, which removes
nothing, and the review lists the grants whose Entra tokens the gateway stops accepting and every
APIM resource MOSAIC deletes, in order. Only **Unpublish MCP server** in the review runs that plan.
If the publication changed since the plan was made, MOSAIC refuses it and the review shows a fresh
one. Unpublishing denies the MCP API before it deletes it.

The MCP server record and its grants stay in MOSAIC so you can publish it again later, and the
Published MCP servers table shows the publication as **Unpublished**, with when. While it's
unpublished, and before its first apply, the portal doesn't list the server, refuses access requests
for it, and gives no connection details.

**Delete** removes the publication record and its MOSAIC-owned MCP server record only after the
publication no longer owns APIM resources and no entitlement points at the published server. If a
server is still published, unpublish it first. If grants still exist, remove or retarget them first.

Imported MCP servers are not deleted through MCP publishing. They remain customer-owned APIM state.

## Recover an interrupted run

A retained publication lock means MOSAIC could not prove the previous worker and its submitted ARM
operations reached a safe terminal state. Do not clear it just because time passed or the API
restarted.

Use the publication's recovery action only after an operator has confirmed that the original worker
is stopped and every ARM operation it submitted is complete. Confirmed recovery re-establishes
denial, records the publication as failed, and releases the lock. Then plan again and apply the
desired state.

## Verify OAuth discovery after applying

A completed apply confirms the resource writes, not the gateway's OAuth discovery behavior. Use
[the MCP access verifier](../scripts/verify_mcp_access.py) before accepting a publication. For the
fictional endpoint `https://gateway.example.test/mosaic/mcp/weather/mcp`, an anonymous request must
receive `401` with `WWW-Authenticate: Bearer resource_metadata="https://gateway.example.test/.well-known/oauth-protected-resource/mosaic/mcp/weather/mcp"`.
That advertised URL must return `200` without credentials, and its JSON `resource` must match the
MCP endpoint. A deliberately invalid bearer token must receive `401` with `error="invalid_token"`
and the same metadata URL. Do not accept a canonical document that works only when fetched at a
different URL from the challenge.

On native MCP APIs, API Management rewrites a `WWW-Authenticate` challenge set inside a
[`return-response`](https://learn.microsoft.com/en-us/azure/api-management/return-response-policy),
in `inbound` or `on-error`. It inserts the API path before the well-known path, so the challenge
names
`https://gateway.example.test/mosaic/mcp/weather/.well-known/oauth-protected-resource/mosaic/mcp/weather/mcp`,
which returns `401`. The saved policy builds the same canonical URL either way, but only a
challenge set with `set-header` directly on the error response in `on-error` keeps it. This was
observed live on a Developer/classic gateway whose global policy was stock. No public
documentation describes it, so treat it as observed platform behavior, not a documented contract.

MOSAIC therefore sends missing and malformed credentials through the
[`validate-azure-ad-token`](https://learn.microsoft.com/en-us/azure/api-management/validate-azure-ad-token-policy)
failure path, and
[`on-error`](https://learn.microsoft.com/en-us/azure/api-management/api-management-error-handling-policies)
sets each 401's challenge with `set-header` on the validator's error response, never inside
`return-response`. It records the original refusal reason before validation and removes malformed
Authorization headers so a valid value inside a duplicate header cannot be accepted. An anonymous
call's challenge omits `error`; malformed credentials and invalid tokens report
`error="invalid_token"`. Every 401 keeps the validator's status and its JSON body, with the
`MCP access denied.` message; `on-error` doesn't replace the body. Token audience, permissions,
grants, backend credential stripping and streaming are unchanged. No alternate anonymous route or
additional APIM resource is created.

One challenge is still set inside `return-response`: the inbound `403` with
`error="insufficient_scope"` that a valid token matching no grant receives. Its `resource_metadata`
URL may be rewritten the same way on native MCP APIs, and MOSAIC leaves it unchanged for now.
Phase 11's M5 journey sends that 403 live, with an ungranted person's token and a token without
`Mcp.Invoke`. The verifier requires `insufficient_scope` there, but doesn't yet compare the
challenge's `resource_metadata` with the connection details. MOSAIC's other refusals, such as the
cost-center 403s, carry no challenge.

Existing publications need a fresh plan and reviewed apply after updating MOSAIC to receive the
changed fragment and API policy. Verify anonymous, malformed and invalid-token challenges again,
then authorized initialization, tools and streaming. Local policy tests do not prove live gateway
acceptance of the change. Keep the publication blocked if discovery still fails; do not relax the
verifier or expose the MCP endpoint anonymously to work around it.

## Diagnostics warning

MCP streaming can break when API Management diagnostics buffer response bodies. Configure
Application Insights or Azure Monitor diagnostics so response-body bytes are 0 for MCP APIs. MOSAIC
warns about this because diagnostics can be global gateway configuration and are outside the
publication resources it owns. The Azure Monitor diagnostic MOSAIC sets for
[usage](usage-analytics.md) logs no body bytes, so it's safe for MCP APIs.

## Servers that call models

When the server's tools call governed models through MOSAIC, name the application they call models
as with `PUT /api/v1/mcp-publications/{id}/model-caller`, then plan and apply the server again. The
server then receives each call's reference in `x-mosaic-on-behalf-of`, which it passes on to its
model calls, so MOSAIC can attribute each model call to the person whose tool call it served. The
application's own model grant still decides access, limits and cost. Every published server's
fragment removes an `x-mosaic-on-behalf-of` a caller sends. See
[MCP servers that call models](mcp-servers-that-call-models.md).

## Usage tags

Once a caller's grant matches, and before call limits, the enforcement fragment emits an API
Management `trace` with source `mosaic` at `information` severity. Its message reads
`mosaic-attribution v=1 g=<grant> m=<object ID> a=<client ID>`. `g` names the grant. For a
security-group grant, `m` carries the caller's validated object ID, so each member's usage can be
counted separately; otherwise it's empty. `a` is the client application ID the validated token
names. Application Insights also gets the grant as the property `mosaic-grant`, the client as
`mosaic-client`, and when the server has security-group grants, the object ID as `mosaic-member`.
A property the call has no value for, such as `mosaic-member` for a direct grant, reads `-`,
because API Management fails a call when a trace property is empty. Each refusal records its
reason instead, as `mosaic-deny v=1 r=<reason>`, with the caller's object ID and client once the
token has validated. The traces read only policy variables, never a body, so streaming is
unaffected.

When the server has a model caller, the message also records `r=<request ID>`, the reference the
server receives, and `i=<object ID>`, the application it calls models as. Application Insights gets
them as `mosaic-mcp-call` and `mosaic-model-caller`.

A successful apply records the same grant identity on each MCP entitlement's binding, and a
successful unpublish clears it. The portal's usage report then shows these grants as linked from
gateway log traces; see [ADR 0015](adr/0015-end-user-usage-report.md). If MOSAIC can't save a
binding, the run still succeeds, the error is logged, and the next apply corrects the binding.

For the traces to reach Log Analytics, the MCP API needs an Azure Monitor diagnostic at Information
or Verbose. **Enable API diagnostics** in the gateway's **Telemetry** section sets it on every API
MOSAIC published, and MOSAIC then sets it on each new publication. `ApiManagementGatewayLogs`
records the traces in `TraceRecords`, and MOSAIC rolls them up into usage; see
[Usage analytics](usage-analytics.md). An Application Insights diagnostic at Information verbosity
records one trace per call, whatever its sampling rate. To stop them, set that diagnostic's
verbosity to Error. The object and client IDs are personal data, so apply your retention and
access rules to both destinations.
