# Grants and gateway access

[Documentation](README.md) | [MOSAIC](../README.md)

- [Governed model access](#governed-model-access)
- [Cost-center eligibility and resource modes](#cost-center-eligibility-and-resource-modes)
- [Published MCP server access](#published-mcp-server-access)
- [Recovering an interrupted operation](#recovering-an-interrupted-operation)

See [workflow diagrams](workflows.md) for the separation between saved intent, approval, explicit
apply, and a live call. For directory and gateway identity permissions, see [architecture](architecture.md).

## Governed model access

Use **Entitlements** to link a previously published model if necessary, opt it into governed
access, and choose its key/Entra methods. Create a direct user, application, agent, or Entra
security-group grant, then review and apply the **model-wide** plan. The review includes every
grant/settings change it will deploy. Saving a grant is not an APIM write, and a pending revocation
is not yet a runtime revocation. Use the person's Entra object ID, the application's
**service-principal object ID**, the agent identity's object ID, or the security group's object ID;
application client IDs are not interchangeable except for agent identities, whose app ID and object
ID are the same. Prefer the subscription-key header over putting credentials in URLs.

New principal registrations require a GUID object ID for every kind, including manually
entered applications and managed identities. MOSAIC stores it in lowercase, hyphenated form
and permits only one registration per object ID within a tenant, regardless of principal kind.
A GUID's syntax does not verify its existence or kind in Entra; manual enrollment does not
silently require additional Microsoft Graph permissions.

Resource access requests capture optional name and username claims from the requester's validated
sign-in token when submitted. Administrator review keeps the current MOSAIC label separate from
that request-time snapshot and leaves the object ID visible. The name is preferred; username
falls back through `preferred_username`, `upn`, and `unique_name`. Values are normalized and
bounded to 200 characters, are display metadata only, and do not change tenant/object identity,
roles, group eligibility, resource scope, or limits. The request body cannot supply those fields.

Approval uses the snapshot's name, or username if no name is available, as the initial label only
when registering a requester MOSAIC does not yet know. It never overwrites or fills an existing
principal's label. Older requests and tokens without display claims remain valid and can produce
an unlabeled registration; no migration, backfill, or extra Graph consent is required. Existing
directory lookup remains a display fallback where available, not evidence added to the snapshot.
Approval still saves grant intent only; gateway plan review and apply remain separate.

Existing registrations remain readable, including older non-GUID IDs. Self-service direct
access requires one unambiguous registration for the caller and a grant naming that same
principal with its matching user/application subject kind. Duplicate registrations block
listing and direct connection/key operations with a conflict, even if one ID matches exactly.
This is a directory-record ownership check, not independent verification of the token's
user/application class. Security-group membership remains a separate token-based path and
never authorizes disclosure of a group's key. Explicit administrator handoff is unchanged.

To repair legacy registrations, review **Principals**, their grants, and credential audit events.
Disable the affected grants and review/apply revocation before removing their references through
the supported grant, membership, and principal-management operations. Register the correct Entra
object ID and kind, recreate the intended grants, then review/apply access. MOSAIC does not merge
duplicate records or rewrite existing principal/grant IDs automatically. If a key might have
been disclosed, rotate it through an administrator route; fixing directory metadata cannot
recall an already copied bearer credential.

Opting in deliberately stops the publication's former generic/bootstrap key from authorizing
requests. Each direct grant can have its own API-scoped subscription, created on request rather
than by an apply: the grant's holder creates, rotates, or deletes it in the portal, and an
administrator does so for applications and agents on the Entitlements page. Its name is fixed by
the grant, so the applied policy recognizes it at once. Keys work only when the publication accepts
keys and the grant's [cost center](cost-centers.md) allows them. Both primary and secondary keys and
the subject's Entra token share that grant's counters. A key is still a transferable bearer
credential, not proof that the named person is using it. Disabling both authentication methods
denies everyone; it never makes the API anonymous.

When a caller supplies both a key and a token, both must validate and select the same grant.
Government offerings are key-only; eligible initial direct-grant keys are prepared after apply,
with reveal still explicit and audited. See [Government access](azure-government.md).

Security-group grants are Entra-only. They create no APIM subscription and reveal no keys. APIM
matches them from the validated runtime token's `groups` claim, applies limits per member by the
member's `oid`, and chooses the direct grant first or otherwise the most generous matching group
grant within the selected cost center. A disabled grant counts as absent. For a custom center,
**both group and individual token grants also require the center's group claim**. A direct grant
cannot bypass that membership requirement. Group-overage tokens fail this check; only General's
exempt direct-token path can avoid a center-group claim.

Automatic center access is one stable group grant per exact resource, not a set of personal
entitlements. Joining the group performs Graph work, with no per-member automatic entitlement
creation or APIM policy write. Resource/grant configuration still needs reviewed apply. Individual
keys remain separate explicit credentials where permitted.

The reviewed policy explicitly translates the earlier subscription ID/key counter defaults into
shared grant counters, so previously saved grants can be opted in without silently rewriting their
desired state. Other custom counter expressions are rejected instead of weakening enforcement.

Token-governed model access is constrained by APIM's supported chat-completions/response schemas.
Unsupported image, audio, or embedding operations must not be mistaken for metered calls.
For responses, AI Services chat and Anthropic Messages routes that do not include a deployment in
their path, the request body's `model` must exactly match the deployment name shown in connection
information. On an Anthropic publication, governed access permits only the Messages operation.
Publication limits remain safeguards even when a grant has no additional limits. Native rate and
quota enforcement is distributed and gateway-scoped, not an exact global accounting ledger.

Administrators can explicitly reveal/copy an applied grant's key. Portal clients with the MOSAIC
`User` role can use:

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/me/entitlements` | The caller's own direct grants and deployment state; no keys |
| GET | `/me/entitlements/{id}/connection` | Endpoint, operations, runtime audience/scope, model client ID, limits, cost center, and the `x-mosaic-cost-center` header |
| GET | `/me/entitlements/{id}/mcp-connection` | MCP server URL, transport, authentication, call limits, cost center, and runtime state |
| POST | `/me/entitlements/{id}/keys` | Create the grant's key; 201 |
| POST | `/me/entitlements/{id}/keys/rotate` | Regenerate one slot; body `{"slot":"primary"}` or `{"slot":"secondary"}` |
| DELETE | `/me/entitlements/{id}/keys` | Delete the grant's key |
| POST | `/me/entitlements/{id}/keys/reveal` | The requested key; body `{"slot":"primary"}` or `{"slot":"secondary"}` |
| GET | `/me/usage?period=30d` | The caller's own usage and limits per grant, measured from gateway logs; see [Usage and cost in the portal](analytics-reference.md#usage-and-cost-in-the-portal) |

People use the connection's `tenantId`, `entraClientId` and `entraScope` to get a runtime token.
See [Call a published model with an Entra token](call-models-with-entra-tokens.md).
Use the model client for human delegated sign-in only when the applied audience is the runtime
registration it is consented for; copy the client ID and scope actually shown in connection
details. An inherited grant still needs a caller token, not a group credential. Applications and
agent identities sign in as themselves with `.default`; agent users use delegated tokens from
their parent agent identity. See
[Call a published model with Microsoft Entra Agent ID](call-models-with-agent-identities.md).

The administrator equivalents omit `/me` and require `Admin`. Knowing another entitlement or
application ID does not authorize a reveal. Application-owner delegation remains future work.
A current-user route always uses the token's identity, never a caller-supplied user ID.

For custom centers, new key creation/reveal checks current group eligibility as well as a trusted
applied individual grant and resource/center key enablement. An existing copied key is independent
of JWT membership: a member removal requires affected-key recheck and explicit revoke/apply work,
not just a directory edit. See [removal consequences](cost-centers.md#removing-a-member).

When an apply fails, the `/me` and `/portal` routes report it through `runtime.status` alone and
return `runtime.error` as null, even to an administrator. That field holds API Management's raw
error, which names MOSAIC's internal resources and is shared by every grant on the model, so only
the administrator routes return it.

Caller grant and connection responses use explicit field allowlists, not the administrator's
record shapes. Limits retain their quantities, periods and prompt-estimation settings but omit
`counterKeyExpression`. Bindings omit `gatewayId`, `counterKeyExpression`, `source` and `boundAt`;
grant `notes` and the administrator's `poolName` are not returned. The opaque `attributionKey`
and per-member flag remain. APIM product/subscription display names in a binding are returned
only when the legacy **Usage attribution** fallback displays them; otherwise both are null.
The internal binding is unchanged, so usage attribution, quotas and cost-center accounting do
not lose their gateway/subscription links.

Connection publication/gateway/pool/MCP IDs remain scoped references for connection verification
and existing Advanced details. `runtime.subscriptionName` and key-result `subscriptionName`
remain key-association metadata: the portal checks that a reveal describes the requested grant,
slot and subscription, and discards a key when its connection changes. Runtime endpoints,
operation paths, MCP resource-metadata URLs, Entra tenant/client/scope values, and cost-center
headers remain usable connection inputs, not ARM management locators. Generated clients should
use the caller response schemas rather than administrator enforcement models.

Caller grant, connection and key failures keep their HTTP status and MOSAIC `code`, `message`,
`details` envelope. Details contain only approved, locally authored recovery reasons and valid
numeric upstream status/retry information when available, not Azure's raw text, upstream codes,
ARM URLs/resource paths, permission scopes, or policy expressions. Caller authorization,
missing grants, conflicts, validation and upstream failures remain distinct; `gateway_forbidden`
means MOSAIC's gateway permissions need administrator review, not that the caller simply needs
another role. Existing retry/correlation and no-store behavior is preserved. Administrator routes
keep their management diagnostics, but sensitive credential responses never appear in errors or
logs for either role.

Portal access-request responses also project explicit caller fields. They retain readable
justification, decision notes, states and resource/cost-center routing, but do not inherit stored
eligibility-group snapshots, approval provenance, or administrator-only requester snapshots.

In the portal, each model grant on **My access** has a **Connection details** button. The
connection loads only when it is expanded, and it shows the endpoint, full operation URLs,
deployment, key header, accepted methods, Entra tenant, client ID, scope and audience, limits, the
cost center and its `x-mosaic-cost-center` header with a copy-ready curl, and whether the grant is
applied to APIM. If the last apply failed, the panel says so and asks the
person to have an administrator retry it. When the connection has an `entraClientId`, a **Get a token
(Python)** sample signs the person in with the model client using a device code. Without one, the
panel asks them to get the client ID from an administrator. **Show primary key** and **Show
secondary key** reveal one key for 60 seconds. The key is also hidden by **Hide key**, when the
panel closes, and when the user navigates or leaves the page. The key is held only in component
state, never in the query cache, browser storage, the URL, or logs. Code samples use
`$MOSAIC_API_KEY` or `$MOSAIC_ACCESS_TOKEN` placeholders and never include a revealed key. For a
Claude model, the samples call `/anthropic/v1/messages` without an `api-version`, and the panel
gives the Anthropic SDK base URL. An inherited grant explains its source group and JWT requirements
instead of offering a personal key reveal; it still identifies the exact resource's connection.

## Cost-center eligibility and resource modes

A [cost center](cost-centers.md) defines charging and resource policy; its dedicated Entra group
defines membership; an applied entitlement authorizes one exact resource. General is exempt from
the mandatory group, identified by its built-in identity rather than its name or code.

| Resource mode | Access workflow |
| --- | --- |
| Automatic | Members inherit the exact resource through one managed group grant, using JWTs after apply. No personal key is inherited. |
| Request-only | Eligible members request an individual grant. Approval saves intent; gateway apply makes it effective. |
| Not available | New grants/requests under the center are refused server-side, including bypass attempts outside the portal. |

Automatic is available only with caller-side Entra token support. A backend using an API key
does not disable caller JWTs. Where individual keys are supported, both publication keys and
center keys must be enabled and an explicit eligible individual grant is required. Individual
quota exceptions remain reviewed and membership-gated; they do not bypass unavailable resources.
Government offerings are excluded from these new center workflows.

The catalog's visual model grouping is not a permission boundary. A model API, a model in one
particular pool, and an MCP server are independent offerings. A pool-model grant includes the
model identity and pool scope. Defaults and `x-mosaic-cost-center` only select among authorized
grants; neither creates membership or permission.

The portal distinguishes Automatic access from an individual approval and distinguishes approved
from applied access. A separate cost-center creation request asks an administrator to configure a
center, not to fabricate a model grant. Its approval can link to a saved center that still needs
group setup. See [requests](cost-centers.md#grants-requests-and-approvals).

## Published MCP server access

Use **MCP servers** to publish a registered streamable MCP server through a managed API Management
gateway. MOSAIC creates a passthrough MCP API, a backend, an enforcement fragment, an API policy,
and a protected-resource-metadata operation on one shared, blank-path API per gateway. It owns those
resources under the same reviewed plan, explicit apply boundary, and
[environment rules](environments.md) as model
publishing. See [Publish MCP servers through API Management](publish-mcp-servers.md).

In Commercial, published MCP servers accept Entra runtime tokens only. People and agent users request
`api://<model-runtime-client-id>/Mcp.Invoke`. Applications, managed identities and agent identities
request `api://<model-runtime-client-id>/.default` and need `Mcp.Invoke.Application`. The gateway
checks direct grants first, then matching Entra security-group grants from the token's `groups`
claim, and applies call limits per direct caller or per group member. Custom-center individual
and group token paths both require the center's group ID. The center's mode applies to this exact
MCP offering independently of its models. Token limits do not apply to MCP servers.

Government MCP offerings instead use direct-grant subscription keys and do not accept Commercial
Entra runtime tokens. See [Government runtime access](azure-government.md).

The gateway strips caller credentials before forwarding and attaches its own managed identity when
the registered MCP endpoint uses managed identity. It never passes the caller's bearer token or an
APIM subscription key to the upstream server. API-key upstream MCP servers and SSE-only upstreams
are not published in this phase.

Interactive clients discover sign-in through the gateway's `WWW-Authenticate` challenge and the
protected resource metadata document. A VS Code entry is just an HTTP MCP server with the published
URL:

```json
{
  "servers": {
    "mosaic-example": {
      "type": "http",
      "url": "https://<gateway-host>/<api-path>/mcp"
    }
  }
}
```

See [Connect to MCP servers published through MOSAIC](connect-to-mcp-servers.md) for people,
agent identities, agent users, security groups and troubleshooting.

An MCP server whose tools call governed models through MOSAIC does so as its own application, on
that application's separate model authorization, which can be on a model API or on a model in a governed
[model pool](model-pools.md). Name that application on the published server, under
**Calls models as** on the MCP servers page, and the server receives each call's reference to pass
on, so its model calls can be attributed to the people it serves without giving them a grant on
the model. Analytics, the chargeback, and each person's **Usage & cost** then show that use by
person. See [MCP servers that call models](mcp-servers-that-call-models.md) and
[ADR 0025](adr/0025-mcp-model-calls-on-a-persons-behalf.md).

The client needs its MCP authorization and invocation permission; the server's application needs
its model authorization and invocation permission, including membership in the model grant's
custom-center group when it uses JWTs. The two legs can charge different eligible centers. Neither
an MCP grant nor the attribution header grants the application model access.

**Unpublish** on the MCP servers page opens the same review as for models, and its routes are
`POST /mcp-publications/{id}/unpublish-plan` and `POST /mcp-publications/{id}/unpublish?plan={planId}`.
The review lists the grants whose runtime credentials the gateway stops accepting. The portal lists a
published MCP server only once its first apply has put its MCP API in API Management, and not after
it is unpublished.

## Recovering an interrupted operation

Write locks do not expire automatically: a timeout or a new API instance does not prove an old
worker or its submitted ARM operations have stopped. **Check recovery status** performs diagnostics
only. It first reads `GET /api/v1/publications/{id}/lock`, so it also works for a retained metadata
mutation without a publish-run record. A missing lock does not itself prove runtime access.

An administrator can diagnose the exact returned `ownerId` using:

```text
POST /api/v1/publications/{id}/recover
{"runId":"<exact-owner-id>","confirmQuiesced":false}
```

Before submitting `confirmQuiesced:true`, the operator must stop the original worker and confirm
that **every ARM operation it submitted has reached a terminal state**. Do not infer this from a
restart, elapsed time, or an `interrupted` status. Confirmed recovery establishes denial where
needed, preserves ownership, and releases the lock only after durable recovery results. Refetch
the publication afterward and review a fresh plan; failure to establish safe state remains
unknown/locked. This is not an automatic retry, and the UI never sends that confirmation.
