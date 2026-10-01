# ADR 0020: A sourced, dated price list

**Status:** Accepted

## Context

[ADR 0019](0019-usage-telemetry.md) made usage real: API Management's logs are rolled up into
Cosmos, attributed to each grant, person, application, and security group. Nothing turned that
usage into cost. The portal's page is called **Usage & cost**, but in measured mode it showed no
cost, and Analytics had no cost at all. Administrators need to know what usage costs, who and
what drives it, what this month will come to, and how to charge it back.

Several things shape how MOSAIC can price usage:
- Prices differ by cloud, Azure Commercial or Azure Government, and by deployment type: Global,
  Data Zone, and regional Standard, Batch, and provisioned throughput. Regional types are priced
  by region. A model version can have its own price. Custom clouds and other providers, such as
  OpenAI-compatible endpoints, have prices Azure doesn't publish.
- Prices change. A past day must be priced at that day's price, and a mistake must be correctable
  without silently changing what's already been reported.
- MOSAIC can't see invoices, discounts, commitments, or reservations. It can only estimate at list
  price.
- A wrong price is worse than none. A model whose price MOSAIC doesn't know must never look free.
- The rollups count prompt, completion, and total tokens. They don't separate cached prompt
  tokens, which some models bill at a lower rate.
- Provisioned throughput (PTU) is billed by the hour whether or not anyone calls the deployment.

## Decision

**MOSAIC ships a seed of list prices, and every price cites its source.** The seed is
`apps/api/src/mosaic_api/data/model_prices.json`, shipped inside the API's package. Its shape is
modelled on SimpleChat's `model_capabilities.json`:
- At the top: `schemaVersion`, `lastUpdated`, `currency` (always USD), `sources`, `prices`, and
  `ptuThroughput`. Each source has an ID, a title, a URL, and the day it was read.
- Each price names a cloud, a publisher, a model and its aliases, and optionally a version, a
  deployment type, and regions. It gives US dollars per million input, cached input, and output
  tokens, or per PTU an hour. It takes effect on `effectiveFrom`, cites `sourceIds`, and records
  the Retail Prices API meters it was read from.
- `model_prices.schema.json` is generated from the Pydantic model the API validates the seed with,
  so the two can't drift. The API refuses to start with a seed that doesn't validate, and a test
  fails if a price cites no source or a source the seed doesn't list.

`scripts/price_seed.py` builds the seed from the public Azure Retail Prices API, which needs no
sign-in. It queries the Foundry Models service's consumption prices once per product, and records
each query's URL as a source with the day it was read.
- Azure Government is the `usgovarizona`, `usgovtexas`, and `usgovvirginia` regions. Every other
  region is Azure Commercial.
- Standard and regional provisioned types are processed in the resource's region, so they're
  priced by region. Other types list one price for every region, unless the API prices regions
  differently. Regions are grouped by their price and the day it took effect, so a region whose
  price changed is never priced at its new price before then.
- The seed never invents a price. A model or deployment type the API doesn't list stays out, and
  deployments of it have no cost until an administrator prices them.
- PTU throughput per model, which utilization needs, comes from Microsoft Learn's sizing
  guidance, also cited.
- The API lists only today's prices. A refresh keeps each price it replaces, with
  `effectiveUntil` set to the day before its replacement takes effect, and cites the source as it
  was read then. So a refresh doesn't change the cost of a day already priced. The one exception
  is a replacement the API dates before the day the old price was last read. The refresh follows
  the API, which is the record, and reports it.

**Administrators add and override prices as dated versions, and nothing is rewritten.** Each
version is a `priceVersion` item in `desired-state`, written in one transactional batch with its
audit event, `pricing.priceRecorded`. It's never updated or deleted.
- A version records who entered it and when, the day it takes effect, a source URL, and a note.
  All four are required.
- Two versions are of the same price when they name the same cloud, publisher, model, version,
  deployment type, regions, and deployment. A version applies from its date until a later version
  of the same price takes effect.
- A correction is a version with the same date as the one it corrects. An administrator's version
  beats a seeded one, even one a later release ships again, and between administrators' versions
  the one recorded later wins. The history marks the one it beat as corrected. Changing a past
  day's cost takes a version backdated on purpose.
- An override of a seeded price is a version of the same price. MOSAIC never changes the seed at
  run time. A new seed arrives with a release.
- A price can be for any cloud, so custom clouds and other providers are priced the same way, and
  for every model of a publisher. A price can also be pinned to one deployment, keyed
  `{endpointId}/{deploymentName}`, such as a reservation's monthly amount.

**Each deployment is priced by its facts, and the most specific price wins.**
- The facts are the cloud, the publisher, the model and version, the deployment type, the region,
  and, for provisioned throughput, the capacity. MOSAIC reads them from the deployments it
  observed: the SKU's name is the deployment type, and its capacity is the PTUs.
- The cloud comes from the endpoint's host. A host under `.azure.us` or `.usgovcloudapi.net` is
  in Azure Government, and one under `.azure.com` is in Azure Commercial. Any other host, such as
  an OpenAI-compatible provider's, has no cloud until an administrator names one. An administrator
  can override the cloud MOSAIC detected, and the region, for each endpoint.
- Deployments declared on an endpoint reached with an API key
  ([ADR 0018](0018-key-authenticated-backends.md)) have no SKU MOSAIC can read. An administrator
  sets their deployment type, and the capacity of a provisioned one. MOSAIC never overrides a type
  Azure reports.
- These facts are an `endpointPricing` item per endpoint in `desired-state`, saved only over the
  version that was read and audited as `pricing.endpointUpdated`. A change merges field by field
  into what's saved, so two administrators' changes to different fields both survive.
