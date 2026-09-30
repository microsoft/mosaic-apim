# ADR 0015: The end-user usage report contract

**Status:** Accepted

## Context

The end-user portal showed what a person is entitled to, but not how much they had used. The
people who asked for development access, and later production access, had no way to see what
their applications consumed or what it was likely to cost. The administrator's Analytics page runs
on static sample data and describes the whole estate, not one person.

Real consumption comes from Log Analytics. `ApiManagementGatewayLogs` and
`ApiManagementGatewayLlmLog` are keyed on the API Management subscription. ADR 0009 records which
subscription realizes each grant. A grant with no binding can't be attributed, and ADR 0009 says
the portal must report that rather than show zero.

MOSAIC doesn't query Log Analytics yet. The portal still needs a usage page that people can use,
and that won't have to change when real data arrives.

## Decision

**The contract comes before the data.** `GET /api/v1/me/usage?period=7d|30d|90d` returns a
`MyUsageReport` for the caller only. It covers:
- the grants the caller holds, directly or through a group, resolved exactly as the portal's
  entitlements are. There is one row per resource. A direct grant wins over a group grant, and an
  enabled grant wins over a disabled one. A disabled grant appears, with no usage, only when no
  enabled grant covers the same resource;
- totals, a daily timeline per grant, a breakdown by environment, and a row per resource with its
  quotas and rate limits;
- plain-language notes.

The route follows ADR 0008, so it is available to the `User` role and scoped to the caller.

**Simulated data is labeled at two levels.**
- `dataSource` says where the whole report came from: `simulated` today, `logAnalytics` later.
- Each row's `attribution` says how that grant's figures were produced:
  - `simulated`: every row today;
  - `measured`: a bound grant, once a real source exists;
  - `unattributed`: an unbound grant under a real source. Its figures are null, not zero.

  Each row also reports whether the grant is `bound`. Even on simulated data, a user can see
  which grants won't be attributable once real data arrives.

The portal shows a Sample data badge and a notice whenever `dataSource` is `simulated`.

**The simulation is built from real grants and limits, and it is honest about them.**
- It is deterministic, seeded from a SHA-256 of tenant, user, grant, and date, so reloading the
  page never changes the figures.
- Usage starts on the day the grant was created. A disabled grant shows none.
- Volume scales by environment class, so production is busier than development, and weekends
  are quieter.
- MCP servers count requests only. Their token fields are null because they don't meter tokens.
- Usage never exceeds a limit within that limit's own window. Each window is simulated whole and
  then trimmed to the report period, so a given day shows the same figures on the 7-, 30-, and
  90-day views.

**Quotas are reported in their own window.** A monthly token quota reports the current calendar
month, whatever period the page shows. Windows are UTC, and weeks start on Monday. A quota's
utilization is therefore the figure API Management would enforce against, not a proportion of the
selected report period.

**Cost is estimated only where the model is known.**
- Publication-backed model APIs and model deployments are priced from one illustrative rate
  table, labeled as illustrative.
- MCP servers, products, and imported model APIs with no known model have a null
  `estimatedCost` and a `costNote` explaining why.
- Totals sum what could be priced, and `costExcludedResources` counts the rows they leave out.
  When nothing can be priced, the total `estimatedCost` is null rather than zero. An estimate is
  never presented as a bill.

**No silent fallback.** Once a real source is configured, a failure to read it is an error. The
report never substitutes simulated figures for a failed query, as with every other preview in
MOSAIC.

## Consequences

- Switching to Log Analytics is a new `UsageSource` implementation behind the same route. The
  portal doesn't change.
- Every figure the portal shows today is simulated, and says so. Nothing about the estimates is a
  commitment to a price.
- Chargeback, administrator-configured pricing, and per-user usage in the administrator console
  are out of scope.
- Usage by environment relies on ADR 0014. A grant takes its resource's current environment, so
  re-classifying a resource moves its history with it. The simulation doesn't record which
  environment a resource was in on a given day.

## Amendment 2026-09-30: Linking grants to gateway telemetry

Until now a row was `bound` whenever its grant had a binding, and ADR 0009 linked a grant to
telemetry only through an APIM subscription. Entra-token grants, security-group grants, and MCP
grants have no subscription of their own. Their rows showed "Not linked yet", so their real usage
could never be attributed. An orchestrated binding with no subscription also counted as bound,
although nothing in the logs identified its grant.

