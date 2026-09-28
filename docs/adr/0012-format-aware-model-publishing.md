# ADR 0012: Choose a published model's API shape from its format and capability

**Status:** Accepted

## Context

ADR 0010 published every deployment of a provider with that provider's curated operation set. An
Azure OpenAI account got the `/openai/deployments/{deployment}/...` set, and an AI Services
(Foundry) account got `/models/chat/completions`, `/models/embeddings` and `/models/info`. The
publishable-models list offered every discovered deployment. Endpoint inventory already recorded
each deployment's `model_format` (OpenAI, xAI, Meta, DeepSeek, Anthropic, ...) and its ARM
capability flags, but publishing never read them.

That assumption fails in three ways.

- **Anthropic models don't speak the Foundry Models API.** Foundry serves Claude only through the
  Anthropic Messages API at `https://<resource>.services.ai.azure.com/anthropic/v1/messages` (with
  `/anthropic/v1/messages/count_tokens` for token counting). Requests need an `anthropic-version`
  header, and the body's `model` names the deployment. A Microsoft Entra caller uses the
  `https://ai.azure.com/.default` scope; the alternative is the resource key in `x-api-key`
  ([Deploy and use Claude models in Microsoft Foundry](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-claude)).
  ARM reports the account's `cognitiveservices.azure.com` host as its endpoint, and that host
  doesn't serve Anthropic models at all. A Claude publication built the old way could never
  succeed.
