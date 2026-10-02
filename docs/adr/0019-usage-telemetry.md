# ADR 0019: Measured usage from API Management's resource logs

**Status:** Accepted

## Context

[ADR 0015](0015-end-user-usage-report.md) gave the portal a usage report whose contract came before
its data. Every figure it showed was simulated, and the console's Dashboard and Analytics showed
static samples. Its amendment made governed policies tag each call with the grant it matched, but
nothing read the tags.

People need to see how much they use against their own limits. Administrators need the estate's
totals, and who and what drives them: each person, application, agent, and security group, each
grant, model, deployment, and API, the calls that failed or were refused, and grants nobody uses.
Cost comes later, from a price list, but it can only be computed from measured usage.

API Management already records every call. A diagnostic setting sends the service's `GatewayLogs`
and `GatewayLlmLogs` categories to a Log Analytics workspace, as the `ApiManagementGatewayLogs` and
`ApiManagementGatewayLlmLog` tables. What those rows hold depends on each API's Azure Monitor
diagnostic. Several things shape how MOSAIC can use them:
- MOSAIC stays out of the traffic path ([ADR 0001](0001-apim-runtime-boundary.md)), so it can only
  read what the gateway logs.
- The customer's diagnostic setting chooses the workspace, and different gateways can use different
  workspaces in other subscriptions.
- Log Analytics queries take seconds, each caller can run only a few at once, and the workspace
  keeps logs only as long as its retention, often 30 to 90 days.
- A gateway MOSAIC only observes, or an API it only adopted, belongs to someone else.

## Decision

**MOSAIC reads each gateway's logs in the context of the gateway.** It queries Log Analytics'
resource-centric endpoint, `/v1{resourceId}/query`, for the API Management service, with its
managed identity. That needs Monitoring Reader on the service, which `azd provision` grants on the
gateway it deploys, and not access to a whole workspace. The workspace must allow resource
permissions, as new workspaces do by default. MOSAIC doesn't need to know which workspace a gateway
logs to.
- It reads only the resource-specific tables. A setting that writes to the legacy
  `AzureDiagnostics` table is reported, not read.
- It joins each gateway log row to its LLM log rows on `CorrelationId` for token counts.
- Every value spliced into a query is validated first, and nothing a caller sends reaches one.
- `MOSAIC_LOG_ANALYTICS_ENDPOINT` selects the sovereign endpoint, which `infra/main.bicep` sets
  from the deployment's cloud.

**Governed policies record who each call was for, and why a refusal happened.**
- The attribution trace keeps `v=1` and gains `a=`, the validated token's `azp`, so usage can be
  split by client application: `mosaic-attribution v=1 g=<grant> m=<member> a=<client>`. `a` is
  empty for a call that brought only a key. The trace's metadata adds `mosaic-client`. Readers
  already ignore keys they don't know, so no reader breaks.
- Each refusal the policy makes records `mosaic-deny v=1 r=<reason>` just before it returns. The
  reason is a fixed code matching `[a-z][a-z-]{1,31}`: `no-credential`, `keys-off`, `tokens-off`,
  `key-malformed`, `key-unknown`, `token-malformed`, `groups-overage`, `no-grant`,
  `grant-mismatch`, `operation`, `model`, or `access-off`. A refusal after the token validated
  also records ` o=<oid> a=<azp>`.
- Both values come from policy variables that stay empty until the token validates, so nothing
  unvalidated is ever recorded. A 401 that carries no MOSAIC trace, such as a token that failed
  validation, counts as `token-invalid` or `unauthenticated`, judged from the gateway's
  `LastErrorSource`.
- Model and MCP policies record both traces. Neither reads a request or response body.

**MOSAIC sets API diagnostics only on what it published, on gateways it manages.** For its figures
to be complete, each governed API needs an `azuremonitor` diagnostic that logs every call at
Information through the `azuremonitor` logger, with LLM logs on for model APIs.
- On a gateway in manage mode, the administrator's **Enable API diagnostics** creates the logger
  and writes that diagnostic on every API MOSAIC published. The event is audited as
  `gateway.telemetryEnabled`.
- Once the logger exists, whoever created it, the rollup job checks each published API it hasn't
  instrumented yet, such as a new publication, and writes the diagnostic if it lacks something.
  The event is audited as `gateway.telemetryInstrumented`, with the actor `system:usage-rollup`.
  The job doesn't check an API again once it's instrumented. **Enable API diagnostics** checks
  every published API again, so it also repairs a diagnostic someone changed later.
