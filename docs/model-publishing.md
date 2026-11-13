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
| Anthropic Messages | Claude models on Foundry | `/anthropic/v1/messages` and `/anthropic/v1/messages/count_tokens`, served from the resource's `services.ai.azure.com` host |

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
