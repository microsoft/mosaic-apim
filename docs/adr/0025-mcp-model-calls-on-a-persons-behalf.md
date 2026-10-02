# ADR 0025: Attribute an MCP server's model calls to the person it serves

**Status:** Accepted. Amends [ADR 0017](0017-mcp-gateway-enforcement.md) and
[ADR 0019](0019-usage-telemetry.md).

## Context

A person calls an MCP server that MOSAIC publishes, with their own Entra token and their MCP grant
([ADR 0017](0017-mcp-gateway-enforcement.md)). A tool on that server can then call a governed model
through MOSAIC. It does so as the server's own application, with that application's own model
grant. So MOSAIC attributes the model's usage to the application, and nobody can see whose tool
calls drove it. Chargeback by person stops at the server.

The owner decided on 2026-10-02:
- The MCP server is the access point. Access, limits and the cost center for its model calls come
  only from its application's own grant (app role `Models.Invoke.Application`). The person needs no
  grant on the model. On-behalf-of token exchange, which would need one, was rejected.
- The server passes on who called it. MOSAIC trusts that only from approved intermediaries,
  records it for usage, and never uses it for access.

These constraints shaped the design:
- MOSAIC stays out of the traffic path ([ADR 0001](0001-apim-runtime-boundary.md)). Usage comes
  from each gateway's own resource logs, which MOSAIC reads one gateway at a time
  ([ADR 0019](0019-usage-telemetry.md)).