- The diagnostic samples 100% at a fixed rate and logs at Information. It logs no client IP, no
  headers, and no body bytes, frontend or backend. That also keeps MCP streaming working, as
  [ADR 0017](0017-mcp-gateway-enforcement.md) requires. LLM logs are on without their message
  settings, so they record token counts but no prompts or completions. A gateway that refuses LLM
  logging with a 400 gets the diagnostic without it, and the gap is reported.

These writes are the one exception to MOSAIC's rule that every API Management change runs from a
reviewed plan. They change what the gateway logs, not what it allows or where it routes. They
touch only resources MOSAIC owns, they're idempotent, and each is audited. MOSAIC never changes an
adopted API's diagnostics. An adopted API without a diagnostic of its own inherits the service's
All APIs diagnostic, which its owner controls.

**MOSAIC never writes a diagnostic setting.** That needs a role on the service MOSAIC doesn't hold,
and the workspace, and what ingestion costs there, are the customer's choice. The gateway's
**Telemetry** section checks five things instead:
- the logger;
- the log routing, which fails when there's no setting, when the setting leaves out `GatewayLogs`
  or `GatewayLlmLogs`, or when it writes to `AzureDiagnostics`;
- MOSAIC's access to the logs, with a probe of the last 24 hours;
- each governed API's diagnostic;
- the rollups.

For routing and access it shows the `az` command that fixes the problem. The command updates the
setting MOSAIC found, keeping its other categories and metrics, or creates `mosaic-gateway-logs`.

**Usage is rolled up into Cosmos, and every view reads the rollups.** A background job in the API
queries each gateway's logs and folds them into the `usage-rollups` container, partitioned by
tenant. The portal, the Dashboard, and Analytics read only that container, so a page view costs a
few Cosmos reads however busy the gateways are. The container holds four kinds of item:
- **Facts:** one per day, gateway, grant link, and caller, with hourly figures and a breakdown by
  API, deployment, model, and client application. The portal reads these.
- **Summaries:** one per day or month, gateway, and dimension. The dimensions are the total,
  caller, grant, grant and caller, client application, API, deployment, model, denial, and
  unattributed calls. Large ones are split into shards of 1,000 entries. The console reads these.
- **The attribution registry:** what each grant key and APIM subscription stands for, copied from
  bindings. A record is never deleted. When its binding goes, it's marked revoked, so a revoked
  grant's past calls still report against the grant and the person.
- **State:** how far each gateway has been rolled up, and the last error.

Each figure is additive, except busiest-minute peaks, which combine by maximum. Those are exact
for one grant's counter and for one deployment, and a lower bound anywhere else. Latency is kept
as a histogram, so percentiles are estimates.

Daily facts and summaries expire after `MOSAIC_USAGE_ROLLUP_RETENTION_DAYS`, 400 by default and at
least 62. Monthly summaries, the registry, and the state are kept. Analytics shows ranges longer
than about a quarter by the month.

**The job rolls up whole days, and never lowers them.** It runs every
`MOSAIC_USAGE_ROLLUP_INTERVAL_SECONDS`, 900 by default. Each cycle:
1. copies every binding's grant key and subscription into the registry;
2. takes a 15-minute lease per gateway, so two API instances don't query the same gateway twice;
3. reads today and yesterday, plus up to 7 days of any backfill;
4. halves a query's window until the result fits Log Analytics' limits;
5. writes only the items whose content changed, deletes the ones that disappeared, and folds the
   months those days fall in.

The job also re-reads older days in a few cases:
- **First rollup:** a gateway's first rollup reads back `MOSAIC_USAGE_ROLLUP_BACKFILL_MAX_DAYS`,
  90 by default.
- **Catch-up:** a job that stopped or kept failing re-reads the days it missed, up to that same
  limit.
- **Backfill:** an administrator can request one of 1 to 730 days. A backfill requested while the
  first rollup, a catch-up, or another backfill is still reading extends it rather than replacing
  it.

A failed query leaves the day as it was and stops the gateway's cycle, and the state records why.
A day before yesterday is never lowered, and neither is a month. If a re-read finds fewer calls
than MOSAIC already holds, the stored figures stay. That way, logs the workspace has since deleted
don't erase history. Every item has a deterministic ID, so overlapping runs write the same
documents. A gateway's state is saved only over the version that was read, and the job writes only
its own fields. So a refresh or backfill requested during a run, or the outcome of enabling
telemetry meanwhile, isn't lost. An administrator can ask for a rollup now, at most once a minute
for each gateway and once a minute for all of them.

