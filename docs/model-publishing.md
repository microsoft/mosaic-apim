# Publish and unpublish models

[Documentation](README.md) | [MOSAIC](../README.md)

- [Publishing models](#publishing-models)
- [Catalog summaries](#catalog-summaries)
- [Unpublishing](#unpublishing)

## Publishing models

Publishing takes a deployment MOSAIC observed on a registered model endpoint, or one an
administrator declared on an endpoint reached with an API key, and exposes it through a registered
gateway. It is the first thing MOSAIC writes to API Management, and it completes the loop
[ADR 0001](adr/0001-apim-runtime-boundary.md) described and deliberately stopped one step
short of: desired state, observed state, deterministic plan, explicit apply, audited result.

A `Publication` in `desired-state` records the intent. Saving it changes nothing in Azure. Planning
it produces a persisted, deterministic `PublishPlan`; applying runs against that specific plan and
rejects one whose digest no longer matches, so an administrator cannot approve one set of changes
and have another applied. A `PublishRun` records the outcome of every step.

The admin console applies a plan only from its review. Its default view compares the reviewed target
with MOSAIC's recorded applied configuration, hiding unchanged intent. **Your changes** uses the
current editing interaction; **Previously saved changes included** lists the other pending changes.
Without reliable edit context, the review simply says **Pending changes**. Returning a field to its
applied value cancels that pending change, so it is not listed as another gateway change.
Compiled or inherited grant values that cannot be attributed to the current edit also remain under
**Pending changes**, rather than being mislabeled as earlier saves.

**Apply all pending changes** still applies the complete selected publication or pool, never just
the highlighted edit. Operational impact stays visible: governed model and pool batches can
temporarily deny unchanged callers and suspend keys, generated policies replace rather than merge
manual edits, and removing pool members can delete their APIM backends. Intended-equal configuration
can still be written again. **Technical execution**, collapsed for new plans, retains the complete
target, ordered resource steps and policy facets; older plans without a comparison show it expanded.
Counter expressions are represented only by fingerprints, never their contents.

The applied baseline is a timestamped MOSAIC record, not a fresh live-Azure verification. Older
records can compare recorded access but lack historical routing settings; failed or interrupted runs
also limit what is known. Those previous values are explicitly unavailable, not silently unchanged.
The ephemeral editing context expires and does not survive a reload; the server's complete pending
comparison remains available. A grant's effective per-person limits say whether they come from a
grant override or cost-center defaults; shared quotas and publication safeguards are additional.

In the Published models detail, **Review changes** makes a fresh plan and opens that review; it is
also how a failed or rolled-back publication is retried.
If MOSAIC refuses the reviewed plan, for example because the publication changed after it was
planned, the review says why and shows a fresh plan in its place.

For a custom [cost center](cost-centers.md), access modes belong to the exact published offering:
Automatic creates one stable group JWT grant, Request-only approvals create individual grants,
and Not available blocks new grants/requests. Both individual and group token grants require the
center's verified Entra group claim. Automatic requires **caller-side** token support; a backend
using an API key does not prevent it. Explicit individual keys remain available where publication
and center permit them. General is exempt from the mandatory group, and Government is excluded
from this new workflow.

Resource-mode, grant, quota, and key-policy changes enter the reviewed plan/apply boundary.
Membership edits instead change the real Entra group through the optionally consented API identity;
they do not generate per-user automatic grants or APIM management writes. A successful member
removal is not immediate JWT invalidation or key revocation. See
[membership and credential follow-up](cost-centers.md#removing-a-member).

Applying creates, in dependency order:

| Order | Resource | Purpose |
| --- | --- | --- |
| 1 | Named value | Only for an endpoint reached with an API key: a Key Vault reference API Management reads the key through with its own identity |
| 2 | Backend | The model endpoint origin, with query and fragment stripped |
| 3 | `mosaic-*` policy fragment | Backend authentication (the gateway's managed identity, or the endpoint's key from the named value), routing to the backend, and, where the gateway's tier supports them, token limit and token metric |
| 4 | API | The route, created with no `serviceUrl` so removing the fragment fails closed |
| 5 | Operations | A curated, versioned set per API shape |
| 6 | API policy | A thin `<include-fragment>` of the MOSAIC fragment |
| 7 | Product | Carries the API |
| 8 | Product/API link | |
| 9 | Subscription | Only when the publication requires one |

Each resource is created after the resources it names. API Management accepts a policy fragment and
only then checks the backend its `set-backend-service` names, failing the write if that backend does
not exist yet, so the backend comes first. A fragment that sets a key from a named value names that
too, so the named value comes before both. Likewise, the API policy includes the fragment; the
operations and the API policy belong to the API; the product link joins the product and the API;
and the subscription is scoped to the product. A plan saved by an earlier MOSAIC release that put
the fragment first is refused at apply; plan the publication again.

A publication from an endpoint reached with an API key owns its named value, `<backend>-key`. MOSAIC
gives API Management the secret's versionless identifier and never the key, and never calls
`listValue`. After writing it, MOSAIC reads it back and removes it again if API Management reports
that it couldn't read the secret, saying what to grant. Its fragment first removes every credential
a caller could send (`api-key`, `x-api-key`, `Authorization`, `Ocp-Apim-Subscription-Key`, and the
`api-key` and `subscription-key` query parameters), then sets the backend's key header from the
named value: `api-key` for the Azure OpenAI and Foundry Models APIs, `x-api-key` for the Anthropic
Messages API. A caller can neither supply nor override the key, and none of their credentials reach
the backend. The plan's digest covers the secret, so pointing the endpoint at another secret needs a
new review.

Operation sets are shipped and versioned by MOSAIC rather than fetched from the provider, so a plan
is deterministic and does not couple an APIM write to a third-party document being reachable. Each
publication records the API shape it was created with, chosen from the deployment's model format
and capability ([ADR 0012](adr/0012-format-aware-model-publishing.md)):

| Shape | Deployments | Operations |
| --- | --- | --- |
| Azure OpenAI | Azure OpenAI chat, responses, completion, embeddings, image and transcription models | Chat completions, completions, embeddings, image generations, audio transcriptions and translations, and responses |
| Foundry Models | Foundry (AI Services) chat and embeddings models | The Foundry Models inference routes under `/models` |
| Anthropic Messages | Claude models on Foundry; the bounded standalone Bedrock tuple below | `/anthropic/v1/messages` and `/anthropic/v1/messages/count_tokens`; governed access exposes Messages only |

Deployments no shape can serve, such as realtime, video and text-to-speech models, are listed as not
publishable with a reason, and creating a publication for one is refused. A publication also records
the shape version that produced it. OpenAI-compatible endpoints have no curated shape, and are
refused rather than guessed at.

API Management meters the Anthropic Messages API with `llm-token-limit` and `llm-emit-token-metric`
only on v2 tiers. On a classic tier such as Developer, the publish wizard explains this, and a Claude
publication applies no token limits or token metrics. Governed grants on it can use call limits
instead.

Every step records whether it created the resource or found one already there. A step succeeds only
once Azure has finished its write: API Management finishes a policy fragment or an API write
asynchronously, on an update as well as a create, so MOSAIC waits for any write Azure answers with
`Azure-AsyncOperation` or `Location`, whatever its status code. A failed step says why: when Azure
explains a failed operation, or refuses a request outright, the step's error carries Azure's error
code, message and most specific detail, such as a validation error naming the policy element, line
and column. It is bounded in length and never includes policy markup. An update API Management
rejects after accepting it leaves the previous content in place and fails its step, so it is never
reported as applied: a re-applied publication rolls back, and a failed governed apply denies access,
then restores only the last safe access. If a step fails, MOSAIC reverses the completed steps and
deletes **only** resources that run created — ownership is recorded at the moment of the write,
never inferred from a name, so a product that merely matches a MOSAIC name is never destroyed. A
resource MOSAIC replaced rather than created is not reverted, because the previous content was never
stored; those are named in the run instead. If the rollback itself fails, the run reports
`rollbackFailed` and lists exactly what was left behind.

Unpublishing runs the same machinery over the tracked resources in reverse, so a fragment is removed
before the backend it routes to. A publication that still owns API Management resources cannot be
deleted, and a gateway with published models cannot be removed, so intent is never dropped while
the resources it created keep running.

## Standalone Bedrock

The first standalone AWS route is deliberately bounded: explicitly declare
`anthropic.claude-sonnet-5` at `https://bedrock-mantle.us-east-1.api.aws` with the Anthropic
Messages shape and an operator-managed API key. It requires a v2 APIM gateway and governed
access. It does not add discovery, SigV4, inference-profile routing, another grant system or a
generic-provider adapter. The existing Claude pool path is unchanged.

**Guided release** separates four actions in the full workspace:

1. **Register** saves endpoint declarations and the Key Vault reference, not a model call.
2. **Save publication draft**, then explicitly **Review publication**, saves publication intent
   and reads APIM evidence. Neither writes gateway resources or creates usable access.
3. **Apply reviewed plan** uses the existing ordered, locked writer and recovery. Drift refuses
   the plan and requires explicit re-review; a failure never silently reviews or applies again.
4. **Grant and connect** uses the existing entitlements, approval and reviewed access apply.
   Publication alone creates no caller grant. Connection details use the gateway URL and
   MOSAIC caller authentication, never an AWS URL, key, signature or workspace.

The owned route accepts only `POST /anthropic/v1/messages`, pins the exact model and
`anthropic-version: 2023-06-01`, removes caller credentials/signing and provider override headers,
and discards unmatched query parameters before setting its Key Vault-backed `x-api-key`.
CountTokens, Converse, InvokeModel, discovery, beta headers, server-executed tools and other
models/regions are not offered. Omit `service_tier` for this contract; AWS documents omitted
or `default` as Standard, but this narrow route deliberately accepts omission only.

Native JSON and SSE are not translated. Client tools and results, image blocks, adaptive or
disabled thinking (including signatures), and explicit prompt-cache controls stay in the native
body and order. The application executes tools, including the model's documented GA client
toolsets; MOSAIC does not execute them. The request guard requires a positive integer `max_tokens`
no greater than the model's documented 128K output bound and a nonempty `messages` array.
It supplies no generation default and does not disable adaptive thinking when omitted.
AWS remains authoritative for full native schema, context/tokenization, image size/dimensions,
cache-checkpoint eligibility and semantic validation; provider errors/refusals remain native.
The documented context window is 1M tokens; image inputs have AWS's documented size and
dimension constraints, not a new MOSAIC limit.

Existing aggregate call/token limits, cost-center counters, quotas, budget denial and attribution
still apply. APIM's streamed/image token accounting can be estimated; thinking consumes output
tokens. Those safeguards are not a per-request generation cap, exact token counter or hard
spending ceiling. Administrator and caller cost projections mark this standalone lineage
incomplete because aggregate logs lack cache read/write counts; unknown cost is not zero.
An eligible pool using the same deployment retains its own price. A budget's known priced
subtotal is a lower bound: it can cross a threshold or block at/above the cap, but cannot lift a
block, re-arm thresholds on a raised amount or produce a comprehensive forecast.

The selected API owns exactly one backend `forward-request` with
`buffer-response="false"` and `follow-redirects="false"`, rather than inheriting the default
buffered forward. Review and Apply strictly re-read service, API, product and operation policies,
including complete referenced collections; 404 absence is allowed for not-yet-created resources,
but malformed successful reads are inconclusive and refuse before writes. The digest covers this
evidence. Only recognized compatible parent policies are accepted; custom buffering, retries,
fragments, response inspection and operation overrides require separate reconciliation.
Other providers, legacy APIs and pools retain their existing policy inheritance. Redirects are
returned as a sanitized native-shaped 502, with no `Location` or second credentialed forward.

**Proof boundary.** Offline tests cover compiled policy/evidence, existing fake-backed reviewed
apply and caller/cost contracts. A loopback HTTP fixture demonstrates ordinary native JSON and
first SSE data received before the server may complete; it is not an APIM expression emulator
or live APIM streaming proof. The screenshot script renders only the fictional demo estate.
The feature is deployable for usable access, not restricted to demo estates. BR3 verification
requires separate approval of a bounded non-production AWS account, region/model access,
least-privilege key custody and rotation, an APIM v2 gateway and Key Vault permissions, a
dedicated bounded MOSAIC grant, permitted inference spend, rollback and cleanup. BR3 must prove
real ordinary/streamed calls, first-event timing, credential isolation/no redirects, denial and
limits, and measured caller/grant/cost-center attribution. No live proof is implied by Apply.

For a connection whose grant is applied, this caller-side streaming example uses only MOSAIC
connection details. Use the complete gateway Messages URL in `MOSAIC_MESSAGES_URL`; remove
`"stream": true` and `-N` for ordinary JSON. The example's 1024 is an explicit caller choice,
not a product default:

```sh
curl -N "$MOSAIC_MESSAGES_URL" \
  -H "Ocp-Apim-Subscription-Key: $MOSAIC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"anthropic.claude-sonnet-5","max_tokens":1024,"stream":true,"messages":[{"role":"user","content":"Hello"}]}'
```

Sources: [AWS native Messages](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html),
[Sonnet 5 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-5.html),
[native request/response](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html),
[AWS API keys](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html),
[APIM forward-request](https://learn.microsoft.com/azure/api-management/forward-request-policy)
and [APIM LLM token limits](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy).

## Catalog summaries

In **Models > Published models** or **Imported model APIs**, select the exact offering and choose
**Edit catalog summary**. Write optional plain text, choose **Review summary** to compare the
saved and draft copy with a portal preview, then explicitly **Save summary**. **Back to edit**
retains the draft; leaving or canceling asks before discarding unsaved changes. A failed save keeps
the draft and reports the error.

Summaries may contain at most 2,000 Unicode code points after trimming surrounding whitespace.
An empty or whitespace-only summary removes the copy; missing legacy summaries stay empty rather
than receiving a generated description. Existing longer summaries remain readable, but an
intentional rewrite must fit the limit. Line breaks are preserved and markup is shown as plain text.

Saving changes MOSAIC catalog metadata only: it does not change visibility, publish or apply an
APIM plan, or grant access. A model that is not published remains unavailable in the portal.
Older published records without a linked model API expose an explicit **Link catalog entry**
recovery action rather than linking or publishing automatically. Reload the portal's **Catalog**
to fetch the saved copy; open portal tabs do not receive a cross-app push update.

The portal shows the summary after choosing an exact offering, not as shared copy for a visual
model group. Models served by a pool use the same flow from
**Pools > Models and deployments**; see [public pool-model summaries](model-pools.md#portal-and-console).
MCP summary editing is not part of this workflow.

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| PATCH | `/model-apis/{id}/catalog` | Updates explicitly supplied model catalog fields; omitting summary preserves it, while null/empty/whitespace clears it. Visibility-only updates do not revalidate existing summary text. |
| PATCH | `/model-pools/{poolId}/models/{modelId}/catalog` | Updates only the exact pool model's public summary. Both IDs are required; routing, reviewed plans, access state, and private pool descriptions are unchanged. |

## Unpublishing

Unpublishing is reviewed like publishing, because it cuts people off. **Unpublish** in the Published
models table asks MOSAIC for an unpublish plan, which deletes nothing, and opens it in a review:

- **Who loses access**: every enabled grant in the access the gateway applied last, with what stops
  working for each. Subscription keys stop working, and the gateway refuses Entra tokens. A model
  without governed access has no such list, so the review says that anyone calling it with a key
  for its product loses access.
- **What MOSAIC deletes**: every resource MOSAIC created, in the order it deletes them. Anything
  MOSAIC found already in API Management stays, and the review names it. Deleting the product
  also deletes every subscription to it, including ones API Management added. A publication from
  an endpoint reached with an API key deletes its named value last, after the fragment that names
  it; the key itself stays in Key Vault.

Only the review's **Unpublish model** runs the plan. MOSAIC runs exactly the reviewed steps and
refuses a plan that no longer matches the publication, for example because access was applied or a
resource recorded since the review. The review then says why and shows a fresh plan in its place.
With governed access, MOSAIC denies every call and suspends each grant's subscription before it
deletes anything, and deletes the API first.

| Method | Route under `/api/v1` | Result |
| --- | --- | --- |
| POST | `/publications/{id}/unpublish-plan` | The unpublish plan to review. Removes nothing and changes nothing on the publication |
| POST | `/publications/{id}/unpublish?plan={planId}` | Runs that plan, `202` with the run. Without a plan, `409` with reason `planRequired`; with a plan the publication has outgrown, `409` with reason `stalePlan` |

Apply runs only publish plans, and unpublish only unpublish plans. Grants stay in MOSAIC, and a
governed grant shows its runtime access as revoked. The Published models table shows the publication
as **Unpublished**, with when, and **Re-plan** publishes it again; applying that plan restores its
grants' access. While a model is
unpublished, the portal leaves it out of the catalog, refuses a new access request for it with a
`409` and reason `notPublished`, marks grants and requests for it as no longer available, and
gives no connection details for it. A model API imported from a gateway MOSAIC didn't publish is
unaffected.
