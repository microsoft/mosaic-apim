# ADR 0014: Environments for gateways, model endpoints, and MCP servers

**Status:** Accepted

## Context

MOSAIC administers many API Management gateways across an enterprise. Each gateway, model
endpoint, and MCP server had a free-text `environmentLabel` of up to 60 characters. Nothing read
it, the console could set it only at registration, and it could never be changed. Several
problems followed:

- A development model endpoint could be published through a production gateway. Nothing checked
  the pairing when a publication was created, planned, or applied.
- End users couldn't tell a development catalog entry from a production one. So they couldn't
  ask for development access to build against, then ask for production access when their
  application went live.
- An administrator couldn't see, across gateways, which environment anything belonged to.

A label that nothing enforces is only a comment. Environment is useful only if MOSAIC can hold a
rule to it: a production gateway never fronts a development endpoint.

## Decision

**Administrators define environments centrally.**
- Each tenant has one `EnvironmentCatalog` document, in `desired-state` under the tenant's
  partition. MOSAIC seeds it with six built-in environments: Development, Test, QC, Staging,
  Production, and Sandbox. Administrators can add their own.
- Each environment has:
  - an immutable key;
  - a display name, a description, and a Fluent badge color;
  - a production-class flag;
  - aliases, used only for suggestions;
  - a list of exceptions: other environments whose endpoints its gateways may also front.
- A production-class environment may list only production-class exceptions.
- An environment can't be deleted while anything uses it, and built-in environments can't be
  deleted at all.

**Classification lives on the things that exist in Azure.**
- Gateways, model endpoints, and registered MCP servers each carry one `environment` key, or none
  (Unclassified).
- Imported model APIs, imported MCP servers, and products take their gateway's environment.
  Model-deployment grants take their endpoint's environment.

Derived records don't get their own label, for three reasons:
- A model API is served by exactly one gateway and can't be in a different environment from it.
- A second label would drift from the first.
- A re-import would have to decide which label wins.

**One function judges every pairing.** `permits()` returns a verdict of `allowed`, `warning`, or
`blocked`, with plain-language wording that names both environments and says how to fix the
pairing:
- A gateway may front endpoints from its own environment, and from environments it lists as
  exceptions.
- A production-class gateway never fronts a non-production or unclassified endpoint.
- An unclassified gateway never fronts a production-class endpoint.
- Other pairings that involve an unclassified resource are allowed, with a warning. When the
  tenant turns on **Require classification**, they are blocked.
- An environment key the catalog doesn't know is blocked. Keys are validated on write, so only
  data written outside MOSAIC can produce one.

The console looks verdicts up in a compatibility matrix the API derives from the catalog. It
never reimplements the rules. The same function judges MCP publications and gateway-hosted MCP
servers.

**Suggest, never assume.** Every existing resource starts Unclassified. MOSAIC reads an Azure
`environment` or `env` resource tag, but only from ARM responses it already fetches:
- gateway preflight;
- model endpoint preflight;
- the subscription scan.

That tag and the legacy label each produce a one-click suggestion, matched against keys, display
names, and aliases. An administrator confirms every suggestion. Reading a tag never classifies a
resource. A failed ARM read keeps the tag last seen; only a successful read without the tag
clears it.

**No change may leave an applied publication blocked.** An *applied* publication is one where
`may_own_gateway_state()` is true. Several kinds of change are refused if they would leave one
blocked:
- re-classifying a resource;
- editing an environment or its exceptions;
- deleting an environment;
- turning on Require classification.

The refusal lists the publications involved. Draft publications may be left behind; they fail at
plan time with the verdict. Unpublish, rollback, and recovery never check environments, so a
change of rules can never trap a publication in place.

Three further rules keep the invariant true under concurrency:
- **Observation never overwrites classification.** Preflight and sync used to save whole documents
  with unconditional upserts. A preflight that began before a re-classification could revert it,
  and could even resurrect a deleted gateway. Observation writes now re-read the document, keep
  every administrator-authored field, apply only what was observed, and replace against the etag.
  They never recreate a deleted document.
- **Locks are taken in one order:**
  1. the tenant `environments` scope;
  2. gateway scopes;
  3. endpoint scopes;
  4. publication locks.

  Each group is sorted by ID. Anything that writes an environment key or changes the rules holds
  the tenant scope, and so does publication creation. A new `endpoint:{id}` scope stops a
  publication from appearing while its endpoint is being re-classified.

  A re-classification or rule change also locks every publication it could newly block, drafts
  included, and judges the applied ones only while holding those locks. An apply holds its
  publication's lock for its whole run, so the change fails fast with "A publication affected by
  this change is being applied" instead of racing it. Apply re-checks the verdict under the same
  lock.

  Gateway scopes and publication locks are the existing fail-fast locks. The two new scopes are
  *leases* instead: a document with an owner and an expiry, taken with a create and taken over
  only once expired, through an etag-conditional replace. A crashed process therefore can't
  strand a tenant's environment changes, and the leases never need an administrator to clear
  them. The critical sections they guard are short and never span a call to Azure, so a busy
  lease is retried briefly before the caller sees "Another environment change is in progress."