- **Token governance depends on the gateway tier.** `llm-token-limit` meters OpenAI Chat
  Completions and Responses on every tier that has it. It meters the Anthropic Messages API only
  on the v2 tiers
  ([llm-token-limit policy](https://learn.microsoft.com/azure/api-management/llm-token-limit-policy)),
  and `llm-emit-token-metric` has the same limit. A classic-tier gateway, such as the Developer
  tier used for development, can't token-limit a Claude publication.
- **Not every deployment is a chat model.** Realtime, video (Sora), text-to-speech and rerank
  deployments have no curated operation that can serve them. On a Foundry account neither do image
  generation, transcription, legacy completions or Responses-only models, because the Foundry
  Models set covers only chat and embeddings. Offering those deployments published an API that
  could only fail.

## Decision

**A publication records an API shape, chosen when it's created.** `Publication.api_shape` is one of:

| Shape | Operations (relative to the API path) | Backend origin |
| --- | --- | --- |
| `azureOpenAi` | ADR 0010's `/openai/deployments/{deployment}/...` set and `/openai/responses` | The registered endpoint's origin |
| `foundryModels` | `/models/chat/completions`, `/models/embeddings`, `/models/info` | The registered endpoint's origin |
| `anthropicMessages` | `POST /anthropic/v1/messages` (`messages`), `POST /anthropic/v1/messages/count_tokens` (`count-tokens`) | `https://<subdomain>.services.ai.azure.com` |

A publication stored before this change has no shape, and reads as its provider's default:
`azureOpenAi` for Azure OpenAI and `foundryModels` for Foundry. Its plan, policy and operations
don't change. The shape can't be edited: changing it would republish a different API contract
under the same name, so the administrator deletes the publication and creates a new one instead.
The shape is therefore not a separate input to the plan digest. Everything it affects (operations,
backend URL, policy SHA) is already in the digest.

**A pure assessment decides the shape, or explains why there isn't one.**
`assess_deployment` reads the provider, the deployment's model name, `model_format` and ARM
capability flags, the registered endpoint and the gateway's SKU. The publishable-models list,
publication create and plan all call it, so they agree.

- A model whose format is `Anthropic`, or whose name starts with `claude`, gets
  `anthropicMessages`. It must be on a Foundry account whose endpoint yields a
  `services.ai.azure.com` origin, and its gateway's tier must be known. Otherwise it's not
  publishable, with a reason.
- Any other deployment is classified as one of chat, responses, completion, embeddings, image,
  transcription, speech, realtime, video, rerank or unknown. The classifier uses ARM's flags plus a
  few name patterns, because ARM's flags are incomplete for partner models. It checks the most
  specific families first, since a realtime or transcription model also carries chat or audio
  flags. The provider's default shape is used when it covers that capability.
- Azure OpenAI covers chat, responses, completion, embeddings, image and transcription. Foundry
  Models covers chat and embeddings. Both cover unknown, so a deployment whose flags say nothing
  publishes exactly as it did before this change.
- Anything else is listed as **not publishable, with a reason**, rather than hidden or offered.
  Create refuses it with the same reason.

**The Anthropic shape supplies what the Messages API needs.** The published API's policy (legacy
and governed alike) removes any caller `x-api-key` header, so caller credentials never reach the
provider. It sets `anthropic-version: 2023-06-01` only when the request doesn't send one, so a
client can choose a newer version. It authenticates to the backend with API Management's managed
identity for the `https://ai.azure.com` resource, instead of `https://cognitiveservices.azure.com`.
The gateway identity needs the same *Cognitive Services User* role that the runtime-access check
already requires for Foundry accounts. The origin comes from the account's custom subdomain: a
single-label subdomain under `cognitiveservices.azure.com`, `openai.azure.com` or
`services.ai.azure.com` maps to `<subdomain>.services.ai.azure.com`. Any other host, such as a
regional endpoint, is refused rather than guessed.

**Token governance follows the gateway's tier.** Preflight already records the APIM SKU as
`GatewayCapabilities.sku_name`. `gateway_tier` groups it into v2 (Basic v2, Standard v2, Premium
v2), classic (Developer, Basic, Standard, Premium, Isolated), Consumption, or unknown. An unread SKU
is unknown, never assumed.

- `anthropicMessages` publications can be token-metered only on v2. The OpenAI-style shapes keep
  their existing behavior on every tier.
- Where metering is unavailable, `PublicationCreate.enforcement` must be `null`. The publication
  renders no `llm-token-limit` or `llm-emit-token-metric`, and its plan carries a warning that says
  why.
- Where metering is available, enforcement stays required, as before.
- An Anthropic deployment behind a gateway of unknown tier isn't publishable until the gateway's
  access check runs. That avoids a wrong guess in either direction.
- MOSAIC has no publication-level request-limit facet. The wizard therefore says token limits are
  unavailable and explains why. It applies no limits rather than silently substituting a different
  kind of limit.

**Governed access permits only `messages` on an Anthropic publication.** ADR 0011's operation
allowlist for this shape is `messages`. `count-tokens` and every other route are denied, because
token counting is still a call to the provider and can't be metered. On a classic-tier gateway,
grants can still carry call limits (`rate-limit-by-key`, `quota-by-key`), but not token limits. A
grant that sets token limits there is excluded from runtime access, with a plan warning telling the
administrator to replace its token limits with call limits. The governed-policy renderer refuses
such a grant too, so it can never be applied unmetered.

**Connection information describes the shape.** `ModelConnection` carries `apiShape` and the
shape's operations, and `publicationLimits` is `null` when the publication applies no token limits.
The admin console and the portal's **Connection details** tell Anthropic callers to:

- use the Messages API, with the request's `model` set to the deployment name;
- use `<gateway>/<api path>/anthropic` as the Anthropic SDK base URL;
- send the key in `Ocp-Apim-Subscription-Key`, because the gateway strips `x-api-key`.

The portal's code samples call `/anthropic/v1/messages` with no `api-version`. When
`publicationLimits` is `null`, both apps say that token limits are unavailable on the gateway's
tier.

## Alternatives considered

- **Keep choosing the operation set by provider, and document that Claude is unsupported.** This is
  simpler, but it keeps offering deployments that can't work, and Claude is a common reason to put
  a Foundry account behind a gateway.
- **Fetch each deployment's OpenAPI document.** ADR 0010 rejected this, because plans must be
  deterministic and reviewable. Partner models don't publish one consistently in any case.
- **Pass the caller's `x-api-key` through to Foundry.** This would put a provider key in every
  client and bypass the gateway's identity-based backend authentication.
- **Substitute request limits for token limits on classic tiers automatically.** This would change
  what an administrator asked for without asking. Publication-level request limits would need their
  own facet and review story. Per-grant call limits already exist for governed access.
- **Treat an unknown tier as classic.** On a v2 gateway, that would silently drop token limits the
  administrator could have had. Treating it as v2 would render a policy the gateway might not run.
  Asking for a gateway access check is cheap.

## Consequences

- Claude deployments on Foundry publish and can be called through the gateway with the Anthropic
  Messages API. On classic-tier gateways they run without token limits or token metrics, and the
  admin console says so before anything is applied.
- Deployments MOSAIC can't publish are visible, with a reason, instead of producing broken APIs.
  New shapes (WebSocket realtime, video jobs, Foundry images and audio) can be added one at a time
  as `ApiShape` values.
- A Claude publication created before this change has the `foundryModels` shape and can't work.
  It must be unpublished, deleted and created again. An existing realtime or video publication
  keeps planning as before. Only new creates are refused.
- The classifier's name patterns are a heuristic, and need updating when a provider adds a new
  kind of model. A deployment the classifier can't place stays publishable with its provider's
  default shape, as it was before.
- Consumption-tier gateways don't offer `rate-limit-by-key` or `quota-by-key`. That caveat
  already applied to governed access.
- The Anthropic routes, like the Foundry Models routes, name the deployment in the request body
  rather than the path. Governed access rejects a request whose `model` isn't the publication's
  deployment. A publication that hasn't opted into governed access doesn't check the body, as with
  the existing Foundry Models shape.
- Other Foundry partner models (for example Grok, Llama and DeepSeek) keep the `foundryModels`
  shape. Foundry also documents the OpenAI-compatible `/openai/v1/` routes for all Foundry Models,
  with the model named in the body. Moving those models to that route would be a new `ApiShape`,
  and is left to separate work.