- No caller identity header decides access ([ADR 0011](0011-governed-model-access.md)).
- API Management fails a call when a trace property is empty (the
  [ADR 0019 amendment](0019-usage-telemetry.md#amendment-2026-10-01-trace-properties-are-never-empty)).
  It runs only the .NET types it documents for policy expressions. MOSAIC's test double of API
  Management parses expressions without running them, so a unit test can't prove the gateway
  accepts one: G17 failed that way.
- An MCP policy never reads a body, or streaming breaks ([ADR 0017](0017-mcp-gateway-enforcement.md)).

## Decision

**A model call names the MCP call it serves, and MOSAIC finds the person when it reads the logs.**

1. **The MCP server receives its call's reference.** On an MCP server with a model caller, which is
   described below, the enforcement fragment matches the caller's grant as before. It then sets
   `x-mosaic-on-behalf-of` to the call's own request ID, `context.RequestId`, after removing any
   copy the caller sent. Its attribution trace adds `r=<request ID>` and
   `i=<model caller's object ID>`. Every MCP server's fragment removes a caller's copy, with or
   without a model caller, so a server only ever receives a reference the gateway set.
2. **The server passes it on.** The server copies the header, unchanged, onto each model call it
   makes while serving that request. It calls the model through the same gateway, as its own
   application, with an Entra token.
3. **The model records it, and nothing else.** Every governed model fragment reads the header only
   when the call presented a validated application token: no real `scp`, and
   `Models.Invoke.Application` in `roles`. The value must be exactly one GUID.
   - The attribution trace adds `r=<reference>`, lowercased. It's `!` for anything else an
     application sent, so a builder can see it's wrong, and empty otherwise.
   - The header is then removed before the model, with the caller's credentials and the cost-center
     header.
   - The reference never decides anything. A bad one is ignored or marked, never refused, and an
     error reading it records nothing.
4. **MOSAIC checks it when it reads the logs.** When the rollup reads a gateway's logs, it joins
   each model call that names a reference to the admitted MCP call on that gateway whose trace
   carries the same `r=`. It attributes the model call to that MCP call's caller only when both of
   these hold:
   - The MCP call's trace names a model caller (`i=`), and the model call's caller is that
     application. The model call's caller is its grant's subject, or a security-group grant's
     validated member.
   - The model call happened during the MCP call: within its `TotalTime`, plus 5 minutes either
     side. That holds whether `TimeGenerated` marks a call's start or its end.

   The person is the MCP call's validated member, or its grant's subject, which MOSAIC already
   resolves through the attribution registry.

A person who calls the model directly carries a delegated token, so the model records no
reference. Even if it did, the rollup would require the caller to be the server's application.
Another application can't use a reference either, because the MCP call names its own model caller.
A reference is a gateway request ID that only the server ever sees, and it's valid only while its
MCP call runs. Like any design that trusts an intermediary, MOSAIC can't stop an approved server
from misattributing a model call among the people who called it while their calls ran.

**Each MCP publication can name the application its tools call models as.**
- `PUT /api/v1/mcp-publications/{id}/model-caller` with `{"principalId"}` names a MOSAIC principal
  that signs in as an application: a service principal, a managed identity or an agent identity,
  with a GUID object ID. `DELETE` on the same route clears it.
  - Both take the publication's lock, and wait for an interrupted apply to be recovered.
  - Each change is audited as `mcpPublication.modelCallerChanged`, with the previous and new
    principal. Naming the same principal again changes nothing.
  - Like any edit, a change discards the saved plan.
- One model caller per MCP publication. One application can serve several.
- A plan compiles the model caller into the access snapshot, as `modelCaller`: its principal ID,
  object ID and name. The snapshot and the policy, which carries `i=` and the header, are both in
  the publication's digest. A change therefore makes the saved plan stale, and the applied snapshot
  shows what's live.
- **Model publications are unaffected.** Their fragment reads references the same way whoever
  calls. The rollup's check that the caller is the application the MCP server names is what makes
  the link tight. So naming or clearing a model caller changes one publication's plan, not every
  model the application can call.
- **Plan warnings:**
  - When MOSAIC can no longer name the principal as an application, for example because it was
    deleted or is now a person, the plan compiles no model caller and says so. Attribution is never
    a reason to hold back the server's own access.
  - When the application holds no enabled direct grant on a model this gateway publishes, the plan
    warns that its model calls will be refused, unless a security group gives it access. It also
    says MOSAIC attributes only calls made through this gateway.
- MOSAIC refuses to delete a principal that an MCP server calls models as, or to change it to a
  kind that isn't an application. The refusal names the publication. The setting is cleared on the
  server first, where the change is planned and applied.
- The console shows it on the MCP server, as **Calls models as**, with whether it's applied. The
  Identity page marks each application an MCP server calls models as.

**What MOSAIC records.**
- **Traces.** `v=1` stays, because only keys are added, and readers ignore keys they don't know.
  - A governed model's trace reads
    `mosaic-attribution v=1 g=<grant> m=<member> a=<client> r=<reference>`.
  - An MCP server's trace adds `r=<request ID> i=<object ID>` when it has a model caller, and is
    unchanged otherwise.
  - Application Insights gets the properties `mosaic-mcp-call` and `mosaic-model-caller`. Like
    every property, each is written through `append_trace_metadata`, so each records `-` rather
    than an empty value.
- **Rollups.** A model call's fact gains, when it was attributed:
  - the person's object ID;
  - the MCP grant's key;
  - the MCP server's API.

  Each is left out of the document while empty. Two summary dimensions follow:
  - `onBehalf`, keyed by the application's grant link, the person and the MCP API;
  - `onBehalfUnresolved`, keyed by the grant link and why a reference couldn't be used:
    `malformed`, `missing`, `late`, `caller` or `unknown`.

  The existing dimensions keep their meaning. The application's calls are still its own: it's
  still their caller, their grant and their client.
- **Reports.**
  - Analytics lists each person's model use through MCP servers, by MCP server and application,
    with their cost. It also counts the references it couldn't use, by reason. People and
    Applications don't change, so nothing is counted twice.
  - A person's portal Usage & cost shows their own model use through MCP servers, and nobody else's.
  - The chargeback CSV gains **On behalf of** and **On behalf of object ID** after **Cost center
    name**. It splits the application's grant rows by person, and leaves the columns empty for
    the application's own use.

**The application's grant pays.** A model call through an MCP server is charged to the cost center
of the application's own grant, as before, and the person is a reporting dimension. The gateway
enforces the application's limits, pools and budget block as the call arrives, before MOSAIC
knows the person. Charging the person's cost center would let a header move cost without access
following it.

**The contract for server builders.** For each MCP request that carries `x-mosaic-on-behalf-of`:
- Copy the value onto every model call made while serving that request.
- Call the model through the same MOSAIC gateway, with an Entra token for the server's own
  application (`api://<runtime>/.default`, app role `Models.Invoke.Application`), not a key.
- Treat the value as opaque. Don't parse it, store it, change it, reuse it for another request,
  or use it to decide anything. It grants nothing.

[MCP servers that call models](../mcp-servers-that-call-models.md) is the guide.

## Consequences

- People's model use through an MCP server can be reported and charged back by person, with no
  model grant for them and no change to how access is decided.
- **Only calls through the same gateway are attributed.** MOSAIC reads each gateway's logs on
  their own, so a model call through another gateway keeps its reference in its log, but MOSAIC
  can't find the MCP call it names. The plan says so. The header format allows the rollup to look
  across gateways later, without changing any policy.
- **Attribution follows the MCP call's log row.** It arrives when the MCP call ends, usually
  minutes later, and the rollup reads today and yesterday again each cycle. The application's cost
  center is charged regardless; only the split by person waits. A model call whose MCP call MOSAIC
  can't find, or that ran outside it, stays the application's own use, and its reason is counted.
- **One hop only.** Every MCP server removes a caller's reference. A tool that calls another MCP
  server, which then calls a model, is attributed to the first server's application.
- **Each publication records references only after its next apply.** Every governed model and MCP
  fragment changes, to read or remove the header and to add the trace keys. Until a model is
  re-applied, its fragment passes the header to the backend. The header carries only a request
  ID, never personal data, and no MCP server sets it until its own re-apply after naming a model
  caller. MOSAIC is a proof of concept, so nothing is grandfathered.
- Publications without governed access record no attribution, and leave the header alone, as
  they leave every other header.
- Rollups hold one more personal identifier: the person each attributed model call was for. It
  sits beside the application's, in the same container, under the same retention.
- New fact fields and summary dimensions exist only once a reference has been attributed. A
  release from before them can't read those documents, so before rolling back past them, delete
  the `onBehalf` and `onBehalfUnresolved` summaries, and the facts that carry an on-behalf person.

## Alternatives considered

- **Pass the person's identity, signed by the gateway (HMAC).** The MCP fragment would set a
  header carrying the person's object ID, a timestamp and the model caller, signed with HMAC-SHA256
  under a key the gateway holds. Each model fragment would check the signature and freshness, and
  that the caller is the named application. This would attribute at write time, with no join.
  It's feasible: `HMACSHA256`, `Convert`, `Encoding`, `DateTimeOffset` and `Guid` are on API
  Management's documented list of types for policy expressions.
  `CryptographicOperations.FixedTimeEquals` isn't, so a constant-time comparison would be
  hand-written. Rejected, because:
  - **It needs a secret.** A Key Vault-backed named value, as ADRs 0018 and 0021 use, could be
    shared by every gateway. But every gateway would then need access to MOSAIC's vault, or every
    governed apply on it would fail, and a rotation reaches each gateway at a different time,
    within four hours, unless MOSAIC refreshes each one. A secret per gateway that MOSAIC generates
    and never stores rotates instantly, but attributes only within one gateway, the same limit
    this design has.
  - **Every governed model policy would carry cryptography.** If API Management refused it at
    runtime, as it refused G17's expressions, every governed model would fail at once.
  - **The person's object ID would leave MOSAIC.** It would go in clear to every MCP server, and,
    until each model is re-applied, to every model backend.
  - **Replay would be bounded only by a freshness window, such as 15 minutes.** A reference is
    bounded by its own MCP call.
- **On-behalf-of token exchange.** Rejected by the owner: the person would need a model grant.
- **Trust a plain identity header from approved intermediaries.** It needs no join, but an
  approved server could then name anyone in the tenant, not just its own callers.
- **Look the MCP call up in API Management's cache at the model** (`cache-store-value` and
  `cache-lookup-value`). That needs no secret and attributes at write time, but the built-in cache
  is per region, volatile, and missing on the Consumption tier, so attribution would quietly come
  and go.
- **A global "intermediary" flag on a principal.** It would let the application vouch for callers
  of any MCP server. The link per publication says which server's callers it may name, and is
  reviewed with that server's plan.
- **Compile the approved applications into each model's policy.** The model would refuse to record
  references from other applications at write time. But naming a model caller would then change
  the plan of every model the application can call. The read-time check is as tight without that
  coupling.
- **Name the model caller on the registered MCP endpoint rather than its publication.** The
  publication is where MOSAIC plans, reviews and applies gateway behavior, and different gateways
  may publish the same server differently.

## Live verification still required

Unit tests run the policies through a double of API Management that parses expressions, and the
queries against a double of Log Analytics. On a real gateway, journey M9 should confirm that:
- API Management saves and runs both changed fragments:
  - the MCP fragment's header from `context.RequestId.ToString()`;
  - its trace keys and properties;
  - the model fragment's reference expression, with its regex, `Jwt` claims and `try`/`catch`.
- the MCP backend receives the gateway's value, and a value the caller sent is replaced, or removed
  when the server has no model caller;
- the model backend never receives the header;
- `TraceRecords` shows the MCP call's `r=` and `i=` and the model call's matching `r=`, and
  Application Insights shows `mosaic-mcp-call` and `mosaic-model-caller`, with `-` where a call has
  none;
- the rollup's join runs on Log Analytics, and `TimeGenerated` and `TotalTime` bound a model call
  by its MCP call as expected;
- the person's model use shows in Analytics, in the person's portal and in no one else's, and in
  the chargeback CSV;
- a person's direct call with the header records nothing, another application's reference counts
  as `caller`, a made-up GUID as `missing`, and a malformed value as `malformed`.
