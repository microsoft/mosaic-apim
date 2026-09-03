# ADR 0006: Model endpoints have two access relationships, not one

**Status:** Accepted

## Context

ADR 0004 gave MOSAIC a gateway registry. A gateway on its own governs nothing: the thing an
administrator actually wants to allocate is a *model*, and models live on Azure OpenAI and Azure AI
Foundry endpoints that API Management fronts. Until MOSAIC can hold those endpoints and read what is
deployed on them, entitlements have nothing to point at.

The Model Foundry workspace looked like it already did this. It did not. It was browser state that
fabricated resource IDs, discarded any submitted secret, and called no API. There was no endpoint
route, service, or repository, and the `FoundryConnection` entity in the domain had no
implementation behind it.

Gateway onboarding models exactly one access relationship: *can MOSAIC read this API Management
service?* A model endpoint has two, and they are held by different principals against different
planes:

| | Identity | Plane | Question |
| --- | --- | --- | --- |
| Onboarding | MOSAIC's managed identity | Control plane (ARM) | Can MOSAIC enumerate the models? |
| Runtime | **The gateway's** managed identity | Data plane | Can the gateway actually call them? |

Collapsing these into one verdict would hide the most common real failure: MOSAIC reads an endpoint
perfectly well, reports it healthy, and the gateway still cannot call a single model on it.

## Decision

**Endpoints are registered records, onboarded exactly like gateways.** A `ModelEndpoint` in
`desired-state` holds the resource ID, access state, capabilities, and an inventory summary.
Registration runs a preflight, sync mirrors deployments into `observed-state`, and a missing role
produces the exact role, scope, and `az role assignment create` command. MOSAIC writes nothing to
Azure AI and still writes nothing to API Management.

**MOSAIC asks for `Reader`, not a Cognitive Services role.** Every built-in role whose name begins
`Cognitive Services` or `Foundry` that grants
`Microsoft.CognitiveServices/accounts/deployments/read` *also* carries data-plane `dataActions`
— usually `Microsoft.CognitiveServices/*`, i.e. full inference — and most carry
`accounts/listkeys/action`. `Reader` (`acdd72a7-3385-48ef-bd42-f606fba81ae7`) is the only built-in
that grants the read with no data actions, no key access, and no write. Granting MOSAIC the ability
to call models or read account keys to satisfy a *listing* feature would contradict the security
model in ADR 0001.

`Reader` also grants `Microsoft.Authorization/roleAssignments/read`, which is what the gateway
runtime check needs, so one assignment covers both jobs. MOSAIC additionally emits a narrower custom
role definition operators may create instead. That role deliberately omits the role-assignment read,
so choosing it degrades runtime access to *not evaluated* — which the UI says plainly rather than
hiding.

Note the near-miss: `Microsoft.CognitiveServices/accounts/AIServices/deployments/read` is a
**dataAction**, not an action. The control-plane permission has no `AIServices` segment. Using the
wrong one silently grants inference.

**Runtime roles are matched by GUID, never by name.** Microsoft renamed the Foundry roles in 2026 —
`Azure AI User` became `Foundry User` — and states that role IDs are unchanged and that code should
bind to the GUID during the rollout. The required role is selected by resource shape:

