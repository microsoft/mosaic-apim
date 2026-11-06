# Pricing

This guide is for administrators who run MOSAIC. It explains where MOSAIC's prices come from, how
it finds the price of each deployment, how to add and correct prices, how provisioned throughput is
charged, and how to refresh the prices MOSAIC ships. The README's
[Usage and analytics](analytics-reference.md#usage-and-analytics) section lists the views and routes,
[Usage analytics](usage-analytics.md) explains the usage that's priced, and
[ADR 0020](adr/0020-price-list.md) records the original design.
[ADR 0031](adr/0031-billing-derived-pricing.md) extends it with reviewed billing-derived rates.

## What MOSAIC's costs are

MOSAIC multiplies the usage it measures by a price list. Every cost is:
- **An estimate in US dollars**, using the applicable public seed, administrator-entered, or
  approved billing-derived rate. Reports can mix these sources. Billing-derived rates reflect an
  observed month's contract-discounted, amortized consumption; they are not an invoice or a
  promise of the next month's rate. The provider's billing system remains authoritative.
- **Computed each time it's read**, by the day, at the price in effect that day. MOSAIC stores no
  cost, so a new price or a correction shows everywhere at once.
- **Null when MOSAIC can't price it**, shown as **No price**, never as $0. Each report counts the
  tokens, requests, and items it left out, and the **Unpriced deployments** tab says why each one
  has no price.
- **Only for calls the model served.** A call the gateway refused for a limit or a quota, or that
  the deployment throttled or failed itself, has no tokens, so it costs nothing. Azure doesn't
  bill a call that never reached the model, or one the deployment throttled. Whether it bills the
  prompt of a call a content filter blocked with 400 is still to be confirmed.
  [Usage analytics](usage-analytics.md#calls) says what counts.

The rollups don't separate cached prompt tokens from the rest, so every prompt token is priced at
the ordinary input price, not a separately discounted cache rate. Cost is shown for whole days
only, so the **Last 24 hours** range shows none.

## Where the prices come from

MOSAIC ships a seed of list prices, `apps/api/src/mosaic_api/data/model_prices.json`, with every
release. Every price in it cites its sources, which the **Sources** tab on the **Pricing** page
lists with the day each was read:
- **The Azure Retail Prices API**, `https://prices.azure.com/api/retail/prices`, queried once per
  product of the Foundry Models service: Azure OpenAI, Azure OpenAI Embedding, Azure OpenAI
  Reasoning, Azure OpenAI GPT5, and the DeepSeek, Llama, Mistral, and Phi models. Each query is a
  source, with its URL.
- **Microsoft Learn's provisioned throughput guidance**, for how PTUs are billed and how many
  tokens a minute one PTU serves for each model.

The seed covers Azure Commercial and Azure Government. In Azure Commercial it prices GPT-3.5
Turbo, the GPT-4o, GPT-4.1, and GPT-5 families, o1, o3, o3-mini, o4-mini, the text embedding
models, DeepSeek-R1, Llama 3.3 70B Instruct, and Phi-4, in each deployment type the API lists:
Global, Data Zone, and regional Standard, and Global and Data Zone Batch. In Azure Government it
prices the models and types the API lists there, a shorter list. It also has PTU hourly rates for
every model OpenAI, DeepSeek, Meta, and Mistral AI sell in Azure.

MOSAIC never makes a price up. A model the API doesn't list has no seeded price, and stays unpriced
until an administrator enters or publishes a suitable billing-derived rate. These have none:
- models sold through Azure Marketplace, such as Anthropic's and Cohere's;
- model versions the API lists under another name, such as Mistral Large 2411;
- models billed differently for audio and text tokens, such as the realtime models, because
  MOSAIC's figures don't tell those tokens apart.

## Billing data: import, review, publish

**Pricing > Billing data** accepts an uploaded extraction or a read-only connection to Azure,
AWS, or GCP. Both paths use the same normalization and review pipeline. No billing data changes a
price until an administrator chooses **Publish rates**.

1. Choose the **pricing cloud** and **source month**. The default is the previous complete UTC
   calendar month (`YYYY-MM`); current/future months are rejected. The pricing cloud is a price
   namespace, not a billing identity, management connection, or runtime endpoint.
2. **Import billing file**, or create a connection, **Check access**, then **Fetch month**.
   The import editor provides required/optional columns, sample CSV, and the GCP extraction SQL
   from `GET /api/v1/pricing/billing/templates`. Use those schemas rather than a cost summary.
3. Review eligible and excluded spend, raw/eligible/excluded row counts, source/invoice months,
   issues, existing versus proposed rates, quantities, and normalized evidence. Unknown units,
   ambiguous models, missing benefit allocations, missing required token dimensions, and
   unsupported capacity or modality need resolution, not a guessed rate.
4. Bind each included candidate to the correct endpoint/deployment. Confirm ambiguous or
   intentional cross-account matches explicitly; the same model name is not proof of the same
   agreement. A deliberately confirmed cloud/model scope is broader than a deployment binding.
   Exclude candidates you cannot substantiate. Replacing a manual override requires a separate
   explicit acknowledgement.
5. Save mappings, review the **effective date**, and **Publish rates**. The default is the UTC
   approval date, not the source month's first day. Backdating requires acknowledgement because
   it recalculates past reports and can change budget status. A stale preview or concurrent edit
   must be reloaded and reviewed before retrying.

Rates are quantity-weighted: sum the eligible USD cost (including separately attributable amortized
benefit cost) and divide by the normalized quantity, then express the result per million input or
output tokens, per PTU-hour, or per explicitly attributed deployment-month. MOSAIC does not average
row prices, divide spend by requests, or invent separate input/output prices from blended totals.
Accounts/resources, model versions, regions, service tiers, routing, and usage categories remain
separate during derivation. A partial candidate must not replace a usable complete price.

Only **USD-denominated** rows are accepted. A mixed/non-USD file fails explicitly; auxiliary
`CostInUSD` or currency-conversion columns are not an FX service. The basis is
`contractAmortized`: negotiated consumption plus attributable commitments, excluding tax and
one-off credits. Discounts already reflected in a net/effective field are not applied again.
Zero cash-charge prepaid usage without its amortized allocation is not free usage.

### Supported files and limits

| Source | Supported extraction | Required basis and important limitations |
| --- | --- | --- |
| Azure | Cost Details `.csv` / `.csv.gz`: `Date`, `MeterCategory`, `MeterName`, `Product`, `Quantity`, `UnitOfMeasure`, `CostInBillingCurrency`, `BillingCurrencyCode` | Export **AmortizedCost**. Preserve meter/resource, region, pricing-model, benefit, charge-type and additional-info fields where available. ActualCost rows lacking prepaid allocation are blocked. Only recognized model/token/PTU meters can become rates. |
| AWS | CUR / CUR 2.0 `.csv`, `.csv.gz`, `.parquet`; legacy slash headers or snake-case columns | Preserve usage account/product/type/start/amount, pricing unit, line-item type/currency/unblended cost; include net cost and reservation/savings-plan effective-cost fields where applicable, plus model/resource/region. Cost Explorer aggregates are not supported. Bedrock ModelUnit-Hour is evidence, not an Azure PTU-hour. |
| GCP | Flattened `.csv` / `.csv.gz`, or nested `.json`, `.jsonl`, `.ndjson` | Preserve billing account, service/SKU description, usage start, amount in pricing units and pricing unit, `cost`, `currency`; keep project/resource/region, `cost_type`, invoice month and signed credit records. Filter by **usage time**, not invoice month. Recurring eligible benefit credits retain their sign; promotions/free-tier credits are excluded. Unallocated commitments block affected rates. |
| MOSAIC | `.csv` / `.csv.gz` with `schema_version=mosaic_billing_v1` | Required: `provider`, `pricing_cloud`, `source_month`, `model`, `usage_category`, `usage_quantity`, `usage_unit`, `currency`, `eligible_cost_usd`. Include account/resource and model/version/region/type/tier/routing facts; optional `amortized_benefit_cost_usd` is additive, so do not include it twice in eligible cost. This is a trusted administrator extraction, not independent proof of a contract. |

Default shared bounds are **15 MiB input**, **50 MiB decompressed**, and **100,000 rows** across
the acquired parts, not a truncated sample. Gzip is bounded during decompression. PDFs, Excel,
ZIP archives, arbitrary schemas, and cost-only summaries are not accepted. The CUR Parquet reader
supports a constrained set of encodings/compression (uncompressed, Snappy, Gzip); unsupported
Parquet features require a supported CSV extraction. Extract the relevant detailed model rows
upstream when a full cloud bill exceeds the bounds; retain quantities and benefit attribution.

### Connections and least privilege

MOSAIC **does not create exports, buckets, datasets, roles, trust relationships, or billing
permissions**. An administrator provisions them first. Connections read billing data only; they
do not deploy models or grant runtime access. Check dataset access, not just successful sign-in.
Provider operations and access checks can incur object-read, query, storage, and network charges.

**Azure**

- Choose Commercial or Government, the existing Azure management connection (or host identity),
  billing/subscription scope, and `costDetailsApi` or `exportManifest` mode.
- Cost Details requests `AmortizedCost` asynchronously, honors polling/backoff and reads all
  report partitions. It supports up to 13 months' lookback and 64 partitions by default.
  Management-group scopes are unsupported. Subscription and supported billing-account/profile,
  invoice-section, enrollment-account, department and customer scope shapes are recognized;
  agreement/cloud support is still subject to the provider's actual API availability.
- Grant **Cost Management Reader** at the narrowest applicable Azure scope, or the agreement's
  corresponding enrollment/billing-profile read role and required cost visibility. A generic ARM
  Reader on the model account does not establish access to a billing agreement. Use **Check
  access** and correct the reported scope/agreement/permission issue; do not broaden to Owner.
- Export mode reads an existing amortized manifest and every CSV/Gzip partition from official
  cloud-specific Blob hosts. Supply a credential-free HTTPS manifest URL, with identity-based
  **Storage Blob Data Reader** access to its container. Blob reads acquire a separate
  `https://storage.azure.com/.default` token rather than reusing the ARM token; the Storage
  audience is the same in Commercial and Government while the identity authority remains
  cloud-specific. Configured manifest URLs must not embed SAS/query credentials. Provider-issued
  SAS partition URLs are read without a bearer token; unsigned Blob partitions use the Storage
  token. Arbitrary/private download hosts are refused. Configure an export for the selected
  observation month; MOSAIC does not create it or silently substitute ActualCost/retail prices.

**AWS**

- Configure the IAM role ARN, AWS region, CUR S3 bucket and bounded prefix, optionally an export
  name/account identifier. The connector selects one authoritative manifest revision for the
  month and reads every referenced object rather than double-counting export reruns.
- The default auth path exchanges a short-lived Entra OIDC token for STS
  `AssumeRoleWithWebIdentity`; configure the AWS OIDC provider and role trust for the API
  identity's actual issuer, audience and subject. `awsAudience` defaults to
  `api://AzureADTokenExchange`. This is not an implicit AWS ambient credential chain or a
  runtime Bedrock key. `awsExternalId` is not supported by direct
  `AssumeRoleWithWebIdentity`; that combination is rejected. External-ID trust requires signed
  `AssumeRole`, which the built-in connector does not perform. For that trust model, obtain
  short-lived role session credentials outside MOSAIC with signed `AssumeRole`, store those
  resulting credentials in the vault, leave `awsExternalId` unset, and arrange their rotation.
  Give the external source identity only `sts:AssumeRole` on the one billing-reader role.
- Restrict the role to `s3:ListBucket` on the one bucket with the configured prefix condition,
  and `s3:GetObject` on that prefix's manifest/report objects. Add only the necessary
  `kms:Decrypt` permission if the export is KMS-encrypted. No Bedrock invocation, Cost Explorer,
  export creation, or S3 write permission is needed.
- Vault alternatives are a raw web-identity token, JSON with `webIdentityToken`, or JSON with
  `accessKeyId`, `secretAccessKey`, and optional `sessionToken`. Explicit keys are used directly
  for the S3 reads; MOSAIC does not use them to assume the configured role. Stored temporary
  tokens/session keys do not renew themselves; rotate them before expiry. Federation is
  preferable for schedules.
  Default discovery is bounded to five listing pages and 50 report objects; narrow the prefix
  or upload a supported extraction when those limits are exceeded.

**GCP**

- Configure query project, dataset, detailed export table (or schema-compatible restricted view),
  location, optional billing-account filter, and `gcpMaxBytesBilled`. The default job cap is
  **10,000,000,000 bytes**; the accepted configured range is 1,000,000–100,000,000,000 bytes.
- Give the querying identity **BigQuery Job User** on the query project and **BigQuery Data
  Viewer** only on the necessary table/view (with authorized-view access to the underlying data
  when appropriate). It needs table metadata/schema access plus query-result reads, not billing
  administrator or data-editor permissions. OAuth must permit `jobs.insert` (the `bigquery` or
  `cloud-platform` scope); the narrower `bigquery.readonly` OAuth scope cannot submit these jobs.
  Dataset/table read-only enforcement comes from IAM, not that unsupported job-submission scope.
- Prefer Workload Identity Federation via `gcpWorkloadIdentityAudience`, optionally
  `gcpServiceAccountEmail` for impersonation. Establish the external identity trust and the
  narrowly scoped service-account impersonation permissions yourself. Alternatives are vault
  `service_account` JSON or supported `external_account` JSON. The latter uses an embedded
  `subject_token` or the API's Entra credential; arbitrary Google ADC file/URL/executable
  `credential_source` providers are not implemented. A stored subject token expires.
- Check access reads table/schema and performs a dry run. Acquisition uses parameterized usage
  bounds and a deterministic job ID with polling/pagination. A view must expose the nested fields
  referenced by the query (`service`, `sku`, `usage`, `location`, `resource`, `price`, `invoice`,
  and `credits`), not merely pass the minimum schema check. For a flattened extraction use the
  documented upload SQL below instead.

### GCP flattened extraction query

Run Standard SQL with timestamp parameters `usage_start` (inclusive) and `usage_end` (exclusive),
the first instants of the chosen and following UTC months. Replace only the fictional table
identifier with your configured detailed export. Preserve credits as JSON rather than joining
`UNNEST(credits)` into the outer result, which would duplicate usage/cost.

```sql
SELECT
  billing_account_id, project.id AS project_id,
  service.description AS service_description,
  sku.id AS sku_id, sku.description AS sku_description,
  usage_start_time, usage_end_time,
  usage.amount_in_pricing_units AS usage_amount_in_pricing_units,
  usage.pricing_unit AS usage_pricing_unit,
  cost, cost_type, currency,
  invoice.month AS invoice_month, location.region AS location_region,
  resource.name AS resource_name,
  price.effective_price AS price_effective_price,
  TO_JSON_STRING(credits) AS credits
FROM `example-project.billing_export.gcp_billing_export_resource_v1_EXAMPLE`
WHERE usage_start_time >= @usage_start
  AND usage_start_time < @usage_end
```

### Management credential vault

Use the isolated **API-only management credentials vault**, configured by
`MOSAIC_MANAGEMENT_KEY_VAULT_URI`, not the backend runtime-key vault. Grant the API identity secret
read/write/delete access as required by the existing management-vault setup (for example **Key
Vault Secrets Officer** on this dedicated vault); gateways must have **no access**. See
[Azure Government management setup](azure-government.md) for the existing vault separation.

The connection editor accepts write-only JSON/token material. APIs also accept
`credentialSecretUri` referencing a secret in the configured management vault. Saved connection
views expose whether a secret exists, never its value; Cosmos stores references, not credential
values. Rotation/clearing/deletion cleans up only secrets MOSAIC created and owns. Externally
supplied secret references are never deleted by these connection operations; their owner remains
responsible for rotation and retirement. Do not put
secrets in source files, screenshots, source-scope labels, URLs, or billing evidence.

Configure the vault before saving production billing credentials; a local in-memory development
store is **not durable or an acceptable production credential store**. The API identity also
needs network access to the vault
and provider endpoints. The adapters use provider HTTP/token-exchange protocols, not a general
AWS/GCP SDK credential-discovery chain.

### Schedules, late data, and recovery

- Each connection's monthly schedule is **off by default**. Enable it explicitly, choose day
  1–28, time and IANA time zone (default day 3, 04:00 UTC). The serialized field is `timeUtc`,
  but the scheduler interprets it in `timeZone`; use UTC if you want UTC execution. The target is
  the preceding calendar month in that schedule's zone. If the local scheduled time falls
  before that month has ended in UTC, the worker waits until UTC month-end before starting it.
- `MOSAIC_BILLING_REFRESH_ENABLED=true` (default) runs the worker for polling and opted-in monthly
  drafts. `MOSAIC_BILLING_REFRESH_INTERVAL_SECONDS=300` (30–86,400) controls its tick. Keep at least
  one API replica awake and set `MOSAIC_SCHEDULED_WORKERS_ALWAYS_ON=true` only when hosting
  actually guarantees it. Cosmos supplies durable state/coordination; memory resets at restart.
  `GET /api/v1/pricing/billing/scheduler` reports readiness, in-flight work, warnings and errors.
- Persisted runs/operation IDs and leases coordinate polling and monthly acquisition. A tick
  after a missed time can catch up the latest due month, not every missed historical month.
  Existing non-failed work for that connection/month suppresses another scheduled run.
  A failed acquisition is recorded; inspect it and retry/fetch explicitly rather than expecting
  unbounded automatic retries. If the worker is disabled, use **Check status** for in-flight work.
  Interactive status checks and background polls share the same per-run lease. A heartbeat
  extends ownership during provider waits without releasing the lease; losing ownership or
  cancelling the request stops retrieval before its results are published.
- Scheduled work **only creates drafts**. It never approves or activates prices. Failed, empty,
  incomplete, or expired-credential retrievals leave the approved book intact. Schedule and
  notification preferences are independent.
- A complete month is not a settled invoice. Late provider rows, credit true-ups, or corrected
  manifests require another extraction/retrieval and review. Exact duplicate uploads return the
  prior run unless forced through the API. A changed source produces a new draft and may
  supersede an earlier draft, never an approved list. `forceRerun` permits an explicit connected
  rerun; an older completed month can be selected.

### Evidence, activation, and cost effects

MOSAIC retains tenant-scoped normalized evidence in bounded chunks: source scope/month,
acquisition time, manifest/revision or content fingerprint, cost basis, currency, derivation
version, units/quantities, eligible/amortized/excluded amounts and reasons, mapping decisions,
and publication identity. Raw upload bodies are discarded after normalization, not kept as
invoice archives. Do not upload contact details or unrelated invoice data. Sources/history use
internal `mosaic://pricing/billing-runs/...` evidence references rather than fake public URLs;
only administrators can inspect account-level evidence. Portal users get cost-basis information,
not the billing dataset or credentials.

Publication prepares immutable dated price versions, then activates them with one
compare-and-swap-protected publication marker. A failed preparation must not expose half a list.
Deployment-bound approved rates outrank unrelated global seed refreshes; a broader cloud/model
rate is not a deployment override. A later source month does not retrospectively change an earlier
approved rate without a newly approved effective date.

Analytics, Dashboard spend/forecasts, chargeback, portal usage, and budgets read the same price
book. Public/manual/billing-derived/mixed provenance describes that basis; unpriced usage is
still excluded, never invented as zero. A zero denominator or missing required dimension is a
review issue, not a zero price. A supported zero-cost rate needs positive quantities and
substantiated free consumption, not a missing prepaid benefit.

Approval can raise or lower projected spend. Backdating can revise historical reports and the
current month's budget status. The existing budget worker re-evaluates and can block or unblock
cost centers at its next check; a billing connector never writes gateway access policies itself.
Export/query charges and excluded tax/credits do not become caller spend merely because the
source contains them.

## Clouds

A price is for one cloud. An ARM-managed Azure deployment's default pricing cloud comes from its
explicit resource cloud, not the cloud hosting MOSAIC. URL-only endpoints use their supported
Azure AI host family:

| Host ends in | Cloud |
| --- | --- |
| `.azure.us` or `.usgovcloudapi.net` | Azure Government |
| `.azure.com` | Azure Commercial |
| Anything else | None until an administrator names one |

On the **Endpoint facts** tab, an administrator can override the cloud MOSAIC detected for an
endpoint, or name one for an endpoint whose host doesn't say. A cloud other than Azure Commercial or
Azure Government is a custom one, such as `openai` for an OpenAI-compatible provider, or a name for
another sovereign cloud. Its key is up to 40 lowercase letters, digits, and hyphens. MOSAIC ships no
prices for custom clouds, so their prices are entered like any other.

These overrides affect prices only. They never change a resource's Azure management connection,
credential authority, runtime cloud, or same-cloud publishing rules.

The same tab can override an endpoint's region, which MOSAIC otherwise reads from the endpoint.
Choosing the cloud MOSAIC detected clears the override. If another administrator saves an
endpoint's facts after you open its form, your save is refused rather than undoing theirs. Choose
**Load the latest** to see what they saved, then make your change again.

## How a deployment finds its price

MOSAIC prices each deployment by its facts:
- **Model, version, and publisher**, from the deployment MOSAIC observed.
- **Deployment type**, the deployment's SKU, such as `GlobalStandard`, `DataZoneStandard`,
  `Standard`, `GlobalBatch`, or `ProvisionedManaged`.
- **Capacity**, for provisioned throughput, the deployment's PTUs.
- **Cloud and region**, from its endpoint, unless an administrator overrode them.

Deployments declared on an endpoint reached with an API key have no SKU MOSAIC can read. Set their
deployment type, and the PTUs of a provisioned one, on the **Endpoint facts** tab. MOSAIC
uses the type Azure reports whenever there is one, and doesn't let an administrator override it.

A price applies to a deployment when it's for the deployment's cloud, it names the deployment's
model, one of the model's aliases, or every model of its publisher (`*`), and every other fact it
names matches. A price that leaves a fact out applies whatever that fact is. The most specific price
that applies wins:
1. a price for that one deployment;
2. then a price that names the model, before one that matches an alias, before one for every
   model;
3. then one that names the version;
4. then one that names the region;
5. then one that names the deployment type;
6. then one that names the publisher.

So when no price names the deployment's region, one without a region applies, and when none names
its version, one without a version applies. Between equally specific prices, the latest to take
effect wins. Between two with the same date, an administrator's beats a seeded one, and then the one
recorded last wins. A token price applies only to a deployment that isn't provisioned, and a PTU
rate only to one that is.

A model API's calls are priced by the deployment its publication fronts. A grant's calls are priced
by the model API it grants. An API MOSAIC adopted rather than published doesn't say which deployment
it calls, so its calls have no price. MCP servers carry no tokens and have no cost.

A [model pool](model-pools.md)'s API fronts many deployments, so MOSAIC prices each of its
calls at the member that served it. It places a call on a member by the backend the gateway logged,
or by the host and deployment that backend calls. A provisioned member's cost is shared among the
pool's callers like any other's, from the day MOSAIC first saw it behind the pool, and a member
since removed keeps its price for the days it served. A call MOSAIC can't place on a member has no
price, and the chargeback bills it to **Unknown model**. Calls an MCP server's application makes
through a pool for the people who called the server are priced the same way.

## Why a deployment has no price

The **Unpriced deployments** tab lists every deployment MOSAIC knows that has no price today, every
adopted model API with calls in the last 30 days, and every model pool with calls in that time that
MOSAIC couldn't place on a member, busiest first. Each row says why:

| Reason | What to do |
| --- | --- |
| Cloud unknown | Name the endpoint's cloud on the **Endpoint facts** tab |
| Deployment type unknown | Set the declared deployment's type on the **Endpoint facts** tab, or add a price for every type |
| PTUs unknown | Set the provisioned deployment's PTUs, or enter a monthly amount for it |
| No price listed | Add a price for the model in its cloud, from a source you trust |
| Price not yet in effect | Wait for the price's date, or add one that takes effect sooner |
| Not yet deployed | Nothing. A provisioned deployment costs nothing before it existed |
| Deployment unknown | Publish the model through MOSAIC, so it knows the deployment, or accept that the adopted API's calls are unpriced. For a model pool, check that the gateway's logs name the backend each call reached |

## Adding and correcting prices

On the **Pricing** page, **Add price** enters a new price, and **Override** on a price enters a new
version of it. Each needs:
- the cloud, the model, and optionally the publisher, version, deployment type, and regions;
- either prices per million input, cached input, and output tokens, a rate per PTU an hour, or,
  for one provisioned deployment, an amount each month;
- the day it takes effect;
- a source URL, such as a contract or a price page;
- a note saying why, for whoever reads the history.

Prices are never edited or deleted. Each one is a new version, recorded with who entered it and
when, and audited as `pricing.priceRecorded`. **History** on a price shows every version:
- **In effect:** the version that prices today.
- **Scheduled:** a version that takes effect later.
- **Superseded:** a version a later one replaced, or a seeded price a newer seed replaced, which
  shows the day it ended.
- **Corrected:** a version a later one with the same date replaced.

A new version prices only the days from its date. Days before it keep the price they had, so
recording a new price never changes the cost already reported. To correct a mistake, enter a version
with the same date as the wrong one. It beats a seeded price with that date, even after a later
release ships the seed again, and the latest of several corrections wins. Every report shows the
corrected cost the next time it's read. To change the price of days already reported, backdate the
version to the first wrong day.

A version with a later date can replace an equally scoped manual override. When Azure changes a list
price, the next release's seed brings the new price from the day it changed, and it replaces an
override that took effect before then. After upgrading, check **History** on the prices you've
overridden, and enter your price again from the new date if it should still apply. An approved
deployment-bound billing rate is more specific than the global seed and is not displaced by it.

## Provisioned throughput

A provisioned deployment costs its capacity whether or not anyone calls it:
- **By the hour:** its PTUs times the PTU hourly rate, for every hour of the month. The seeded rates
  are Azure's hourly list prices.
- **By the month:** an amount you enter for that one deployment, such as a reservation's monthly
  cost. Choose the deployment and **Monthly amount** when adding the price. It's spread evenly over
  the days of each month.

MOSAIC charges a deployment from the day Azure created it. When it can't read that, it charges from
the day it first saw a governed API that fronts the deployment.

Each month's cost is shared among the deployment's callers by their share of its tokens that month,
whichever day they called, counted across every gateway. Filtering Analytics to one gateway doesn't
inflate anyone's share. A month's total can trail its days when a rollup cycle fails part way, so
MOSAIC also adds up the days, for every month it still keeps them, and uses the larger figure. A
month nobody called the deployment, its cost is idle: Analytics adds it to totals, and the
chargeback export charges it to **Reserved capacity with no calls**.

The **Deployments** table on the **Cost** tab shows each provisioned deployment's monthly cost,
idle cost, and utilization: its tokens against what its PTUs could serve in the range, by Microsoft
Learn's figures for the model. Output tokens count several times over, as those figures say.
Utilization is blank for a model Learn has no figures for.

## Reading cost

- **Analytics > Cost** shows the total and trend for the chosen range, cost by model, deployment,
  caller, and API, and this month's spend and forecast. **Cost by consumer** ranks the people and
  applications that called, so a security group's calls count once, under its members. The other
  tabs add a cost column where usage has one. A call whose LLM log named no model counts under the
  model MOSAIC knows its deployment serves, so cost by model adds up to the same total as cost by
  API.
- **Spend this month** is the calendar month so far, in UTC. The **Month-end forecast**, marked
  **Projected**, is pay-as-you-go spend so far, times the days in the month, divided by the days
  MOSAIC has figures for, to the hour, plus each provisioned deployment's whole month, from the
  day it was deployed. It appears once MOSAIC has a day of figures. The Dashboard shows both.
- **Monthly totals:** a range reaching past daily retention reads whole months. If a price changed
  in such a month, the month is priced as if its calls were spread evenly across it, and the
  report says so.
- **The portal** shows each person the cost of their own calls, per grant and in total, and how many
  of their resources have no price. A grant only some of whose calls have a price shows the priced
  part, says why the rest has none, and counts among the resources the total leaves out. Model
  calls an MCP server's application made for them are listed apart, priced the same way and named
  with the cost center of the application's grant, which paid for them. Their totals leave them
  out. A model in a model pool is named by its display name, never by its pool.

## Chargeback

On the **Cost** tab, choose **Chargeback by month** under **Export**, then **Export CSV**. The file
has one row per month, party, model, deployment, and endpoint, with requests, prompt, completion,
and total tokens, cost, and whether the row was priced fully, partly, or not at all. A note says
why a row isn't fully priced. **Cost by deployment** exports the Cost tab's deployments table.

A grant's calls are charged to its subject: the person, application, or security group it was
granted to, with its Entra object ID. The **Cost center** and **Cost center name** columns after
**Object ID** name the [cost center](cost-centers.md) the grant charges, so the same subject has a
row for each of its cost centers. Calls MOSAIC couldn't attribute are charged to
**Unattributed calls**, and idle reserved capacity to **Reserved capacity with no calls**. Neither
belongs to a grant, so their cost center is empty, and no cost center's spend includes them. Rows
are split by calendar month, and the first and last month are cut to the chosen range. Filtering
the Cost tab by cost center exports only that cost center's rows.

**On behalf of** and **On behalf of object ID**, after **Cost center name**, split out the model
calls an MCP server's application made for the people who called that MCP server
([MCP servers that call models](mcp-servers-that-call-models.md)):
- Each person gets a row of their own, with their calls, tokens and cost. The row is still charged
  to the application, under its grant's cost center, because the application's grant paid for the
  calls. The person is a reporting dimension only. On a model pool's grant, each person gets a row
  for each member that served their calls.
- The rest of the application's calls, its own use and any MOSAIC couldn't attribute to anyone,
  stay one row with both columns empty. A row that isn't an application's grant's leaves them
  empty too.
- The split rows add up exactly to the row they replace: the same requests and tokens, and the
  same cost to the ten-thousandth of a dollar. The row's rounded cost is shared in proportion to
  what each part's calls cost, and what rounding leaves over goes to the largest remainders, so a
  person's row can differ from their figure in Analytics by a ten-thousandth of a dollar.

## Refreshing the seed

Azure's list prices change. Between releases, enter newer prices as versions. To refresh the seed
itself, run this from the repository root, with the workspace's virtual environment:

```powershell
python -m scripts.price_seed --check   # what would change, without writing anything
python -m scripts.price_seed           # rebuild model_prices.json and its schema
```

The script queries the Retail Prices API again, keeps only prices in effect on the day it runs, and
writes both files. `--cache <folder>` keeps each query's results so a second run doesn't fetch them
again, and `--date` records another day as the day the prices were read.

The API lists only today's prices, so a refresh keeps each price it replaces. The old price gets an
`effectiveUntil`, the day before its replacement takes effect, and cites its source as it was read
before. So the days it priced keep their price. A price for every region can change region by
region, so each region keeps the old price until the day the API dates its own change. A price
the API no longer lists is kept without an end. If the API dates a replacement before the day the
old price was last read, the refresh follows the API, which is the record, and prints a warning.

The script decides which meters make up each price from a catalog at its top. To add a model, add
its meters there and rebuild. Review the diff, and run the API's tests, which check that every price
cites a source the seed lists and that the file matches its schema.

## Limits

- Costs use the applicable approved/manual/public rate, not full invoice reconciliation. No
  administrator percentage or fixed-dollar markup policy is implemented; billing-derived pricing
  does not complete the additional markup requirements in issue #165.
- No FX, tax allocation, arbitrary commitment allocation, invoice payment, or new AWS/GCP public
  seed is included. Models without a supported imported/manual/fallback rate remain **No price**.
- Connected retrieval is implemented and tested with synthetic provider responses; those tests
  and the fictional screenshots do not prove live customer-account connectivity.
- Cached prompt tokens are priced at the full input rate.
- MOSAIC prices tokens, not images, audio, or fine-tuning, and only for deployments it knows. Calls
  through an adopted API have no price.
- Ranges by the hour show no cost.
- Seeded prices are only as current as the release. Check the **Sources** tab for the day they were
  read.