**The gateway tags every call it authorizes with the matched grant.** The MOSAIC-managed policy of
a model or MCP publication emits an API Management `trace`, with source `mosaic` and severity
`information`. It runs once the caller's grant is resolved and before any limit. For models, that's
after the operation guard. Its message is versioned key=value text,
`mosaic-attribution v=1 g=<grant> m=<member>`:
- `g`: the grant's stable counter identity, a SHA-256 of tenant, publication, and entitlement. The
  policy's own rate limits and quotas count against the same identity, so key rotation, access
  method, and snapshot revision don't change it.
- `m`: the caller's validated Entra object ID when a security-group grant matched, because a group
  grant's usage must be split by member. It's empty otherwise.

Readers split the message on spaces and ignore keys they don't know, so a later version can add
keys, such as the calling client. Only a change that breaks those readers bumps `v`. The trace
repeats the values as metadata: `mosaic-grant`, and `mosaic-member` when the policy has enabled
security-group grants. API Management documents metadata only as Application Insights properties,
so resource logs are read from the message.

Throttled calls are tagged too, so a source filters by response code. Calls the policy rejects
before a grant matches carry no tag and belong to no grant.

**Bindings record how a grant is linked.** `EntitlementBinding` gains two server-managed fields.
An applied publication sets them for every grant in its snapshot:
- `attributionKey`: the `g` value.
- `attributionPerMember`: true for a security-group grant.

Model publications already projected orchestrated bindings. MCP publications now project them on a
successful apply and clear them on a successful unpublish. A manual binding can set neither field,
because only an applied policy makes the tag real.

**Each row reports `linkedBy`.**
- `gatewayLog`: the binding has an attribution key. A real source matches it to `g`, and for a
  per-member grant, also matches `m` to the caller's object ID. That keeps one member's
  report from including another member's calls.
- `subscription`: the binding names an APIM subscription. A source matches it as before. Imported
  model APIs, imported MCP servers, products, and model deployments carry no MOSAIC policy, so this
  is the only way to link them.
- null: nothing links the grant.

`bound` is true exactly when `linkedBy` isn't null. A binding that names only a gateway no longer
counts as bound. The portal's Usage page shows the same fact as **Usage tracking**: At the gateway,
By APIM subscription, or Not linked yet. My access explains it in plain language.

**A real source has prerequisites.**
- `ApiManagementGatewayLogs` records the trace's message in `TraceRecords` when the
  gateway's Azure Monitor diagnostic logs at Information or Verbose. Token counts come from
  `ApiManagementGatewayLlmLog`, joined on `CorrelationId`.
- The Application Insights diagnostic that `infra/modules/apim.bicep` deploys logs at Information
  with 100% sampling. A trace isn't subject to sampling, so each governed call adds one trace
  record there. Setting that diagnostic's verbosity to Error stops them without affecting the
  resource logs.
- `m` and `mosaic-member` hold an Entra object ID, which is personal data. Both destinations need the
  retention and access controls the tenant applies to personal data.

Consequences:
- A publication applied before this change doesn't tag calls until its next apply. Until then its
  rows show By APIM subscription where the grant has a subscription, and Not linked yet otherwise.
- The simulated source is unchanged. `linkedBy` changes which rows count as bound and which rows a
  real source can attribute, not the simulated figures.
- MOSAIC hasn't yet confirmed on a live gateway that the message reaches `TraceRecords`. The Log
  Analytics source must verify it before relying on it.

## Amendment 2026-09-30: Measured usage

[ADR 0019](0019-usage-telemetry.md) adds the real source this ADR left room for, behind the same
route:
- **Figures are measured in Azure.** `/me/usage` reads rollups of each gateway's logs.
  `dataSource` is `logAnalytics`, and each row is `measured` or `unattributed`.
- **The simulation is only for local and test runs.** `MOSAIC_USAGE_SOURCE=simulated` is refused
  in Azure, and a failure to read the rollups is an error, as this ADR required. The portal's badge
  for simulated figures now reads Sample figures.
- **The attribution trace gains a key.** It adds `a=<client ID>`, the validated token's `azp`,
  under `v=1`, as readers that ignore unknown keys allow.
- **Measured reports add fields.** They add `freshness`, the last 24 hours by hour, throttled,
  quota-refused, and failed calls, each day's busiest minute against per-minute rate limits, and
  each grant's last use. A quota whose window MOSAIC has only partly measured is marked `partial`,
  so its use is a lower bound.
- **Measured reports carry no cost until MOSAIC has a price list.** The simulation keeps its
  illustrative rates. The portal hides the cost column when no row has a cost.
- **Environment rows count unmeasured resources.** A row's `unmeasuredResources` counts the
  resources whose usage MOSAIC can't link, which its figures leave out.
- **Usage tracking is worded by its source.** The portal's labels now read Linked from gateway log
  traces, Linked from the APIM subscription, and Not linked yet.
