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
- a registered MCP server whose transport is streamable HTTP;
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

One MCP publication owns seven APIM resources:

| Order | Resource | Purpose |
| --- | --- | --- |
| 1 | Backend | Points to the registered MCP server URL |
| 2 | Policy fragment | Validates Entra tokens, matches grants, tags each authorized call with its grant, applies call limits, strips caller credentials and attaches backend managed identity when configured |
| 3 | MCP API | Exposes the streamable MCP endpoint at `{gateway}/{api_path}/mcp` |
| 4 | MCP API policy | Includes the enforcement fragment and adds the resource metadata challenge on validation failures |
| 5 | Metadata API | Owns the well-known protected-resource-metadata path for this publication |
| 6 | `metadata` operation | Handles `GET /mcp` under the metadata API |
| 7 | Metadata API policy | Returns the RFC 9728 JSON document with the resource URL, tenant authorization server and `Mcp.Invoke` scope |

MOSAIC never takes over an APIM resource it did not create or already record as its own. A name or
path collision with customer-owned APIM state stops creation or planning.

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

## Diagnostics warning

MCP streaming can break when API Management diagnostics buffer response bodies. Configure
Application Insights or Azure Monitor diagnostics so response-body bytes are 0 for MCP APIs. MOSAIC
warns about this because diagnostics can be global gateway configuration and are outside the
publication resources it owns. The Azure Monitor diagnostic MOSAIC sets for
[usage](usage-analytics.md) logs no body bytes, so it's safe for MCP APIs.

## Usage tags

Once a caller's grant matches, and before call limits, the enforcement fragment emits an API
Management `trace` with source `mosaic` at `information` severity. Its message reads
`mosaic-attribution v=1 g=<grant> m=<object ID> a=<client ID>`. `g` names the grant. For a
security-group grant, `m` carries the caller's validated object ID, so each member's usage can be
counted separately; otherwise it's empty. `a` is the client application ID the validated token
names. Application Insights also gets the grant as the property `mosaic-grant`, the client as
`mosaic-client`, and when the server has security-group grants, the object ID as `mosaic-member`.
Each refusal records its reason instead, as `mosaic-deny v=1 r=<reason>`, with the caller's object
ID and client once the token has validated. The traces read only policy variables, never a body,
so streaming is unaffected.

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
