# ADR 0007: MOSAIC connects to an MCP server to govern it

**Status:** Accepted

## Context

MOSAIC governs models two ways and MCP servers only one.

| | Adopted from a gateway | Registered directly |
| --- | --- | --- |
| Models | `ModelApi` (ADR 0005) | `ModelEndpoint` (ADR 0006) |
| MCP | `McpServer` (ADR 0005) | **missing** |

An MCP server could only become governable once someone had already published it through API
Management. That is backwards: an administrator who runs an MCP server — hosted in Azure, bought
from a vendor, or built in-house — had nowhere to put it, and the roadmap's next phase is to push
governed servers *into* a gateway, which requires holding them first.

The gap is sharper than symmetry. API Management's management plane returns only `name`,
`displayName`, `description`, and `operationId` per tool. There is **no input schema, no output
schema, and no annotations** anywhere in the ARM contract. Governing what a tool can actually *do*
requires the tool's own declaration, and only the server can supply it.

## Decision

**Registering an MCP server means connecting to it.** `McpEndpoint` is a record in `desired-state`
holding a URL, an authentication mode, access state, capabilities, and an inventory summary.
MOSAIC connects as a minimal read-only MCP client — `initialize`, `notifications/initialized`,
`tools/list` — and mirrors the tools into `observed-state`. It never calls `tools/call`, and there
is no code path that could add one by accident.

This is the first time MOSAIC makes an outbound call to a host it did not derive from an Azure
resource ID, so the rest of this record is mostly about that boundary.

**One access relationship, not two.** ADR 0006 gave model endpoints two planes because a gateway's
managed identity needs an Azure *role* to call a model, and role assignments are readable over ARM.
Neither half holds here: API Management fronts an MCP server with a backend credential rather than
a role assignment, and Entra app roles are not readable over ARM at all. A gateway-runtime verdict
would be `notEvaluated` in nearly every case, which is worse than not claiming to answer. That
relationship belongs with phase 4, where publishing a server through a gateway is implemented.

**Preflight is a live call, and it says so.** A model endpoint has a control plane, so MOSAIC can
ask ARM whether it may read a resource without touching it. An MCP server has none: the only way to
answer "can MOSAIC read this" is to connect. Preflight therefore performs the handshake and stops,
recording the negotiated protocol version, transport, and server identity. Discovery is a separate,
explicitly triggered sync, so preflight stays cheap enough to run on every registration.

**MOSAIC implements the handshake era of the protocol, and an unknown revision is a capability.**
The current MCP specification, `2026-07-28`, is *stateless*: it removed the `initialize` handshake,
the session header, and the GET stream outright. Everything from `2025-11-25` back is the handshake
era, and the handshake era is what API Management speaks — Microsoft documents no support for the
stateless one.

MOSAIC therefore offers `2025-11-25` and accepts a counter-offer from `{2025-11-25, 2025-06-18,
2025-03-26, 2024-11-05}`. A server answering with anything else — including a modern stateless
server — is recorded as `unsupportedProtocol`, held apart from the failure states, because "this
server speaks a dialect MOSAIC does not" is not something an operator clears by retrying. This is
exactly ADR 0005's treatment of an API Management service too old for MCP.

One protocol detail is easy to get wrong: the `MCP-Protocol-Version` header was introduced in
`2025-06-18`. Sending it to a server that negotiated an earlier revision invites a `400` for a
header that revision never defined, so MOSAIC omits it below that floor.

**Streamable HTTP only.** API Management can also expose a server over the deprecated HTTP+SSE
transport, which needs a second two-endpoint transport with a long-lived GET stream. An SSE-only
server is recorded as `unsupportedTransport` rather than half-supported. This is separate from the
*response body*: a Streamable HTTP POST may legally answer with `application/json` **or**
`text/event-stream`, and both are handled, including comment lines and unrelated notification
frames arriving before the answer.

**Annotations are untrusted claims, and absent never means safe.** `ToolAnnotations` has a trap
that matters more for a governance product than anywhere else: `destructiveHint` and
`openWorldHint` default to **true**, while `readOnlyHint` and `idempotentHint` default to false. A
tool with no annotations is therefore *presumed destructive and open-world*, not safe. The
specification is also explicit that clients "MUST consider tool annotations to be untrusted".

So every hint is stored as `bool | None`, never as a defaulted `bool`; absent and false stay
distinguishable all the way to the wire and the UI, which renders "not stated". `readOnlyHint:
true` is displayed as the server's claim, never as a guarantee. The inventory summary counts tools
that *stated* they are read-only and tools that stated nothing at all, and deliberately has no
"destructive" count — a count built on the defaults would report silence as a claim.

**A capability that was not negotiated is not called.** If `initialize` does not advertise `tools`,
MOSAIC records zero tools and does not call `tools/list`, because calling an unnegotiated
capability violates a client MUST. "This server offers no tools" and "this read failed" stay
distinct: the first is a successful sync, the second exempts `observedMcpTool` from the sweep so
ADR 0004's rule that a failed read is never mistaken for a deletion continues to hold.

