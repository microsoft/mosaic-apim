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
