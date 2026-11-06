# Gateway onboarding and inventory

[Documentation](README.md) | [MOSAIC](../README.md)

- [Gateways](#gateways)
- [MCP servers](#mcp-servers)
- [Importing models and MCP servers](#importing-models-and-mcp-servers)
- [Registering MCP servers](#registering-mcp-servers)
- [Policies without markup](#policies-without-markup)

## Gateways

A gateway is an existing Azure API Management service that an administrator registers with MOSAIC by
resource ID. MOSAIC supports several across subscriptions and environments; the APIM that `azd`
deploys is registered automatically on first startup and also appears as a one-click suggestion.
Each gateway belongs to one environment, or is Unclassified; see [Environments](environments.md).

Onboarding runs a preflight against Azure Resource Manager with MOSAIC's managed identity. It reads
effective permissions at the resource scope, and when they are missing it reports the exact role,
scope, and `az role assignment create` command an operator needs. MOSAIC cannot grant itself that
role, so it explains rather than fails silently.

The needed roles are:

| Purpose | Role | Role definition ID |
| --- | --- | --- |
| Observing a gateway | API Management Service Reader Role | `71522526-b88f-4d52-b57f-d31fc3546d0d` |
| Publishing models into a gateway | API Management Service Contributor | `312a565d-c81f-4fd8-895a-4e21e48d571c` |

A gateway MOSAIC can only read is fully usable for observation. Writing requires two independent
conditions: the contributor role above, and an administrator switching the gateway from `observe` to
`manage`. MOSAIC refuses the switch until preflight has actually confirmed write access, and refuses
every write to a gateway left in `observe` mode however the role is assigned.

Administrators switch modes with **Management mode** on the gateway's Overview tab, and confirm each
direction. The switch itself changes nothing in API Management; it only records the mode in MOSAIC.
**Manage** stays disabled until the access check confirms write access. Until then, the page shows the
role, scope and identity to grant, and **Check access** runs the preflight again. In `manage`,
MOSAIC writes to the gateway only when an administrator applies a reviewed publish or unpublish
plan, or confirms recovery of an interrupted apply. Switching back to `observe` leaves published
models in API Management, where they keep serving calls. MOSAIC then refuses to plan, apply or
unpublish them, so grant and access changes saved in MOSAIC wait until the gateway is managed again.
Either switch is refused while an apply or unpublish on one of the gateway's publications is still
running, or is awaiting recovery after an interruption.

Two scoped writes are exceptions to a publication-plan apply: owned-API diagnostics/logger setup
and budget blocked-list updates on managed gateways. Both are audited; neither changes the
observation-only boundary. See [architecture](architecture.md#three-paths-three-consistency-boundaries).

The contributor role also grants `subscriptions/listSecrets`. MOSAIC uses that capability only
for an authorized, explicitly requested reveal of an applied, owned grant's key. A custom role
that permits writes but excludes that action cannot reveal keys; the API reports the missing
key-read action separately. This is a product authorization boundary, not a claim that MOSAIC's
managed identity is technically unable to read secrets.

Synchronisation collects APIs and their operations, MCP servers and their tools, products,
subscriptions, gateway users and groups, backends, named value metadata, and policies at the
service, product, and API scopes. Operation-scope policies are read on demand rather than during a
full sync. A failure reading one collection degrades the run to `partial` and is recorded, rather
than discarding the whole snapshot — and the entity types it could not read are exempt from the
stale-document sweep, so a transient failure never looks like a deletion.

## MCP servers

API Management models an MCP server as an API of type `mcp`, visible only on management API version
`2025-09-01-preview` or later. MOSAIC keeps its inventory on the stable `2024-05-01` contract and
uses the preview version for MCP discovery alone, so a preview API that changes cannot break the
sync administrators depend on.

A service that rejects the preview version is not a failure. MOSAIC records
`capabilities.mcpServers` as `unavailable`, the run still succeeds, and the MCPs workspace explains
why the gateway has nothing to offer. Any other read failure is reported as an error and exempts
MCP servers from the sweep, keeping "MOSAIC could not read this" distinct from "there are none".

## Importing models and MCP servers

Observed state is disposable and rebuilt on every sync. Importing promotes a selection of it into
`desired-state` as `ModelApi` and `McpServer` records: MOSAIC now governs these, and the records
survive the sweep.

Importing is a Cosmos write and nothing else. No API Management resource is created, changed, or
deleted, no policy is written, and no Azure write API is called. Import does not request or grant
the contributor role. See [ADR 0001](adr/0001-apim-runtime-boundary.md) for the runtime boundary.

Detection decides which rows arrive pre-checked, not which imports are allowed. Every observed API
is offered, an administrator can adopt one MOSAIC did not recognise or skip one it did, and the
record keeps whether the choice was `detected` or `manual`. A name absent from the gateway's most
recent snapshot is rejected outright rather than skipped, because importing four of five selected
APIs and reporting success would leave an administrator believing they had governed something they
had not.

Record IDs are deterministic, so re-importing after a sync refreshes a record in place instead of
duplicating it. Deleting a gateway deletes what was imported from it.

MCP servers are detected too. API Management models an MCP server as an API with `type: mcp`, which
is only visible from management API version `2025-09-01-preview`, so MOSAIC issues that one call
separately from the stable version it pins everywhere else. A service tier or release channel
without that version is not a fault: MCP visibility is reduced and the sync still succeeds.

## Registering MCP servers

Importing covers servers a gateway already hosts. Registering covers the rest: an administrator
gives MOSAIC a Model Context Protocol server URL, and MOSAIC connects to it directly. That matters
for two reasons — a server can be governed before any gateway fronts it, which is what publishing
into API Management will need, and the management plane cannot describe a tool properly. ARM
returns only a name, display name, description, and backing operation. **Input schemas, output
schemas, and behaviour annotations exist nowhere in it**, and only the server can supply them.

MOSAIC acts as a minimal read-only MCP client. It performs `initialize`, sends
`notifications/initialized`, pages `tools/list`, and ends the session. **It never calls a tool.**

An MCP server has no control plane, so unlike a model endpoint there is no way to ask "may MOSAIC
read this" without connecting. Registration therefore runs the handshake and stops; discovering
tools is a separate, explicitly requested sync.

MOSAIC implements the protocol's *handshake* era. The current revision, `2026-07-28`, is stateless
— it removed the handshake, the session header, and the GET stream — while API Management speaks
the handshake era. MOSAIC offers `2025-11-25` and accepts a counter-offer down to `2024-11-05`. A
server outside that range is recorded as `unsupportedProtocol`, and an SSE-only server as
`unsupportedTransport`. Both are capabilities, not failures: neither is something an operator fixes
by retrying. A server that does not advertise a `tools` capability records zero tools without
`tools/list` ever being called, which stays distinct from a read that failed.

Because this is MOSAIC's first outbound call to a host it did not derive from an Azure resource ID,
the boundary is deliberate. HTTPS is required outside local development; loopback, link-local, and
private addresses are refused, including the instance metadata address; redirects are never
followed; a managed-identity token is attached only to an audience the operator explicitly named;
and responses and page counts are bounded. Only a Key Vault secret *URI* is stored, resolved at
call time. `401` is reported as "needs authorization", with the scope and resource metadata URL the
server asked for, never as "unreachable".

Tool annotations are recorded as the **server's claims**, never as MOSAIC's findings. The
specification defaults `destructiveHint` and `openWorldHint` to *true* and requires clients to
treat annotations as untrusted, so an absent hint is stored and displayed as "not stated" rather
than as its default — a tool that said nothing is never rendered as if it promised to be safe.

## Policies without markup

MOSAIC parses API Management policy XML in memory and keeps only a SHA-256 digest and redacted
semantic facets. Administrators see sentences:

> Limits model usage to 10,000 tokens per minute, counted per subscription.
>
> The gateway authenticates to `https://cognitiveservices.azure.com` with its own managed identity.

Rules MOSAIC does not interpret are labelled **externally authored** and counted by name. They are
never hidden, and never shown as markup. Policy documents routinely carry inline credentials, so not
storing the source is a security property as well as a product decision. URLs lose their query string
before they are stored or displayed, because backend URLs commonly carry Azure Functions keys and
storage SAS tokens there.

For publications it owns, MOSAIC authors named `mosaic-*` policy fragments and the owned API policy
that includes them, through the reviewed-plan and explicit-apply workflow. Importing an existing
customer-authored API does not retrofit these policies or rewrite that API. Fragments are also
inventoried so operators can inspect the ownership boundary.