**The egress boundary is constrained, and its residual risk is named.** MOSAIC runs with a managed
identity, so an operator-supplied URL is a token-exfiltration surface and not merely a
request-forgery one. HTTPS is required outside local and test environments; loopback, link-local,
private, reserved, and multicast literals are refused, most importantly the instance metadata
address `169.254.169.254`; redirects are never followed, because a redirect would carry credentials
to a host nobody registered; a managed-identity token is attached only when the operator explicitly
stated the audience; and responses are bounded in bytes with a fixed cap on `tools/list` pages.

MOSAIC has no VNet integration today, so refusing private addresses costs nothing and removes the
worst case. Two limits are stated rather than hidden: literal-address checks do not defeat DNS
rebinding, and only an Admin can register an endpoint, which is the real control.

A URL carrying userinfo — `https://alice:secret@mcp.example.com/mcp` — is **rejected**, not
stripped. Stripping it silently would persist and echo the credential in the stored record while
never actually sending it, which is the worst of both outcomes. The record itself always holds the
canonical URL rather than the submitted one, so what MOSAIC dials and what it shows an
administrator cannot diverge.

**An incomplete read is never allowed to look complete.** `tools/list` paging stops at a fixed cap.
If the cap is reached while the server is still offering a cursor, MOSAIC raises rather than
returning the partial list: a truncated read reported as a successful one would let the sweep
delete every tool past the cap, defeating the very rule the bound exists to protect. The run is
recorded as partial, `observedMcpTool` is exempted, and the previous documents stand. The same
reasoning applies to a failed sync's *access verdict*: it moves with the status, because a stale
"MOSAIC can read this" beside `unauthorized` would hide the challenge the server sent and keep
offering a sync it had just refused.

**Credentials stay Key Vault URIs, and this is where that path finally runs.** `McpAuthMode` is its
own enum rather than `EndpointAuthMode`, because an MCP server may legitimately need no credential
and that must never become a valid way to register a *model* endpoint. MOSAIC stores only a secret
identifier and resolves the value at call time. ADR 0006 modelled this and did not implement it;
this is the first code path that reads a secret value. The API app already holds **Key Vault
Secrets User** and already receives `MOSAIC_KEY_VAULT_URI`, so no Bicep or RBAC change was needed.

A static bearer token is not OAuth 2.1, which is what the MCP authorization specification defines.
It is permitted under the clause allowing clients and servers to "negotiate their own custom
authentication and authorization strategies"; full OAuth 2.1 with RFC 9728 discovery is out of
scope. A `401` is never reported as unreachable: MOSAIC parses `WWW-Authenticate` for
`resource_metadata` and `scope` and records `unauthorized` with what the server asked for.

**Two renames, both wire-compatible.** `McpEndpoint` was a value object holding API Management's
named URI template for a server; it is now `McpServerRoute`, which is what it actually is, freeing
the name to mirror `ModelEndpoint` exactly. `ObservedModelEntity` is now `ObservedEndpointEntity`.
Both are plain class renames — the value object has no `entity_type` and is embedded under the
unchanged field `endpoints`, and the observed base's wire field is still `endpointId` — so neither
needed a Cosmos migration.

**The third observed scope forced a generalisation, and it was free.** ADR 0006 said a third
observed scope "should prompt a real generalisation with a migration, rather than a third copy".
The generalisation turned out to need no migration: the sweep already keys on `endpointId`, and an
MCP endpoint *is* an endpoint. `ObservedMcpTool` is a sibling of `ObservedModelDeployment` under
the renamed base, and the endpoint-scoped repository methods now live in a store shared by the
model and MCP repositories.

## Consequences

- Administrators can govern an MCP server before any gateway fronts it, which is what the next
  phase needs in order to publish one into API Management.
- MOSAIC records tool input schemas, output schemas, and annotations that exist nowhere in the API
  Management management plane. That is the concrete reason the live call is worth its cost.
- MOSAIC now makes outbound calls to operator-supplied hosts. The blast radius is bounded by the
  admission rules above, but the property "MOSAIC only talks to ARM" no longer holds and should not
  be assumed anywhere else.
- Tool annotations are stored as claims, so any future feature that gates on them — an entitlement
  that only permits read-only tools, say — must treat them as untrusted input, not as facts.
- MCP support is pinned to the handshake era. When API Management adopts the stateless protocol,
  a server will begin reporting `unsupportedProtocol` and this decision needs revisiting rather
  than patching.
- Which revision API Management's MCP gateway actually negotiates is not documented by Microsoft.
  The handshake era is inferred from its ARM contract and tooling. The fallback is already correct
  behaviour, but the inference is worth an empirical check against a real service.
- A registered `McpEndpoint` and an adopted `McpServer` can describe the same server. Nothing
  reconciles them yet, which is the same drift ADR 0005 already accepted for `ModelApi`.