**Calls are attributed by trace first, then by subscription, and never dropped.**
- A call that carries a trace of a known version is linked by its grant key, and for a
  security-group grant, by its member too. The trace links it even before the registry knows the
  key, so it counts once the registry catches up.
- Otherwise, a call that presented an APIM subscription key is linked by that subscription, if
  the registry has it. Subscriptions MOSAIC didn't record for a grant belong to nobody here.
- A call linked by neither is kept as unattributed, with the reason: no subscription, an unknown
  subscription, or a publication's shared key.
- A call's caller is the validated member for a security-group grant, and the grant's own subject
  otherwise.

**Where usage comes from is explicit.** `MOSAIC_USAGE_SOURCE` is `auto`, `rollups`, or `simulated`:
- `auto` reads rollups in Azure and simulates everywhere else.
- `simulated` is refused in Azure, so a deployment never presents invented figures as real ones.
  `infra/main.bicep` sets `rollups`.
- Under `rollups`, a failure to read is an error. A report never falls back to simulated figures.
- The portal shows each report's `dataSource`, and how current it is: current, delayed, failing,
  pending, or not linked. Figures count as delayed when the last success is more than two
  intervals and 30 minutes old.

**Administrator analytics covers the whole tenant.** The Analytics routes need `Admin`. They
filter by time range, gateway, environment, resource, and kind of subject, and export any table as
CSV. Callers MOSAIC has no record of are named by object ID lookups in Microsoft Graph. A request
looks up at most 200, and names are cached for an hour. Without rollups, the routes answer with
`notConfigured` and empty reports, never samples. Cost comes from the price list
[ADR 0020](0020-price-list.md) adds. The portal hides its cost column whenever no row has a cost.

## Consequences

- People see their measured use against each grant's limits, and administrators see the estate by
  person, application, group, grant, model, deployment, and API. Neither depends on the workspace's
  retention for history MOSAIC has already rolled up.
- Existing deployments need `azd provision` for the container, the logger, and Monitoring Reader.
  Other gateways need a diagnostic setting and the role, which the Telemetry section spells out.
- Figures trail calls by the workspace's ingestion delay, usually a few minutes, plus up to one
  interval. The gateway enforces limits as calls arrive, so someone can reach a limit before a
  view shows it.
- Each governed call adds a gateway log row, and for models an LLM log row, to the customer's
  workspace. The workspace bills that ingestion.
- Rollups hold Entra object IDs and client application IDs, which are personal data. Daily items
  expire, but monthly summaries keep per-caller figures until deleted. Tenants must apply their
  retention and access rules to both the container and the workspace.
- A publication applied before this change doesn't record client applications or refusals until
  its next apply.
- An observed gateway, or an adopted API, is measured only as well as its owner's diagnostics
  allow. Calls through a publication's shared key can be counted, but not tied to a person.
- Environments are current, not historical. Analytics filters by each gateway's environment, and
  the portal groups by each resource's, so re-classifying one moves its history with it.

## Alternatives considered

- **Query Log Analytics for each page view.** Rejected. It takes seconds, runs into the per-caller
  query limit, and loses history when the workspace's retention runs out.
- **Workspace-centric queries.** Rejected. MOSAIC would need Log Analytics Reader on every
  workspace, which exposes other resources' logs, and would need to find each gateway's workspace.
- **Application Insights and `llm-emit-token-metric`.** Rejected as the source. Application
  Insights samples, custom metrics allow few dimensions, and neither is guaranteed to exist on a
  gateway MOSAIC didn't deploy.
- **API Management's reports.** Rejected. They're keyed on API Management users and subscriptions,
  which token callers don't have, and they carry no token counts.
- **Stream logs through Event Hubs.** Deferred. It would give lower latency at the cost of more
  infrastructure and a service to ingest the stream.
- **Write each gateway's diagnostic setting.** Rejected. It needs more than read access to every
  gateway, and would make MOSAIC choose where the customer's logs go and what they cost.

## Live verification still required

Unit tests run the queries against a local double, not Log Analytics. On a real gateway, confirm:
- that the trace messages reach `ApiManagementGatewayLogs.TraceRecords` in the shape the queries
  expect, for model and MCP APIs;