| Endpoint | Role | GUID |
| --- | --- | --- |
| `kind: OpenAI` | Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` |
| `kind: AIServices` | Cognitive Services User | `a97b65f3-24c7-4388-baec-2e87135dc908` |
| Foundry project scope | Foundry User | `53ca6127-db72-4b80-b1b0-d745d6d5456d` |

Microsoft's own documentation conflicts here: the Foundry RBAC concept page says not to use
`Cognitive Services *` roles for Foundry, while the Foundry Models identity guide says to assign
`Cognitive Services User` at the Foundry resource scope. They address different scenarios — agents
versus model inference on the account. For "a gateway calls a deployment on an AI Services account",
`Cognitive Services User` is the documented answer, and it is what MOSAIC reports.

**"Not evaluated" is never reported as a denial.** Verifying another principal's access reads role
assignments at the endpoint scope filtered by the gateway's principal ID. When MOSAIC lacks
`roleAssignments/read`, the result is `notEvaluated` with an explanation, never `canInvoke: false`
presented as fact. The same applies to the gateway's identity itself: a gateway MOSAIC has not yet
observed reports `notEvaluated`, and only a gateway whose identity block was actually read and found
empty reports `noGatewayIdentity`. Absence of an observation is not an observation of absence.

**Role assignments are filtered by ancestry, not just equality.** `$filter=principalId eq` returns
assignments at, above, **and below** the requested scope, and RBAC inherits downward only. An
assignment on a Foundry project confers nothing at the parent account, so a scope that is not the
endpoint or one of its ancestors is rejected — and reported, because an operator who granted the role
on a project needs to know why it does not count. An exact match wins over an inherited one;
inherited grants are labelled with their origin, because a broad inherited grant and a deliberate
assignment on this endpoint are not the same governance posture.

**Foundry projects resolve upward.** A project is not a deployment container: it carries only
descriptive properties, and models are enumerated on the parent account. A registered project ID is
preserved for display and scoping but reads happen at `account_scope`.

**Credentials remain Key Vault URIs.** An OpenAI-compatible endpoint is registered with a Key Vault
secret identifier the operator created; MOSAIC stores only the URI. No Bicep or RBAC change was
needed, and the "MOSAIC stores no secret values" property holds. This is, however, the first place
MOSAIC will *read* a secret value — to call a non-Azure `/v1/models` — and that is stated plainly
rather than buried.

**Observed model state is a sibling shape, not a generalisation.** Observed gateway documents are
queried on `gatewayId` in Cosmos SQL. Introducing a shared scope field would orphan every existing
document, because a query on the new field cannot sweep documents written with only the old one.
`ObservedModelEntity` is therefore a sibling of `ObservedEntity` keyed on `endpointId`, with
parallel repository methods. This costs a little duplication and requires no migration.

**Subscription scanning is explained, not granted.** Administrators asked to discover endpoints
without pasting resource IDs, so MOSAIC offers three sources: a paste, hosts already observed as AI
backends in a registered gateway (which needs no new permission at all, because that inventory is
already in Cosmos), and a subscription-wide enumeration. The last needs `Reader` at subscription
scope. `infra/main.bicep` targets a resource group, and self-granting a subscription-wide role to
power a convenience feature is precisely the escalation MOSAIC refuses elsewhere. So the scan runs
per subscription, and one it cannot read records the remediation and is skipped rather than blanking
the whole list.

**MCP discovery belongs to ADR 0005, not here.** An earlier draft of this work added its own
MCP-server detection on the same preview API version. ADR 0005 landed a richer model first —
`ObservedMcpServer` with transports and tools, plus adoption records — so that implementation is
kept and this one was withdrawn rather than run in parallel. Two mechanisms reading the same preview
contract would be strictly worse than one.

## Consequences

- Administrators onboard model endpoints without redeploying MOSAIC, and see two separate,
  independently actionable access verdicts per endpoint.
- MOSAIC holds no inference rights and no key access on any model endpoint, which is a stronger
  position than the feature strictly required.
- Because runtime access is read rather than granted, an endpoint can be fully healthy from MOSAIC's
  point of view and still unusable through a gateway. That is surfaced, not smoothed over.
- Two observed shapes exist in `observed-state`. A future third scope should prompt a real
  generalisation with a migration, rather than a third copy.
- The Cognitive Services ARM version is pinned to `2024-10-01`, the newest with a published REST
  reference. Newer stable versions exist in the spec repo and add deployment fields MOSAIC does not
  read. That is a deliberate, reversible trade.
- Discovery for OpenAI-compatible endpoints is modelled but not implemented; those endpoints
  register and hold their credential reference without yet listing models.