- A price applies when its cloud matches, its model or one of its aliases names the deployment's
  model, or it's for every model, and every other fact it names matches. A publisher is compared
  only when the deployment's is known. A regional price applies only in a known region. A PTU
  rate applies only to a provisioned deployment, and a token price only to one that isn't.
- The most specific applicable price wins: one pinned to the deployment, then one naming the
  exact model rather than an alias or every model, then one naming the version, then the region,
  then the deployment type, then the publisher. So a price without a region applies where no
  regional one does, and one without a version applies where no versioned one does. Among equally
  specific prices, the latest in effect wins. For the same date, an administrator's beats a seeded
  one, and then the one recorded last wins.
- A deployment nothing prices has no cost: null in the API, **No price** on screen, never $0.
  Reports count what they left out, and the Pricing page lists each unpriced deployment with
  why.

**Cost is computed each time it's read, by the day.** Nothing about cost is stored. A report
prices the rolled-up usage of each day at the price in effect that day, so a new price changes
only the days from its date, and a correction changes history the next time anyone looks.
- Tokens cost the prompt tokens at the input price plus the completion tokens at the output price.
  The rollups don't separate cached prompt tokens, so every prompt token is priced at the input
  price, and every report says so.
- A model API's calls are priced by the deployment its publication fronts. An API MOSAIC adopted
  rather than published has no known deployment, so it has no cost. A grant's calls are priced by
  the model API it grants. MCP servers carry no tokens and have no cost.
- Days older than daily retention exist only in monthly totals. A month in which a price changed
  is priced as if its calls were spread evenly across its days, and the report says so.
- MOSAIC prices whole days, so a view by the hour shows no cost.

**Provisioned throughput costs its capacity, shared by token share.**
- A provisioned deployment's day costs its PTU hourly rate times its capacity times 24, or a
  monthly amount an administrator entered, divided by the days in the month.
- It costs that from the day Azure created it, as its `systemData.createdAt` records. When MOSAIC
  didn't read that, it's from the day MOSAIC first saw a governed API fronting it.
- Each month's cost is shared among the deployment's callers by their share of its tokens that
  month, counted across every gateway, so filtering a report never inflates anyone's share. A
  month with no calls leaves its cost idle, and reports and the chargeback show it apart.
- Utilization compares the deployment's tokens with what its PTUs serve, by Microsoft Learn's
  figures for the model, and is shown next to its cost.

**Analytics, the Dashboard, and the portal show cost.**
- Cost fields on the analytics responses are null when nothing could be priced, and each report
  counts the tokens, requests, and items it left out.
- `GET /analytics/cost` adds the Cost tab: the total, the trend, and cost by model, deployment,
  caller, and API. Each deployment row shows its price, and for provisioned throughput, its
  monthly cost, utilization, and idle cost.
- Spend this month is the calendar month so far, in UTC. The forecast is pay-as-you-go spend so
  far, times the days in the month, divided by the days the figures cover, to the hour. Reserved
  capacity costs the same every day, so its whole month is added as it is. The forecast appears
  once the figures cover a day, and is labelled projected.
- The `/pricing` routes need `Admin`, like every other administrative route.
- The portal prices only the caller's own calls. For a security-group grant, that's their own
  share of the group's calls, and a provisioned deployment's cost is shared the same way as in
  Analytics.

**Chargeback is a CSV by month, party, and model.** The `chargeback` export has one row for each
month, party, model, deployment, and endpoint. A grant's calls are charged to its subject: the
person, application, or security group it was granted to. Calls MOSAIC couldn't attribute, and
reserved capacity nobody called, get rows of their own. Each row says whether it was priced fully,
partly, or not at all.

## Consequences

- Administrators see what usage costs at list price, everywhere they see usage, and can charge it
  back. Every figure is an estimate before discounts, commitments, and reservations, not a bill,
  and the reports say so.
- Azure's list prices change more often than MOSAIC is released. Administrators keep prices
  current by entering versions, and a release refreshes the seed.
- A deployment is priced only as well as MOSAIC knows its facts. One on an unrecognized host, or a
  declared one without a type, has no cost until an administrator fills them in.
- Pricing at read time costs some work per report, but keeps every view consistent with the
  latest corrections, and needs no backfill when a price changes.
- No migration is needed. The new items live in `desired-state`, and nothing in Azure changes, so
  a deployment gets cost from its next API release without `azd provision`.
- Price versions record the Entra object ID of the administrator who entered them.

## Alternatives considered

- **Query the Retail Prices API at run time.** Rejected. It adds an outbound dependency that
  air-gapped and sovereign deployments can't reach, it has no history, and its answers couldn't be
  reviewed before they changed someone's costs.
- **Read actual cost from Cost Management.** Deferred. It needs billing access MOSAIC doesn't hold,
  lags by a day or more, and can't split a deployment's cost by caller. It could reconcile the
  estimates later.
- **Store cost in the rollups.** Rejected. A correction would need history rolled up again, and
  the rollups would hold prices that could be wrong.
- **Edit prices in place.** Rejected. History would change without anyone seeing why.
- **Treat unpriced usage as free.** Rejected. A model with no known price would look free.

## Live verification still required

The seed comes from the live Retail Prices API, but MOSAIC's tests run against fixed prices. On a
real deployment, confirm:
- that a month's estimate for a pay-as-you-go deployment matches its line in Cost Management at
  list price;
- that the Retail Prices API's meters for each seeded model and type are the ones the deployment
  is billed on, in Azure Commercial and Azure Government;
- that Azure Government's endpoints use hosts under `.azure.us`;
- that a deployment's `systemData.createdAt` is the day its PTUs started billing;
- whether `ApiManagementGatewayLlmLog` can report cached prompt tokens, so they could be priced at
  their own rate.
