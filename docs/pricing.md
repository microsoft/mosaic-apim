# Pricing

This guide is for administrators who run MOSAIC. It explains where MOSAIC's prices come from, how
it finds the price of each deployment, how to add and correct prices, how provisioned throughput is
charged, and how to refresh the prices MOSAIC ships. The README's
[Usage and analytics](../README.md#usage-and-analytics) section lists the views and routes,
[Usage analytics](usage-analytics.md) explains the usage that's priced, and
[ADR 0020](adr/0020-price-list.md) records the design.

## What MOSAIC's costs are

MOSAIC multiplies the usage it measures by a price list. Every cost is:
- **An estimate at list price**, in US dollars, before any discount, commitment, or reservation.
  It isn't a bill. Cost Management has what Azure actually charges.
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
the full input price. Cost is shown for whole days only, so the **Last 24 hours** range shows none.

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
until an administrator enters one. These have none:
- models sold through Azure Marketplace, such as Anthropic's and Cohere's;
- model versions the API lists under another name, such as Mistral Large 2411;
- models billed differently for audio and text tokens, such as the realtime models, because
  MOSAIC's figures don't tell those tokens apart.

## Clouds

A price is for one cloud, and a deployment's cloud comes from its endpoint's host:

| Host ends in | Cloud |
| --- | --- |
| `.azure.us` or `.usgovcloudapi.net` | Azure Government |
| `.azure.com` | Azure Commercial |
| Anything else | None until an administrator names one |

On the **Clouds and endpoints** tab, an administrator can override the cloud MOSAIC detected for an
endpoint, or name one for an endpoint whose host doesn't say. A cloud other than Azure Commercial or
Azure Government is a custom one, such as `openai` for an OpenAI-compatible provider, or a name for
another sovereign cloud. Its key is up to 40 lowercase letters, digits, and hyphens. MOSAIC ships no
prices for custom clouds, so their prices are entered like any other.

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
deployment type, and the PTUs of a provisioned one, on the **Clouds and endpoints** tab. MOSAIC
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

## Why a deployment has no price

The **Unpriced deployments** tab lists every deployment MOSAIC knows that has no price today, and
every adopted model API with calls in the last 30 days, busiest first. Each row says why:

| Reason | What to do |
| --- | --- |
| Cloud unknown | Name the endpoint's cloud on the **Clouds and endpoints** tab |
| Deployment type unknown | Set the declared deployment's type on the **Clouds and endpoints** tab, or add a price for every type |
| PTUs unknown | Set the provisioned deployment's PTUs, or enter a monthly amount for it |
| No price listed | Add a price for the model in its cloud, from a source you trust |
| Price not yet in effect | Wait for the price's date, or add one that takes effect sooner |
| Not yet deployed | Nothing. A provisioned deployment costs nothing before it existed |
| Deployment unknown | Publish the model through MOSAIC, so it knows the deployment, or accept that the adopted API's calls are unpriced |

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

A version with a later date replaces an override, wherever it comes from. When Azure changes a list
price, the next release's seed brings the new price from the day it changed, and it replaces an
override that took effect before then. After upgrading, check **History** on the prices you've
overridden, and enter your price again from the new date if it should still apply.

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
  out.

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
  calls. The person is a reporting dimension only.
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

- Costs are list prices. Discounts, commitments, reservations, and taxes aren't applied unless you
  enter them as prices.
- Cached prompt tokens are priced at the full input rate.
- MOSAIC prices tokens, not images, audio, or fine-tuning, and only for deployments it knows. Calls
  through an adopted API have no price.
- Ranges by the hour show no cost.
- Seeded prices are only as current as the release. Check the **Sources** tab for the day they were
  read.
