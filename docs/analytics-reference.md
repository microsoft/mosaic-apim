# Analytics views and API reference

[Documentation](README.md) | [MOSAIC](../README.md)

- [Usage and analytics](#usage-and-analytics)
- [Measuring a gateway's usage](#measuring-a-gateways-usage)
- [Usage analytics in the console](#usage-analytics-in-the-console)
- [Pricing](#pricing)
- [Usage and cost in the portal](#usage-and-cost-in-the-portal)

## Usage and analytics

MOSAIC measures usage from API Management's own resource logs, so it never sits in the traffic
path. A background job in the API reads each gateway's logs from Log Analytics every 15 minutes
and rolls them up into the `usage-rollups` container. The portal, the Dashboard, and Analytics read
those rollups rather than querying Log Analytics per page view, and the figures outlive the
workspace's retention. Pool Health and explicit telemetry diagnostics are separate direct-log paths.
[Usage analytics](usage-analytics.md) covers setup, freshness, retention, privacy, and what
each figure means. [ADR 0019](adr/0019-usage-telemetry.md) records the design.

## Measuring a gateway's usage

MOSAIC can measure a gateway's usage when:
- a diagnostic setting sends the gateway's `GatewayLogs` and `GatewayLlmLogs` categories to a Log
  Analytics workspace as resource-specific tables;
- MOSAIC's managed identity holds Monitoring Reader on the API Management service; and
- each API MOSAIC governs logs every call at Information through the `azuremonitor` logger, with
  LLM logs on for model APIs.

`azd provision` sets up the first two, and the logger, on the gateway it deploys. For every
gateway, the **Telemetry** section of its page checks each condition and shows the `az` command
that fixes what's missing. MOSAIC never creates or changes a diagnostic setting.

On a gateway in manage mode, **Enable API diagnostics** creates the logger and sets the
`azuremonitor` diagnostic on every API MOSAIC published. Once the logger exists, whoever created
it, the rollup job sets the diagnostic on each new publication too. MOSAIC doesn't change adopted
APIs, so their owners set their diagnostics, or the one for All APIs, which an API without its own
inherits. **Refresh now** runs a rollup straight away, at most once a minute. A gateway's first
rollup reads back 90 days by default, and **Backfill** reads older days again from what the
workspace still holds. A re-read never lowers a day's figures, so days the workspace has since
deleted keep what MOSAIC rolled up.

Each call a governed policy authorizes carries a trace naming the grant it matched, the member
for a security-group grant, and the calling client:
`mosaic-attribution v=1 g=<grant> m=<object ID> a=<client ID>`. A call the policy refuses carries
`mosaic-deny v=1 r=<reason>`, plus the caller's object ID and client once its token was validated.
Both are API Management `trace` policies with source `mosaic` at `information` severity, which
`ApiManagementGatewayLogs` records in `TraceRecords`. A call MOSAIC can link neither by its trace
nor by a grant's APIM subscription is reported as unattributed, never dropped. The traces hold
Entra object and client IDs, which are personal data, so apply your retention and access rules to
the workspace. The bootstrap gateway's Application Insights diagnostic logs at Information too, so
each governed call also adds one trace there, whatever the sampling rate. To stop them, set that
diagnostic's verbosity to Error.

A governed [model pool](model-pools.md) traces its calls the same way. MOSAIC places each call on the
member deployment that served it, from the backend the gateway logged, and prices it at that
deployment. Calls it can't place stay with the pool, unpriced. Every pool, governed or not, also
writes a `mosaic-attempt v=1` trace after each attempt, naming the pool model, the backend, host,
and path it called, and the status it got, but no caller. The pool's **Health** card reads them.
Application Insights at Information records them too, so a retried call adds a trace there for
each attempt.

## Usage analytics in the console

The **Dashboard** shows the last 7 days: requests, tokens, active callers, error rate, and p95
latency, spend this month and its month-end forecast, the daily trend, how current each gateway's
telemetry is, and the top five models, callers, and APIs. **Analytics** covers the last 24 hours, 7,
30, or 90 days, 12 months, or chosen dates, and filters by gateway, environment, resource (a model
API, [model pool](model-pools.md), or MCP server), kind of subject, and [cost center](cost-centers.md):

| Tab | What it shows |
| --- | --- |
| Overview | Requests, tokens, active callers, errors, p95 latency, and cost against the previous period, the trend, and the top models, callers, and APIs |
| Cost | What usage cost at list price: the total and trend, spend this month and the month-end forecast, and cost by model, deployment, caller, and API, with each provisioned deployment's monthly cost and utilization |
| Consumers | Each person, agent, application, and Entra security group, with requests, tokens, cost, grants, resources, and when they were last seen; the model calls MCP servers' applications made for each person, which those applications already count; then each grant, and each client application |
| Models | Each model, each deployment's busiest minute against its capacity, and each API and MCP server, with their cost |
| Reliability | Successful, gateway-throttled, backend 429, and denied calls, an estimated latency histogram, denials by reason, and each API's reliability |
| Limits | How close each grant is to each quota and rate limit, and the calls the gateway throttled or refused for quota |
| Access hygiene | Grants nobody used in the last 30 days, keys nobody used because every call brought a token, and grants MOSAIC can't track |
| Unattributed | Calls MOSAIC couldn't link to a grant, such as those made with a publication's or model pool's shared key, and what they cost |

Every tab exports CSV, and tables that show cost export it too. The Cost tab exports a chargeback by
month, person, application, or group, cost center, and model. It splits an MCP server's
application's rows by the person each model call was made for, still charged to the application's
cost center. These routes need `Admin`:

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/analytics/status` | Where the figures come from, and how current each gateway's rollup is |
| POST | `/analytics/refresh` | Roll up every gateway now; 202, or 429 within a minute of the last |
| GET | `/analytics/{view}` | `overview`, `cost`, `consumers`, `models`, `reliability`, `limits`, `hygiene`, or `unattributed`, filtered by `range` (`24h`, `7d`, `30d`, `90d`, `12m`, or `custom` with `start` and `end`), `gatewayId`, `environment`, `resourceId`, `subjectKind`, and `costCenterId` |
| GET | `/analytics/export?view=` | One table as CSV, with the same filters; `chargeback` is the chargeback by month, `costDeployments` the cost of each deployment, and `onBehalf` the model use through MCP servers |
| GET | `/gateways/{id}/telemetry` | The gateway's telemetry checks and each governed API's diagnostic |
| POST | `/gateways/{id}/telemetry/enable` | Create the logger and set API diagnostics on what MOSAIC published; audited |
| POST | `/gateways/{id}/telemetry/refresh` | Roll up this gateway now; 202, or 429 within a minute of the last |
| POST | `/gateways/{id}/telemetry/backfill` | Re-read `days` of older logs, 1 to 730, or `MOSAIC_USAGE_ROLLUP_BACKFILL_MAX_DAYS` when omitted; 202, audited |

## Pricing

MOSAIC prices usage from a sourced, dated price list, at list price, each time a view is read. It
ships list prices for Azure Commercial and Azure Government, read from the public Azure Retail
Prices API, and every price cites its source. On the console's **Pricing** page, administrators add
prices for custom clouds and other providers, such as an OpenAI-compatible endpoint or AWS Bedrock,
or override one from a date. Each is a new version, audited, and never changes the days before it.

A managed Azure deployment's default pricing cloud comes from its explicit resource cloud; a
URL-only endpoint uses its verified host family, where `.azure.us` means Azure Government.
An administrator can override the pricing cloud, or name one for a host outside Azure such as
AWS Bedrock, without changing management credentials or runtime cloud. The most specific price wins, by deployment, model, version,
region, deployment type, and publisher. Provisioned throughput costs its PTUs by the hour, or a
monthly amount, shared among its callers by their share of its tokens. A
[model pool](model-pools.md)'s calls are priced at the member deployment that served each one. Usage
MOSAIC can't price shows **No price**, never $0, and the **Unpriced deployments** tab says what
would price it, including a pool's calls MOSAIC couldn't place on a member.
[Pricing](pricing.md) covers sources, matching, overrides, PTUs, chargeback, and refreshing the
seed. [ADR 0020](adr/0020-price-list.md) records the design. These routes need `Admin`:

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/pricing` | Each cloud, the seed's sources, and how many deployments have a price |
| GET | `/pricing/prices?cloud=` | A cloud's prices, each with the version in effect today, the next one scheduled, and their sources |
| POST | `/pricing/prices` | Add a price, or a new version of one, from a date, with a source URL and a note; audited |
| GET | `/pricing/prices/{lineId}/history` | Every version of a price |
| GET | `/pricing/unpriced` | Deployments with no price today, and adopted model APIs with calls, with why |
| GET | `/pricing/endpoints` | Each endpoint's cloud, region, and deployments, as MOSAIC prices them |
| PATCH | `/pricing/endpoints/{id}` | Override an endpoint's cloud or region, or set a declared deployment's type and PTUs; audited, and refused with 409 if the facts changed since the `version` sent |

## Usage and cost in the portal

The portal's **Usage & cost** page shows people their own requests, tokens, errors, and
throttling for each grant they hold. It breaks them down by day, by hour over the last 24 hours, by
environment, and by resource, and shows each quota's use within that quota's own window and the
busiest minute against each rate limit. It reads `GET /api/v1/me/usage?period=7d|30d|90d`, which
returns only the caller's own usage. A security-group grant's figures count only the caller's own
calls. For each [cost center](cost-centers.md) the caller holds an enabled grant under, the page adds
the cost center's total this month, everyone's calls together, and the total on each resource they
hold there that has a pooled quota, against that quota. It never shows who else called or how much.
When one of those cost centers is at 80% of its [budget](budgets-and-alerts.md), past 100%, or
blocked, this page and **My access** show a banner with the cost center's share of its budget,
from `GET /api/v1/me/budgets`.

When an MCP server's application calls models for someone, as
[MCP servers that call models](mcp-servers-that-call-models.md) describes, the page adds
**Model use through MCP servers**: their own share of those calls, by MCP server and model, with
its estimated cost and the cost center of the application's grant, which paid for it. It never
shows the application's own calls or anyone else's, and it's left out of the person's totals.

In Azure the figures are measured, and the page says how current they are. The gateway applies
limits as calls arrive, so someone can reach one before the page shows it. A row with nothing to
link its calls has null figures, not zero. Cost is the person's own calls at list price, from the
[price list](analytics-reference.md#pricing). A resource MOSAIC can't price shows **No price**, and the totals say how
many resources they exclude.

Local and test runs simulate the report instead, from the caller's real grants and limits. The
figures are deterministic and never exceed a quota, costs are illustrative estimates, and the page
is labeled **Sample figures**. An Azure deployment refuses to simulate, and a failure to read the
rollups is reported, never replaced with simulated data. See
[ADR 0015](adr/0015-end-user-usage-report.md).

The **Usage tracking** column says how each grant's calls are found:
- **Linked from gateway log traces:** a model or MCP publication MOSAIC applied tags every call it
  authorizes with the grant it matched. This links Entra-token, security-group, and MCP grants,
  which have no APIM subscription.
- **Linked from the APIM subscription:** the grant's binding names the subscription that carries
  its calls. Imported model APIs, imported MCP servers, products, and model deployments can be
  linked only this way.
- **Not linked yet:** nothing links the grant, so MOSAIC can't measure its usage. A publication
  applied before gateway tagging existed starts tagging on its next apply.