- **Plans pin the verdict.** A publication's plan digest includes a compatibility fingerprint:
  - both keys and their production-class flags;
  - the exception that permits the pairing, if any;
  - Require classification;
  - the verdict.

  Apply rejects a plan whose fingerprint no longer matches. Display names, colors, and unrelated
  environments are left out, so cosmetic edits don't invalidate plans.

**Environments change through one route.** `POST /api/v1/environment-assignments` takes a batch
of resources and their new environments. It validates the batch's *final* state, so a gateway and
the endpoints behind it can move to Production together; validating one resource at a time would
deadlock.
- The route works out which gateways, endpoints, and publications the batch touches, takes their
  scopes and locks, and reads them again under the locks. If a publication appeared in between,
  it starts again with the wider set, up to three times, before answering "Another environment
  change is in progress."
- Resources linked by applied publications are written in one transactional batch, with an etag
  on every document and one audit event recording each resource's before and after.
- A linked group too large for one Cosmos transactional batch is refused rather than split.
- Registration accepts an environment. Update routes don't, so every re-classification passes
  through the same guard.

**Existing grants follow their resource.** When a resource with enabled grants moves into or out
of a production-class environment, the API names the affected grants and principals by display
name. It requires the administrator to acknowledge them, and records them in the audit event.
Other re-classifications are simply audited.

**Access requests remember their environment.**
- A request records the resource's environment and a small snapshot of its name and gateway.
- If the environment has changed since the request, approval requires the administrator to
  confirm the new one.
- Portal requests accept only model APIs and MCP servers the caller's catalog shows. Anything
  else returns the same not-found error. This closes a gap where a guessed ID for a private
  resource could be requested.

**Findings are advisory.** MOSAIC reports blocked pairings that exist in API Management but not in
its own publications:
- a backend or API service URL on a gateway that points at a registered model endpoint in an
  incompatible environment;
- a gateway MCP server whose URL matches a registered MCP endpoint in one.

Host matching treats the Azure AI account domains (`openai.azure.com`,
`cognitiveservices.azure.com`, `services.ai.azure.com`) as the same account, at lower confidence.
Findings never block anything, and they say what they can't see.

## Consequences

- **Upgrading is safe.** Everything starts Unclassified with Require classification off. Every
  existing pairing is therefore a warning, none is blocked, and nothing already applied becomes
  invalid.
- Plans reviewed before the upgrade must be planned again once, because the digest now includes
  the environment fingerprint.
- The free-text `environmentLabel` remains as display-only legacy data and a source of
  suggestions. The console no longer writes it.
- The bootstrap gateway registers as Unclassified, like everything else. An `environment` tag on
  the azd-deployed APIM becomes a suggestion.
- Findings cover registered resources only. A backend referenced only from policy, such as a
  `set-backend-service` base URL or a named value, isn't inspected.
- Environment chains are deferred. MOSAIC doesn't relate the same model across environments or
  clouds, and the portal doesn't search by model. Users request each environment separately.
- ADR 0001 is unaffected. Classification is MOSAIC desired state, and MOSAIC doesn't write tags
  back to Azure.

## Amendment 2026-09-30: MCP publications follow the same rules

ADR 0017 added a flow that publishes registered MCP servers through a gateway. When this ADR was
written, that flow didn't exist, so MCP pairings were only reported as findings. MCP publications
now follow exactly the rules and process that model publications do. MOSAIC is still a proof of
concept, so existing MCP publications aren't grandfathered.

- **Publishing.** Create, plan, and apply judge the gateway against the registered MCP server with
  `permits()`. A blocked pairing is refused with the same `environmentBlocked` conflict and verdict
  that models return. A warning is added to the plan's warnings. The MCP plan digest includes the
  compatibility fingerprint, so apply rejects a plan whose environments changed. Unpublish and
  recovery still never check environments.
- **Locks.** MCP publication creation holds the tenant `environments` lease, its gateway scope,
  and its publication lock, in the order above. MCP servers have no `endpoint:{id}` scope. The
  tenant lease already serializes creation against a re-classification, because both hold it.
  Apply holds the publication lock for its whole run and checks the verdict again under it.
- **No change may leave an applied MCP publication blocked.** Re-classifying a gateway or an MCP
  server, editing or deleting an environment, and turning on Require classification all judge
  applied MCP publications as well. The refusal lists them alongside model publications, with the
  MCP server's name in place of an endpoint and deployment. A re-classification locks the MCP
  publications it could newly block, drafts included. Its suggestions can move the MCP server with
  its gateway, and the two are written in one batch.
- **Findings.** An applied MCP publication whose pairing is blocked is reported as a
  `blockedPublication` finding that targets the MCP server. The APIs an MCP publication creates,
  including its metadata API, count as MOSAIC-owned. A server MOSAIC published therefore isn't
  also reported as a gateway MCP server that crosses environments.
