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
- an upstream authentication mode of **None**, or **Managed identity** with a configured audience;
- API Management diagnostics configured so response-body logging is 0 bytes for MCP traffic.

MOSAIC does not publish SSE-only MCP servers or upstream MCP servers that require an API key in this
phase.

## Console flow

1. Open **MCP servers** and register or select the server.
2. Choose **Publish** and select a managed gateway.
3. Review the generated publication names and API path, then create the publication. This records
   desired state only.
4. Choose **Plan**. The plan checks live APIM state, builds a grant snapshot and shows every APIM
   resource and policy facet MOSAIC will write.
5. Review warnings. Common warnings include the diagnostics requirement, the need for VS Code or
   another interactive client to have consent for `api://<runtime-client-id>/Mcp.Invoke`, and the
   gateway managed identity's required upstream access.
6. Choose **Apply**. MOSAIC runs the reviewed plan asynchronously and records each step.
7. Create or update MCP entitlements for users, agent identities, agent users or Entra security
   groups.
8. Re-plan and re-apply the publication so the gateway receives the new grant snapshot. Saving a
   grant does not change APIM until this apply succeeds.

## What MOSAIC creates

One MCP publication owns seven APIM resources:

| Order | Resource | Purpose |
| --- | --- | --- |
| 1 | Backend | Points to the registered MCP server URL |
| 2 | Policy fragment | Validates Entra tokens, matches grants, applies call limits, strips caller credentials and attaches backend managed identity when configured |
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

**Unpublish** removes the APIM resources after first denying the MCP API. The MCP server record
stays in MOSAIC so you can publish it again later.

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
publication resources it owns.
