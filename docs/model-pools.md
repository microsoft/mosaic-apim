# Model pools

[Documentation](README.md) | [MOSAIC](../README.md)

- [Member compatibility](#member-compatibility)
- [Routing and failure handling](#routing-and-failure-handling)
- [Runtime request handling](#runtime-request-handling)
- [Publishing and ownership](#publishing-and-ownership)
- [Claude on AWS Bedrock](#claude-on-aws-bedrock)
- [Who can call a pool](#who-can-call-a-pool)
- [Applying access safely](#applying-access-safely)
- [Cost and usage](#cost-and-usage)
- [Portal and console](#portal-and-console)
- [Health](#health)
- [Drift and duplicates](#drift-and-duplicates)
- [Suggestions and API reference](#suggestions-and-api-reference)
- [Deferred work and verification](#deferred-work-and-verification)

A publication exposes one deployment. When the same model is deployed on several endpoints, in
several regions, or with both provisioned and pay-as-you-go capacity, publishing each deployment
gives callers several APIs for one model, and leaves them to pick one and to retry elsewhere when
it's throttled. A **model pool** serves one vendor's models from all of those deployments through
one API. Callers send a model name, such as `claude-opus-4-5`, to one base URL, and the gateway
sends each request to a deployment that can take it. They never see the endpoints, regions, or
deployments behind the pool. [ADR 0024](adr/0024-model-pools.md) records the design. Its first
four phases are built, and this guide describes them.

## Member compatibility

A pool runs on one gateway and takes that gateway's environment. It serves one vendor's models
through one API shape ([ADR 0012](adr/0012-format-aware-model-publishing.md)), such as
Anthropic Messages for Claude. Each **pool model** has the public name callers send, and its
**members**: deployments of that model on any endpoint the gateway may front, including Claude
models on [AWS Bedrock](model-endpoints.md#aws-bedrock-endpoints).

- A model's members must serve the same model in the same format. Members on different versions
  are refused unless the administrator allows mixed versions for that model.
- Each member is judged as a publication's deployment is. It must be in the endpoint's latest
  inventory and provisioned, or, on an endpoint reached with an API key, declared on it. ADR 0013's
  *cannot invoke* blocks a plan, and *not confirmed* is a warning. ADR 0014 judges the gateway
  against every member's endpoint, so a *blocked* member is refused, and a warning is listed in the
  plan.
- Batch deployments can't be members, because they don't answer requests as they arrive.
- Members that process data in different scopes, such as regional and global, are a plan warning.
  So is a member with Azure's own spillover turned on, and a deployment that also serves another
  pool, because the pools then share its capacity.
- Where callers name the model in the request body, as Anthropic Messages and Foundry Models do,
  the policy replaces it with each member's deployment name or Bedrock model ID. In a breaker or
  preferential pool, API Management picks among the members it reaches with the gateway's
  identity, so those must share one deployment name. Members reached with a key, and a linear
  pool's members, have no such limit.

## Routing and failure handling

Administrators choose one of three types for a pool, and MOSAIC derives what it
writes from it:

| Type | How requests are spread | When a deployment throttles |
| --- | --- | --- |
| **Breaker** | One API Management backend pool over every member, by weight | Its circuit breaker opens for its `Retry-After`, and the request is retried on another member |
| **Preferential** | One backend pool with provisioned members at priority 1 and every other member at priority 2 | Provisioned capacity takes every request while any of it is available. A throttled member is skipped until its `Retry-After` passes, and overflow spreads over the priority 2 members |
| **Linear** | Each member in the administrator's order, with no breakers | Each request starts at the first member and moves down the list. Nothing is taken out of rotation |

- **Failure handling.** **Throttling**, the default, moves a request on after a 429 or a 503, and
  trips a breaker on one 429. **Throttling and errors** also moves it on after a 500, 502, or 504,
  and trips a breaker on three such failures in a minute.
- **Breaker timing.** The generated presets honor `Retry-After` and use 10- or 30-second fallback
  trips. There is not one universal one-minute wait.
- **Retries** are immediate, because waiting out `Retry-After` is the breaker's job. A breaker or
  preferential pool retries up to three times by default, and never makes more attempts than the
  model has active members. A linear pool tries each member once, and no request makes more than
  10 attempts. Streamed responses aren't held back.
- **The safeguard**, optional, is a token limit every caller of a pool model shares, counted per
  model, so the pool's own callers can't exhaust its deployments.
- **Draining** a member keeps it in the pool's definition and takes it out of routing at the next
  apply.
- A Consumption-tier gateway has no backend pools, so it runs only linear pools.
- **Members reached with an API key**, on an Azure endpoint registered with a key or on AWS
  Bedrock, can't join an API Management backend pool, so each is a target of its own. In a breaker
  or preferential pool, the gateway tries each once, in the pool's order and with no circuit
  breaker, after the backend pool's attempts, or as soon as the backend pool has no member left.
  The plan says so for each model that has one, and in a preferential pool it warns when a
  provisioned member is reached with a key, because it then comes after the backend pool. In a
  linear pool, they keep their place in the order. A model that would need more than 10 attempts
  is refused.

## Runtime request handling

The pool's policy reads the model a request names, from
the body's `model` or, for the Azure OpenAI shape, from the route, and refuses a model the pool
doesn't serve: an open pool answers `404`, and a governed one `403`. It removes
`x-ms-spillover-deployment`, so a caller can't steer Azure's spillover to a deployment outside the
pool. Each attempt authenticates with the gateway's managed identity, or with its member's API
key, which API Management reads from Key Vault. A pool with a member reached with a key first
removes every credential the caller sent. On the way out, the policy removes the headers that
describe one member: its region, deployment, spillover, rate limits, and the headers AWS adds, such
as its request ID and token counts. A request that is still throttled or failing after its last
attempt gets a generic error that names no member, and keeps its `Retry-After`. The Responses API
isn't offered, because a stored response lives in one account and a later call could reach
another, and neither is the model information route, which describes one deployment.

## Publishing and ownership

Applying a pool creates, in dependency order:

| Order | Resource | Purpose |
| --- | --- | --- |
| 1 | Named values | One per member reached with an API key, `<member backend>-key`, pointing at the key's Key Vault secret. Each is read back after it's written |
| 2 | Backends | One per active member, pointing at its deployment. In breaker and preferential pools, each member reached with the gateway's identity has a circuit breaker |
| 3 | Backend pools | In breaker and preferential pools, one per pool model, balancing its members reached with the gateway's identity by weight and priority |
| 4 | `mosaic-*` policy fragment | Finds the requested model, removes what a caller mustn't send on, gets the gateway's managed identity token when a member uses it, and applies the safeguard. In a governed pool, it also finds the caller's grant and applies its limits |
| 5 | API | The pool's API at its own path. It has no service URL, because the policy picks each request's backend |
| 6 | Operations | The shape's curated operations, without Responses and model information. A governed pool keeps only those its limits can count: chat completions, or Anthropic's messages |
| 7 | API policy | Includes the fragment, retries over each model's members, sets each attempt's credential when a member is reached with a key, and removes member headers from responses |
| 8 | Product | Carries the API |
| 9 | Product/API link | |
| 10 | Subscription | The open pool's own subscription. A governed pool suspends any it has, and each caller uses their own key |

Plans, applies, ownership, rollback, unpublishing, and recovery work as they do for a publication.
A plan is deterministic and reviewed, and pins each active member's environment verdict. Apply runs
only that plan, and a failed step reverses what its run created. Unpublishing runs a reviewed plan
over the pool's resources in reverse, so a backend pool goes before the backends it balances, and a
key's named value goes last. It refuses with `409` and `planRequired` or `stalePlan` as a
publication's does. The same guards hold:

- While a pool is applying or its gateway access state is unknown, editing, drain changes, access
  settings, plan review, unpublishing, and record removal are locked, including direct workspace
  links. Unknown state offers diagnostic recovery even when run history is unavailable; it does
  not confirm quiescence or release the write lock.
- A pool that still owns API Management resources can't be removed, and neither can a gateway with
  published pools.
- An endpoint, or one of its deployments, can't be removed while a pool may own a backend for it.
  Members no gateway holds are forgotten instead.
- An environment change that would block an applied member is refused.
- A pool's API isn't offered for import as a model API.

## Claude on AWS Bedrock

A Claude pool can take models on
[AWS Bedrock endpoints](model-endpoints.md#aws-bedrock-endpoints) as members, alongside Foundry deployments. Each is
reached with its endpoint's Bedrock API key, as a target of its own. MOSAIC never sends the key to
AWS, so a Bedrock member's readiness is never more than *not confirmed*, and the first call through
the pool is the key's first test. For each Bedrock member, the plan tells the administrator that AWS
processes the requests it serves, prompts included, outside Azure. For a cross-region inference
profile, the plan adds that AWS may process them in other regions too. Users aren't told, because
the portal never names a pool's members. An administrator who must tell them can say so in the
model's display name. A Bedrock member's calls stay unpriced until an administrator names its
cloud and adds a [price](analytics-reference.md#pricing).

## Who can call a pool

A new pool is open: its own subscription admits every caller, and an
administrator copies its key from the Azure portal. **Governed access** puts each of the pool's
models behind grants, as [governed model access](access.md#governed-model-access) does for a publication.
An administrator turns it on from the pool's page and chooses whether callers sign in with a key, a
Microsoft Entra token, or either. Once it's applied, the pool can't go back to open access.

For a custom [cost center](cost-centers.md), configure Automatic, Request-only, or Not available
for **each exact pool-model offering**, including both its model identity and pool scope.
Automatic supplies a stable group JWT grant after reviewed apply; adding members does not create
personal automatic entitlements or APIM policy writes. Caller-side Entra support is required,
regardless of whether a member backend uses a key. Request-only approvals remain individual;
Not available blocks new grants/requests server-side.

Both individual and group token grants require the custom center's verified Entra group claim.
Direct-grant precedence, defaults, and the cost-center header are not membership exceptions.
General is exempt from the mandatory group; Government is excluded from the new workflow.

- Entitlements and access requests name a pool model, such as Claude Opus 4.5 in the Anthropic
  Claude pool, and a [cost center](cost-centers.md). People, applications, and agents hold direct
  grants. Security group grants are Entra only, and grants to MOSAIC groups stay desired state,
  which the plan says.
- Each eligible person or application can explicitly create one key per pool and cost center when
  both pool and center allow keys, rather than receiving one from Automatic access or an apply.
  It serves every model they hold directly in that pool under that cost center, and
  goes in `Ocp-Apim-Subscription-Key` or the `subscription-key` query parameter. Generic, product,
  all-API, and all-access keys don't work, and the pool's own subscription is suspended.
- A key serves one cost center, so a call whose `x-mosaic-cost-center` names another is refused.
  An Entra caller with grants under several cost centers names one in that header.
- The policy finds the caller's grant for the model the call names. It then checks the cost center
  and its budget, the grant's limits, the cost center's pooled quota, and the safeguard, in that
  order, before it routes. It removes the caller's key, token, and cost center header, so no member
  receives them.
- A call with no credential, a malformed one, or one the pool doesn't accept gets `401`. A key the
  pool didn't issue gets `403`, and so does a model the caller holds no grant to. A model the pool
  doesn't serve is refused the same way, so a refusal never reveals what the pool serves.
- A governed pool permits only chat completions, or Anthropic's messages, because those are the
  calls its limits can count.
- A cost center's per-person defaults and pooled monthly quotas apply to pool models, counted across
  every region the pool serves the model from. Membership removal does not invalidate issued JWTs
  or copied keys. Recheck affected individual grants/keys and review/apply revocation; out-of-band
  directory edits need an explicit recheck, not a directory watcher. Revoking one model grant
  removes that model at the next pool apply, and the key is deleted when no eligible grants it
  serves remain. See [removal timing](cost-centers.md#removing-a-member).

## Applying access safely

A governed apply first denies every call. It then writes the pool's
backends, API, operations, product, and fragment, and reopens the pool on the reviewed policy. Keys
are suspended while it runs and reactivated only for grants still in force. Unpublishing and
recovery also deny calls before they touch a key. If an apply fails, MOSAIC restricts the pool to
its last safe access snapshot, which admits only grants both the previous and the reviewed policy
allowed. Keys are kept for a reviewed retry, and none is rotated.

- A model with applied grants can't leave its pool until they're revoked and the revocation is
  applied. A grant whose revocation isn't applied yet can't be deleted.
- The plan lists each grant it leaves out, and why. A grant is left out when its model has no
  active deployment, or its cost center is gone or has a recheck pending. So is a grant with token
  limits, or under a pooled monthly token quota, on a gateway tier that can't count tokens.

## Cost and usage

The policy writes the same usage traces as a governed publication's. MOSAIC
places each call on the member deployment that served it and prices it at that deployment's price,
so a model served from several regions, or from both provisioned and pay-as-you-go capacity, is
priced per region and capacity. A provisioned member's reserved capacity is shared by token share,
as it is for a publication, and a member removed from the pool keeps its price for earlier calls.
Calls MOSAIC can't place on a member are never counted as free. Pricing lists them as unpriced,
and the chargeback bills them as an unknown model. Cost center spend, budgets, Analytics, and the
usage report all include pool calls, and so does the model use an MCP server's application makes
through a pool for the people who call the server
([MCP servers that call models](mcp-servers-that-call-models.md)). Reports for users never
name the pool, its members, or their regions.

## Portal and console

The catalog lists each model a governed pool serves as a **Model**, under its
display name and the gateway's environment, with a capacity badge unless the pool hides capacity.
It has no summary, because a pool's description may name its regions. Users see inherited Automatic
access or request an eligible Request-only offering as they would any model. Once applied,
**My access** shows the pool's base URL, the model name to send, and
the safeguard as the model's shared limit. Revealing a key names the other models it works for.
The portal never names the pool, its deployments, or their regions.

In the console, **Pools** lists every pool with its gateway, what it serves, its routing, its active
members, readiness, whether callers need a grant, and status. **Create pool** chooses the gateway
and routing, then each model's deployments from every endpoint the gateway can front. It can
suggest weights from each deployment's capacity, and says why a deployment can't be used. A pool's
page shows its routing, what it owns in API Management, each model's deployments with their region,
capacity, weight and share, readiness, and a drain switch, how callers connect, what its policy
does, and its run history. A deployment reached with an API key is marked **API key**, and in a
breaker or preferential pool it shows when the pool tries it, in place of a weight. One an
administrator declared is marked **Declared**, and a model on AWS Bedrock, which is both, is marked
**AWS Bedrock**. The page's **Who can call it** card turns on governed access, and lists the
grants in force, their limits, and each cost center's pooled quota. The plan's **Pool access
review** shows what changes for callers before anyone applies it. **Entitlements** grants a pool's
models, **Approvals** says the pool's plan applies an approved grant, **Connections** shows the
models one key serves, and **Analytics** labels a pool grant with its pool and model. Saving a pool
records intent. Nothing changes in API Management until a reviewed plan is applied.

## Health

After every attempt, the pool's policy writes a trace naming the pool model, the
backend it sent the attempt to, the host and path it called, and the status it got. It never
records a query string, a header, or a body. A pool's **Health** card reads those traces from the
gateway's logs for the last hour, 6 hours, 24 hours, or 7 days:

- for each model, how many calls succeeded, how many were unavailable because the last deployment
  tried was throttled or failed, how many got a client error, how many were retried, and how many
  its overflow deployments answered;
- for each deployment, its attempts, successes, 429s, failures, and client errors, the minutes its
  breaker would have tripped, and its last attempt.

It reads the logs that [Measuring a gateway's usage](analytics-reference.md#measuring-a-gateways-usage) sets up. When
MOSAIC lacks permission to read them, the card shows the command that grants Monitoring Reader. A
pool applied before the trace existed shows its calls as untraced until it's applied again. The
newest calls take a few minutes to show.

## Drift and duplicates

A plan for a published pool warns when someone removed a resource MOSAIC
created for it, or changed its API policy or policy fragment, outside MOSAIC, since applying puts
them back. A pool's page and its plans also warn when portal users would see one of its models
twice: a deployment behind it is also published on its own, or another pool on the gateway offers
a model under the same name.

## Suggestions and API reference

The Pools page's **Suggested pools** proposes a pool for
each managed gateway, vendor, and API, with the models deployed on two or more endpoints the
gateway can use that no pool there uses yet. **Create pool** opens the editor with those models
and their deployments chosen. On the Models page, each deployment names the pools that use it,
and an endpoint's **Used by pools** card lists them with the deployments each one uses there. The
Dashboard's **Pools** panel lists the published pools with a deployment the gateway can't use or
another problem, and the pools whose last apply failed.

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| GET | `/gateways/{gatewayId}/pool-candidates` | The deployments a pool on that gateway could use, grouped by model, and why others can't be used |
| GET | `/model-pool-suggestions` | Pools worth creating: on each managed gateway, one per vendor and API, with the models deployed on two or more endpoints the gateway can use that no pool there serves yet, and the gateway's existing pools for that vendor |
| GET | `/model-pools`, `/model-pool-summaries` | Pools, optionally on one `gateway`. A summary counts active members by capacity type and readiness |
| GET | `/model-endpoints/{id}/pools` | The pools with members on that endpoint, the deployments each one uses there, and a warning on any deployment portal users would also see published on its own |
| POST | `/model-pools` | Creates a pool, `201` |
| GET, PATCH, DELETE | `/model-pools/{id}` | Reads, changes, or removes a pool. `governedAccess` turns on governed access, and can't be cleared once set. Removal is refused while the pool owns API Management resources |
| GET | `/model-pools/{id}/detail` | The pool with each member judged against today's inventory, environments, and gateway |
| GET | `/model-pools/{id}/health?hours=` | How each model's calls ended and how each member answered over the last 1 to 168 hours, 24 by default, from the gateway's logs |
| POST | `/model-pools/{id}/plan` | A plan to review |
| POST | `/model-pools/{id}/apply?plan={planId}` | Runs that plan, `202` with the run |
| POST | `/model-pools/{id}/unpublish-plan` | The unpublish plan to review. Removes nothing |
| POST | `/model-pools/{id}/unpublish?plan={planId}` | Runs that plan, `202` with the run |
| GET | `/model-pools/{id}/runs`, `/model-pools/{id}/runs/{runId}` | The pool's runs |
| POST | `/model-pools/{id}/recover` | Reconciles an interrupted run, as for a publication |
| GET | `/model-pools/{id}/lock`, `/model-pool-plans/{planId}` | The pool's write lock, and a saved plan |

Entitlement, access request, and portal routes take a `poolModel` resource, whose `scopeId` is the
pool. Pools add no routes of their own for access.

## Deferred work and verification

ADR 0024's last phase, not built yet, adds a model router pool type on API Management's unified
model API, once that's generally available.

Pools have API Management test-double coverage. Live evidence for individual journeys is recorded
in the [E2E roadmap](e2e/roadmap.md); implementation and unit checks do not prove every behavior
ADR 0024 lists under *Verify on a real gateway*.