- that LLM log rows carry token counts with message logging off, and how streamed calls split
  across rows;
- that Monitoring Reader on the service is enough for resource-centric queries, and how Log
  Analytics answers a timespan that ends in the future;
- how the gateway's tier affects LLM logging;
- how KQL's `sum` treats calls with no token counts;
- the default sampling and verbosity of an All APIs diagnostic;
- whether `GatewayLogs` rows exist for an API that has no diagnostic;
- whether a diagnostic setting created from the Azure CLI creates the `azuremonitor` logger.

## Amendment 2026-10-01: Trace properties are never empty

API Management checks each trace `metadata` value as a call runs. A value that evaluates to an
empty string fails the whole call with 500 before it reaches the backend, and the gateway log
records an `ExpressionValueValidationFailure` from `trace`: "The value field is required." The
decision above added `mosaic-client`, which is empty for every call that brings only a key, and a
key call to a governed model failed this way on a live gateway. `mosaic-member`, from
[ADR 0015](0015-end-user-usage-report.md), is empty for every direct-grant call to a publication
that also has security-group grants, so those calls fail the same way, for models and MCP servers.

- **Each property records `-` for a value the call doesn't have.** One helper writes every
  `metadata` element with that guard, and a test fails when a policy adds one any other way.
- **The message doesn't change.** It still leaves `m=` and `a=` empty when a call has no member
  or client. Resource logs and MOSAIC's queries read only the message, so no reader changes.
- **An applied policy keeps its old fragment until it's applied again.** Plan and apply each
  governed model and MCP publication applied before this change. Until then, a model applied
  since this ADR fails every key call with 500, and any publication with security-group grants
  fails its direct-grant calls.
- **Confirm it live.** Once a publication is applied again, a key call and a direct-grant call
  should succeed, and Application Insights should show `-` for the property the call lacks.

## Amendment 2026-10-02: Tokens count only for calls the model served

On a dev deployment, the gateway refused two calls over a grant's limit of 100 tokens a minute,
with 429, before they reached the model. The LLM log still had a row for each, holding the
gateway's estimate of its prompt and no model name. The rollups counted those tokens for the API,
the grant, and the total, and Cost priced them, though Azure bills nothing for a call that never
reached the model. The model breakdown left them out, because it kept only calls the LLM log named a
model for, so cost by model added up to less than cost by API.

- **A call's tokens count only when the model deployment served it**, which is when the gateway
  log records a `BackendResponseCode` from 200 to 299. The queries read no tokens from the LLM log
  for any other call: one the gateway refused with 429 or 403, one with no `BackendResponseCode`,
  and one the deployment answered with its own 429 or another error. Azure doesn't bill a call it
  throttled or failed, and an error response carries no usage, so the LLM log can hold at most the
  gateway's estimate for one. Counting only 2xx keeps the rule simple, and MOSAIC never prices an
  estimate for a call that produced nothing.
- **Requests and outcomes don't change.** Every admitted call still counts as a request with its
  outcome: throttled, quota refused, a client or server error, and the deployment's own 429s, which
  Reliability, Limits, and the portal read. A busiest minute counts only served calls' tokens, so a
  refused call no longer lifts a grant's peak above the limit that refused it.
- **Every call with tokens counts under a model.** The rollups keep a call whose LLM log named no
  model under an empty name. Reports name it after the model MOSAIC observed the API's deployment
  serving, else the deployment's name, else **Unknown model**, as a cost center's model breakdown,
  rebuilt from its grants, already did. So cost by model and cost by API each add up to the total,
  apart from reserved capacity nobody called, and cost by cost center does too, apart from
  unattributed calls as well.
- **Figures already rolled up are corrected when their day is read again.** Each cycle reads today
  and yesterday again, so those days are corrected within one interval of the upgrade. A
  **Backfill** of the affected days, from the gateway's **Telemetry** section or
  `POST /gateways/{id}/telemetry/backfill`, corrects older days while the workspace still holds
  their logs. A re-read finds the same calls, so it replaces the day and folds its month again. A
  day whose logs the workspace has partly or wholly deleted keeps its figures, because a re-read
  that finds fewer calls never lowers a day.
- **Confirm it live.** What the LLM log holds for a call the deployment throttled or failed, and
  for a prompt a content filter blocked with 400, is to be checked on a real gateway. Neither
  counts tokens either way.
