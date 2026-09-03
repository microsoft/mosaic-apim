# ADR 0005: Adopt gateway resources by writing to Cosmos, never to Azure

**Status:** Accepted

## Context

After onboarding, MOSAIC could describe a gateway but do nothing with it. The Gateways workspace
reported "AI APIs 1 of 2" and offered no next step, because observed state is disposable: it is
rebuilt on every sync and swept of anything the newest snapshot did not contain. Nothing an
administrator did to it could survive.

Governance needs the opposite property. Saying "MOSAIC governs this API" is a durable statement of
intent that must outlive the observation that produced it, and must not silently disappear when a
sync is throttled or an API is briefly renamed.

Separately, the nav labelled this area **Model Foundry**, which reads as Microsoft Foundry the
product rather than "the models MOSAIC governs", and API Management now hosts MCP servers, which
had no home in the product at all.

## Decision

**Importing writes to Cosmos and never to Azure.** Adoption creates a `ModelApi` or `McpServer`
record in `desired-state`, with an audit event, describing an API Management resource that already
exists. MOSAIC creates no resource, changes no policy, and calls no Azure write API. ADR 0001 is
unaffected and the API Management contributor role stays ungranted. The word "import" describes
what an administrator experiences; what it does is record intent.

**Detection recommends; the administrator decides.** Classification pre-checks the rows MOSAIC
recognises as fronting a model, and does nothing else. Every observed API is offered, including
ones MOSAIC cannot classify, because a customer fronting a model MOSAIC has never seen still needs
to govern it. The record keeps `selection` as `detected` or `manual`, so a later reader can tell
which choices were the product's and which were a human's.

This is why classification stays conservative. A false positive silently pre-checks the wrong API,
so only distinctive markers qualify: provider hosts, and operation paths like `/converse` or
`:generateContent`. Paths common in ordinary REST APIs, such as `/messages`, are deliberately
excluded even though some providers use them.

**Detection covers non-Azure providers.** `AiBackendKind` gained `openAi`, `anthropic`,
`googleVertex`, and `awsBedrock` alongside the Azure kinds, with `otherLlm` still the catch-all. A
gateway fronting Bedrock is exactly as much an AI gateway as one fronting Azure OpenAI, and naming
the provider is more useful to an operator than reporting "other".

**A selection MOSAIC cannot resolve fails the whole request.** If any requested name is absent from
the gateway's most recent snapshot, nothing is imported. Adopting four of five selected APIs and
reporting success would leave an administrator believing they had governed something they had not,
which for a governance control plane is worse than an error.

**Record IDs are deterministic.** Re-importing after a sync refreshes the record in place rather
than creating a second one, so an administrator repeating an import cannot silently fork MOSAIC's
view of the same API. Deleting a gateway deletes what was imported from it, because intent about a
gateway that no longer exists can never be reconciled.

**The MCP preview contract is isolated.** API Management exposes MCP servers as APIs of type `mcp`,
but only on management API version `2025-09-01-preview` or later. Rather than move the whole client
onto a preview contract, MCP discovery uses that version alone while the inventory stays on stable
`2024-05-01`. A preview API that changes or disappears can then degrade MCP discovery without
touching the gateway sync administrators depend on.

**An unsupported version is a capability, not a failure.** A service that rejects the preview
version records `capabilities.mcpServers` as `unavailable` and leaves the sync successful, because
"this gateway is too old for MCP" is not something an operator can fix by retrying. Every other
failure still routes through the normal guard: it is reported, and MCP servers are exempt from the
sweep, so ADR 0004's rule that a failed read is never mistaken for a deletion continues to hold.

**Models and MCPs are separate workspaces.** `Model Foundry` is renamed `Models`, and MCP servers
get their own section rather than being listed as APIs. They are a different product surface —
tools and a transport rather than operations and HTTP verbs — and MCP-typed APIs are excluded from
the observed API list so one resource never appears twice.

**One import workflow, reachable from wherever the administrator is.** Models and MCPs each own an
import dialog, and the gateway detail page hands off to them by route rather than duplicating the
flow. An administrator who finds an API while inspecting a gateway should not have to learn a
second way to adopt it.

## Consequences

- Administrators can put a specific API under MOSAIC governance without MOSAIC touching Azure, so
  adoption carries no runtime risk and needs no new role assignment.
- Because detection only pre-checks, its coverage is a convenience rather than a gate. Adding a
  provider improves the default selection and can never be the reason an API cannot be governed.
- Imported records can drift: an API deleted in API Management leaves a `ModelApi` behind that no
  longer matches the newest snapshot. Nothing reconciles that yet, and detecting it is future work.
- MCP support depends on a preview API version. If it changes incompatibly the MCPs workspace
  reports nothing rather than breaking, but MCP discovery will need revisiting when the contract
  stabilises.
- `desired-state` now holds records derived from observation. They are still administrator-authored
  in the sense that matters — a human chose each one — but their content is a point-in-time copy,
  and refreshing it requires a re-import.
