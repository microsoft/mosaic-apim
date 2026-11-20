# Register and deploy model endpoints

[Documentation](README.md) | [MOSAIC](../README.md)

- [Model endpoints](#model-endpoints)
- [Deploy a new Azure model](#deploy-a-new-azure-model)
- [Endpoint and gateway access](#endpoint-and-gateway-access)
- [Endpoints reached with an API key](#endpoints-reached-with-an-api-key)
- [AWS Bedrock endpoints](#aws-bedrock-endpoints)
- [Native Google Vertex AI and custom chat](#native-google-vertex-ai-and-custom-chat)

## Model endpoints

A model endpoint is an Azure OpenAI or Azure AI Foundry resource that a gateway fronts.
Administrators register one by resource ID, or accept a suggestion. MOSAIC then reads the
deployments on it and defaults to read-only. It never calls a model. An administrator can separately
opt into reviewed creation of new Azure model deployments, as described below. A resource MOSAIC's
managed identity can't reach, such as one in another Microsoft Entra tenant, can instead be
registered by URL with an API key held in Key Vault; see
[Endpoints reached with an API key](model-endpoints.md#endpoints-reached-with-an-api-key). A
[model pool](model-pools.md) for Claude can also use Claude on
[AWS Bedrock endpoints](model-endpoints.md#aws-bedrock-endpoints).

Azure endpoints registered by resource ID have three separately checked access relationships:

| Relationship | Identity | Plane | What it enables |
| --- | --- | --- | --- |
| Onboarding | MOSAIC's managed identity | Control plane | Listing the deployed models |
| Optional deployment management | MOSAIC's managed identity or selected management connection | Control plane | Creating a new deployment after endpoint opt-in and review |
| Runtime | **The gateway's** managed identity | Data plane | Actually calling those models |

They are reported separately, because an endpoint MOSAIC reads perfectly well can still be
uncallable through a gateway.

Onboarding asks only for `Reader`; enabling deployment management is a separate decision. For the
gateway MOSAIC recommends a built-in role, but it accepts **any**
role whose data actions cover the operations the published API calls:

| Purpose | Recommended role | Role definition ID |
| --- | --- | --- |
| MOSAIC enumerating models | Reader | `acdd72a7-3385-48ef-bd42-f606fba81ae7` |
| Gateway calling an Azure OpenAI resource (`kind: OpenAI`) | Cognitive Services OpenAI User | `5e0bd9bd-7b93-4f28-af87-19fc36ad61bd` |
| Gateway calling an AI Services or Foundry resource, including one registered by Foundry project, and its Claude models | Foundry User | `53ca6127-db72-4b80-b1b0-d745d6d5456d` |
| Gateway calling a resource MOSAIC cannot read yet | Foundry User, which is accepted for either kind | `53ca6127-db72-4b80-b1b0-d745d6d5456d` |

How the gateway's access is judged:

- **The scope is the account the published API calls.** This holds even for an endpoint registered
  by Foundry project, because models are deployed on the parent resource. A grant on the project
  does not reach the account, so it is reported as narrower than what is needed and is not
  counted. Grants on the resource group or subscription do count, and are labelled as inherited.
- **MOSAIC reads each assignment's role definition.** It checks `dataActions` minus
  `notDataActions` against the exact data actions of the operations it publishes. So Cognitive
  Services User, Cognitive Services OpenAI Contributor, or a custom role counts wherever it covers
  them.
- **An AI Services account must cover every API MOSAIC publishes from it.** That includes the
  Anthropic Messages API for Claude, even before a Claude model is deployed. Both of its routes need
  `Microsoft.CognitiveServices/accounts/AIServices/providers/action`.
  - Foundry User and Cognitive Services User grant it.
  - Azure AI Developer doesn't. MOSAIC reports it as missing and names the API that needs the
    action.
  - The `services.ai.azure.com` host and the `https://ai.azure.com` token audience that API uses
    don't change the scope or the roles: it's the same account.
- **Unreadable definitions fall back to a list.** When MOSAIC cannot read a role definition, only
  built-ins already known to be sufficient are trusted.
- **Conditions and deny assignments stop short of "can invoke".** A role assigned under an ABAC
  condition MOSAIC cannot prove holds for those calls is reported as *not confirmed*, never as
  access. A deny assignment covering those calls overrides the role.
- **The network path is part of the verdict.** If public network access is disabled and the gateway
  is not connected to a virtual network, the gateway cannot invoke, whatever roles it holds.
  Private endpoints and firewalls MOSAIC cannot fully evaluate are reported as *not confirmed*, not
  as a denial.
- **Advice matches the check.** Whatever MOSAIC recommends, the check accepts. When the kind is not
  known yet, MOSAIC says so, and granting it `Reader` first lets it recommend the exact role.

[ADR 0013](adr/0013-runtime-readiness-by-data-actions.md) records these rules.

## Deploy a new Azure model

In **Models > Model endpoints**, select an Azure resource and choose **Deploy model**. For a
discovered but unregistered resource, **Register and deploy** first records the endpoint, then
opens the same workflow. Cancelling deployment does not undo registration.

Endpoints stay **Read-only** until an administrator explicitly enables **Manage deployments**.
The workflow reads the live account catalogue, not the last inventory sync. Choose an exact model
version, an Azure-advertised deployment type, capacity in that SKU's native units, an optional
existing content-filter policy, and version-upgrade behavior. Review the parent account, cloud,
region, model, capacity, quota, processing scope, and billing warnings before **Deploy to Azure**.
Foundry projects always deploy onto their parent account.

The management identity needs these actions at the account or an ancestor scope:
`Microsoft.CognitiveServices/accounts/read`, `accounts/models/read`, `accounts/deployments/read`,
and `accounts/deployments/write` (the last three also have the `Microsoft.CognitiveServices/`
prefix). It must be able to read its effective permissions, the subscription's regional usages,
and, for provisioned deployments, the selected model's regional physical capacity. Account-only
Reader does not authorize subscription-level quota reads. Missing, unknown, blocked, unmapped, or
insufficient quota blocks creation; so do unverifiable write permissions. MOSAIC assigns no roles.
The endpoint and the **Deploy model** workspace both show **Deployment readiness**. The summary distinguishes
reading model inventory from creating deployments and reading regional quota. It shows
which checks need attention. Ask an Azure administrator to grant the management identity the
listed deployment actions on the Azure account, and **Cognitive Services Usages Reader** on the
subscription if quota access is missing. An unreadable or empty quota response is not evidence
that quota is exhausted. After changing access, recheck permissions before enabling deployment
management or reviewing a deployment. Gateway invocation uses a different identity and is
reported separately.

Pay-as-you-go, provisioned throughput, batch, and other account-advertised SKUs use Azure's own
capacity limits and quota `usageName` mapping, not a universal tokens-per-minute conversion.
When Azure lists the same SKU for an OpenAI base model and its corresponding fine-tuning variant,
MOSAIC uses the base-model row's limits and quota. It never substitutes fine-tuning quota for
missing base-model quota.
Provisioned capacity can be billable while idle. Quota checks reserve nothing and are not a price
quote. Partner-model Marketplace agreements and other prerequisites must already be satisfied in
Azure; MOSAIC reports Azure's refusal instead of accepting terms or buying capacity.

Creation records a durable operation before contacting Azure and verifies terminal provisioning,
including responses initially marked **Accepted**. A successful create automatically refreshes
inventory. If only refresh fails, use **Retry inventory refresh**; do not create again. After a
timeout or API restart, open **Deployment history** and **Check outcome in Azure**. Uncertain
operations keep the account locked. Before **Confirm worker-stopped recovery**, verify the original
deployment worker has stopped; a terminal Azure resource alone cannot fence a worker paused before
its write. For an absent deployment, also verify that no Azure operation is pending.
If a terminal deployment no longer matches the reviewed configuration, confirmed recovery records
a failed operation and releases the account lock without changing Azure. A running inventory
refresh also requires confirmation that its original worker stopped before **Reconcile interrupted
refresh** releases its claim.

MOSAIC refuses existing deployment names, does not resize, replace, or delete deployments, and
serializes its own deployment writes and endpoint-management changes. Azure's deployment PUT is
an upsert without documented conditional-create support: another external writer can still claim
the name between MOSAIC's last read and PUT. Avoid concurrent external deployment changes on that
account. Provisioning does not publish to APIM, create a grant, or add a pool member; batch
deployments remain ineligible for synchronous publication and pools.

API-key-only Azure registrations, AWS Bedrock, and OpenAI-compatible endpoints do not support this
management workflow. Commercial and Government resources use their stored cloud and management
connection throughout. See [ADR 0028](adr/0028-reviewed-azure-model-deployment.md).

## Native Google Vertex AI and custom chat

These are explicit, separately governed registration contracts, not Azure account discovery.
**Register** saves endpoint, environment, exact declared models and credential references only.
**Check configuration** is offline for these providers: it does not fetch credentials, resolve
DNS, contact Google or test a key. A URL, declaration or successful synthetic protocol test is not
proof that the gateway can invoke a model.

The shared Guided release keeps **Register**, **Review**, **Apply publication**, and **Grant and
connect** separate. Saving publication intent does not create a plan. Explicit Review describes
the route, authentication, gateway blockers and caller consequences without changing APIM.
Unresolved execution evidence keeps Apply blocked on the server as well as in the console.
Neither declaration, publication intent nor a saved grant means a usable connection.

### Native Vertex contract

Choose **Google Vertex AI** and supply an exact Google Cloud project ID. The bounded contract is
native Vertex **v1**, **gemini-3.8-flash**, Standard PayGo **us multi-region**, at the fixed
`https://aiplatform.us.rep.googleapis.com` origin. This is not a US single-region deployment,
the OpenAI compatibility endpoint, a model discovery service, or an automatic global fallback.
Routes bind the project, `us` location, publisher and exact model:

```text
/v1/projects/{project}/locations/us/publishers/google/models/gemini-3.8-flash:generateContent
/v1/projects/{project}/locations/us/publishers/google/models/gemini-3.8-flash:streamGenerateContent?alt=sse
```

Three authentication choices retain independent configuration and unresolved proof:

| Choice | Stored intent | Still unproven for APIM execution |
| --- | --- | --- |
| Workload identity federation | Exact federation audience and service account; no backend secret | Gateway managed-identity subject token, fixed STS exchange and exact service-account impersonation |
| Service-account signing | Exact service account and Key Vault reference; no pasted private key | Gateway-supported restricted-runtime signing, short-lived OAuth exchange, key representation and rotation |
| Google authorization key | Key Vault reference to an operator-managed key | Service-account-bound authorization-key restrictions, exact native US route and streaming acceptance |

These choices never fall back to one another. A Google API key without an authorized principal
is not assumed to confer IAM access. The Google Azure federation guide describes VMs, not proof
that APIM supports this exchange. Saving a secret reference neither reads it nor proves signing.
MOSAIC does not enable an unproved signer, egress configuration or key route.

The bounded request envelope allows inline PNG/JPEG images and application-executed functions.
Native thought signatures and thinking defaults remain opaque and are not rewritten. Remote
media, audio/video, hosted search/code tools and pools are outside this release. Native streaming
uses a separate operation; APIM token-policy documentation listing a schema is not proof of
recognition or counters for that operation. Missing accounting evidence means unknown, not zero
cost or free usage.

### Approved-public-HTTPS custom chat

Choose **OpenAI compatible** to opt a new registration explicitly into the custom-chat contract.
Supply an approved public HTTPS origin/base path, a Key Vault credential reference and exact
case-sensitive model declarations. Only Chat Completions is in scope. Inline images and
application-executed functions are allowed; remote media and server-executed tools are not.
Slash-qualified model IDs remain body values and never become URL paths.

Equivalent default bases such as `https://chat.example.com` and
`https://chat.example.com/v1` identify the same effective `/v1/chat/completions` destination
within one environment. Distinct environments keep separate endpoint and credential IDs.
Existing legacy IDs are not silently migrated. Case-only sibling declarations are rejected
because downstream accounting keys cannot yet distinguish them, while request matching remains
exact and case-sensitive.

Host admission rejects private/loopback/link-local/metadata addresses, non-HTTPS URLs,
credentials in URLs, ambiguous encodings and unsupported ports. Public host syntax alone does
not prove DNS-rebinding protection or constrained gateway egress. No registration probe is made;
runtime stays unavailable until that execution boundary has controlled evidence. Eventual
forwarding must remain unbuffered, accept only the reviewed operation/query and never follow
credentialed redirects. Self-hosted APIM ingress/association work does not establish private
model-backend reachability.

No live provider call, permission assignment, deployment or gateway change is part of these
offline workflows. See the [Phase 10 gates](e2e/roadmap.md#phase-10-gemini-and-aws-bedrock)
for the separate owner-approved live prerequisites.

## Endpoint and gateway access

On the Models page, select an endpoint to open its **Access** card.

The endpoint URL and provider each carry their own evidence. A resource-ID registration starts
with a generated URL and default or supplied provider label; these are **Unconfirmed** until an
account read supplies the corresponding facts. A host-derived provider remains **Inferred from
URL**, not account-confirmed. Older registrations whose provenance was not saved say
**Confirmation not recorded**. A URL registered with an API key is a **Registered URL**, not proof
that MOSAIC read the account. Signing in, a provider label, and account confirmation do not prove
model/runtime or API-schema support.

A successful account-description read can confirm the URL/provider even when MOSAIC still lacks
permission to enumerate models. Confirmation records when each fact was observed. If a later read
fails, the last-confirmed identity and account facts remain visible with their original times;
the failed check is reported separately. An inventory count without a completed sync is **Not
synced**, not evidence that the resource has no models.

Access evidence is dated and names the readiness-rule revision that produced it. **Not checked**
means no attempted check was recorded. **Check out of date** means a previous check has no
revision or used rules different from this API's current rules, even if its old result said it
could invoke or recommended a particular role. Previous verdicts and role commands are available
under a disclosure explicitly marked historical, not as current recommendations. **Check access**
records a new result and shows its outcome and time, including an unchanged verdict or a failed
check. Matching rules mean checked under those rules, not continuously verified access or a
successful model invocation; there is no age-based expiry.

Reading, selecting, or refreshing saved endpoint/access evidence never runs an access check.
This includes
`GET /api/v1/model-endpoints/{id}/runtime-access`, which now retrieves saved evidence rather than
re-evaluating access. API clients that intentionally want a new check must use the existing
`POST /api/v1/model-endpoints/{id}/preflight` and consume its returned endpoint. GET retains its
tenant-scoped list response and unknown-endpoint `404`. Model sync refreshes inventory only,
not the access check. Pool readiness and publish warnings treat old verdicts as not confirmed
without changing existing grants, publications, or applied gateway state.

- **Gateways calling this endpoint** gives each gateway a verdict: *can invoke*, *cannot invoke*,
  or *not confirmed*.
  - When a role satisfies the check, it names the role and the scope where it is assigned.
  - When no role does, it gives the reason for each role the gateway holds: missing data actions, a
    narrower scope, or a condition.
  - When a role is missing, it shows the recommended role and the `az role assignment create`
    command. MOSAIC never runs the command itself.
- **Endpoint settings**, above the gateway verdicts, shows the resource kind, public network access,
  firewall, and key authentication. Those settings decide whether a gateway can reach the endpoint
  at all. Azure omits `disableLocalAuth` from accounts where it was never set, and its default is
  `false`, so MOSAIC shows an unset value as key authentication *Enabled*.

**Persistence compatibility:** new `detailEvidence`, `access.rulesRevision`, and
`runtimeAccess[].rulesRevision` metadata is optional and omitted while unset, so existing
registrations load without a migration. Responses additionally project freshness and the running
rules revision; those derived values are not stored. The first new registration or recorded
access check (including existing key-replacement verification) can populate metadata that an
older extra-forbid API binary cannot read. Reads and inventory sync alone do not backfill it.
The new `accountKindUnconfirmed` runtime reason also requires a reader that understands it.
Upgrade all readers before allowing those writes, using a compatible reader-first rollout or
coordinated replacement. After the first populated write, image-only rollback to a strict older
reader is not safe: follow the [durable-state compatibility and backup
prerequisites](upgrades.md#check-durable-state-compatibility-and-backup-prerequisites) and
quiescence guidance. No automatic metadata downgrade, migration, or deployment tooling is
provided by this feature.

Every role whose name begins `Cognitive Services` or `Foundry` that grants control-plane deployment
read also grants data-plane inference, and most also grant `listKeys`. `Reader` is the only built-in
that grants the read alone. It also covers the role-assignment, role-definition, and
deny-assignment reads that the runtime check needs. MOSAIC also emits a narrower custom role
definition for operators who want one. That role omits the role-assignment read, so runtime access
then reports as *not confirmed* rather than guessing. Roles are compared by GUID rather than name,
because Microsoft renamed the Foundry roles in 2026 (`Azure AI User` became `Foundry User`) without
changing their IDs.

MOSAIC finds endpoints three ways: a pasted resource ID, hosts it already observed as AI backends
inside a registered gateway, and an enumeration of Azure AI accounts across visible subscriptions.
The last needs `Reader` at subscription scope, which MOSAIC does not grant itself — a subscription
it cannot read is reported with the command that would fix it and skipped, so one missing assignment
never blanks the list. When MOSAIC can't see any subscription, or couldn't list them, the Models page
now says so and gives the `Reader` command for the subscription MOSAIC was deployed into and for
each registered gateway's subscription, instead of showing an empty list.

A subscription MOSAIC can list is not necessarily one it can read in full: Azure answers a
subscription-wide list with only the resources the caller can read, and doesn't say anything was
left out. So MOSAIC also checks the permissions it holds at each subscription itself. When those
don't let it read every Azure AI resource there, typically because its roles are on a resource
group or on individual resources, the Models page says MOSAIC can read only part of that
subscription, still offers what it found, and gives the subscription-scope `Reader` command instead
of reporting nothing new to register. If MOSAIC can't read its own permissions, it makes no claim
either way.

Azure can take several minutes, and occasionally longer, to apply a new role. So when MOSAIC asks
for `Reader` on its own identity to read an endpoint or to scan subscriptions, it says so: if
**Check access** or the scan still fails right after the grant, wait a few minutes and try again.

OpenAI-compatible endpoints are registered with a Key Vault secret identifier the operator created.
MOSAIC stores the URI only; discovery for those endpoints is not implemented yet. An explicit
**Check access** request reports that verification is unsupported instead of recording a completed
check. An Azure OpenAI or Foundry URL isn't registered as OpenAI-compatible: it takes the key path below.

## Endpoints reached with an API key

Register an Azure OpenAI or Foundry resource by resource ID whenever MOSAIC can reach it: MOSAIC
then reads its deployments with its managed identity, and no key is involved. When it can't, most
often because the resource is in another Microsoft Entra tenant and its answer is "Token tenant ...
does not match resource tenant", register the resource by URL with its API key, or with the Key
Vault secret that holds it. [ADR 0018](adr/0018-key-authenticated-backends.md) records the
design, and [ADR 0021](adr/0021-keys-mosaic-keeps.md) how MOSAIC keeps a key it's given.

1. **Give MOSAIC the key, or the secret that holds it.**
   - **Paste the key**, the console's default. MOSAIC writes it into a new secret in the Key Vault
     deployed with it, named `mosaic-apikey-<resource>-<random>` and tagged with the endpoint, and
     keeps only the secret's URI. It never shows the key again. The environment's API Management
     can already read that vault, so the key is ready to publish. **Replace API key** on the
     endpoint's **Access** card writes a new version of the same secret, and removing the endpoint
     deletes the secret.
   - **Or store the key in Key Vault yourself**, and give MOSAIC only the secret's URI. The Key
     Vault deployed with MOSAIC (`azd env get-value KEY_VAULT_NAME`) already lets MOSAIC's API and
     the environment's API Management read secrets. Add the key as a secret in the Azure portal, or
     with `az keyvault secret set --vault-name <vault> --name <name> --file <file holding the key>`,
     which keeps the key out of your shell history. The file must hold the key alone, with no line
     break at its end: MOSAIC reports a key that starts or ends with one and never sends it. For
     another vault, grant Key Vault Secrets User on it to MOSAIC's API identity and to each gateway
     that publishes from the endpoint. The endpoint's **Access** card gives the exact commands.
2. **Register it.** On the Models page, **Register endpoint** > **Azure AI with an API key**. Give
   the resource endpoint (`https://<resource>.services.ai.azure.com`, `.cognitiveservices.azure.com`
   or `.openai.azure.com`) or a Foundry project endpoint
   (`https://<resource>.services.ai.azure.com/api/projects/<project>`), and the key or the secret URI
   (`https://<vault>.vault.azure.net/secrets/<name>`). The same request is
   `POST /api/v1/model-endpoints` with `endpoint`, `apiKey` or `credentialSecretUri` and,
   optionally, `deployments`.
3. **Declare its deployments.** An API key can't list a resource's deployments: Foundry lists them
   only to a Microsoft Entra token. So name each deployment to publish and the API it takes: the
   Azure OpenAI API, the Foundry Models API, or the Anthropic Messages API for Claude. Declare them
   while registering, or later from the endpoint's **Deployments** card
   (`POST` and `DELETE /api/v1/model-endpoints/{id}/declared-deployments[/{name}]`). The console
   marks them *declared, not discovered*. A declared deployment can't be removed while it's
   published, or while a model pool's gateway may still hold its backend. Otherwise the pools that
   use it forget it, as they do when its endpoint is removed.

What MOSAIC does with it:

- **It stores the URI, not the key, and without its version.** MOSAIC and API Management both read
  the current version, so a rotated key reaches the gateway within four hours without touching
  MOSAIC.
- **It keeps a key it's given only in Key Vault.** `apiKey` is write-only: no response, record, log
  or error repeats it, and a request MOSAIC refuses isn't repeated back either. MOSAIC trims what
  was pasted around a key and refuses anything with a space or line break inside. To replace a key
  MOSAIC keeps, use **Replace API key**, or `PATCH /api/v1/model-endpoints/{id}` with only
  `apiKey`. After Key Vault accepts it, MOSAIC records the replacement with writes that can't
  conflict. If that record still fails, MOSAIC says the new key is in use, and **Check access**
  refreshes the status. API Management picks up the new version within four hours, so paste the
  resource's other key and regenerate the old one afterwards. A key MOSAIC keeps can't be swapped
  for a secret URI, or the reverse: remove the endpoint and register it again.
- **It checks the key and keeps nothing.** Registration and **Check access** read the secret with
  MOSAIC's identity and send one request that runs no model (`GET /openai/models`) with the key in
  `api-key`. MOSAIC reads only the status code and drops the key. It never logs, stores or returns
  a key, or puts one in an error. **Sync models** doesn't apply to these endpoints.
- **It registers a resource once, whatever host names it.** A resource answers on three hostnames
  that share its subdomain. A second registration of it by key, or by resource ID when it's
  already reached by key, or the reverse, is refused with a `409` that names the existing one.
- **It reports the two relationships these endpoints have.**
  - **MOSAIC**: whether it read the key and whether the endpoint accepted it. If it can't read the
    secret, the card gives the `az role assignment create` command for Key Vault Secrets User on
    the vault.
  - **Each gateway**: whether its managed identity can read the key from Key Vault. Any role whose
    data actions include `Microsoft.KeyVault/vaults/secrets/getSecret/action` counts, whether it's
    on the secret, the vault or above. An access-policy vault counts Get. A firewall that doesn't
    admit trusted Microsoft services is *not confirmed*. A missing role comes with its command.
    MOSAIC needs Reader on a vault to read who holds roles there. Without it, or when MOSAIC
    can't find the vault in the subscriptions it can read, the verdict is *not confirmed*, never a
    denial, and the command resolves the vault with `az keyvault show`.

**One registration per Azure AI resource.** Deployments live on the resource (the account), never
on a Foundry project, so a project and its parent resource list the same models. MOSAIC treats every
registration as covering its resource:

- Registering a resource whose project is already registered, a project whose resource is, or a
  second project on the same resource is refused with a `409`. The message names the endpoint that
  already lists those models, and `details` carries its `id` and `name`.
- Discovery doesn't suggest a resource that a registered project already covers.
- Registering the exact same resource ID again is refused as before.

Overlapping records registered before this check are left as they are. Remove one of them to stop
the duplicate listing.

**Removing an endpoint.** Remove on the Models page asks first. The dialog says what goes: MOSAIC's
record of the endpoint, its synced models and its sync history. Nothing changes in Azure. The API
(`DELETE /api/v1/model-endpoints/{id}`) refuses with a `409` while any publication from the endpoint
may still own resources in API Management, because without the endpoint that publication could
never be planned, applied or unpublished again, and its API would keep serving traffic. A
publication blocks when it:

- recorded resources it created, including a failed apply that left some behind;
- is applying, or its access change is applying or was interrupted (`accessState` unknown);
- still has an enabled grant applied at the gateway;
- is locked by a run in progress.

The refusal lists the blocking publications (id, display name, status) in `details`, and the dialog
shows them. Unpublish those models first. Publication records that own nothing — drafts, planned or
rolled-back publications, and unpublished ones — are deleted with the endpoint and audited as
`publication.removed`, because they could never be planned again without it.

[Model pools](model-pools.md) are checked the same way. Removal is refused with a `409` while a pool
that uses the endpoint is applying or locked by a run, or its gateway may still hold a backend the
pool wrote for one of the endpoint's deployments. `details.modelPools` lists those pools. Remove
the deployments from them and apply them again first. Any other pool forgets the endpoint's
deployments, and any model they leave empty, and is audited as `modelPool.membersRemoved`. Its
last plan no longer describes it, so it has to be planned again.

## AWS Bedrock endpoints

A Claude [model pool](model-pools.md) can use Claude models on Amazon Bedrock, alongside Foundry
deployments. MOSAIC reaches Bedrock through its Anthropic Messages API, with a Bedrock API key held
in Key Vault. Requests to these models, prompts included, leave Azure, and AWS processes them.
[ADR 0024](adr/0024-model-pools.md) records the design.

1. **Provide an operator-managed Bedrock API key.** AWS recommends short-term keys for production
   (at most 12 hours); long-term keys are for exploration. You own expiry and rotation; MOSAIC
   does not renew keys. Allow for API Management's Key Vault refresh delay of up to four hours.
2. **Register the host.** On the Models page, **Register endpoint** > **AWS Bedrock**. Give the
   Bedrock host for one AWS region: `https://bedrock-runtime.<region>.amazonaws.com`, which AWS
   recommends, or `https://bedrock-mantle.<region>.api.aws`. Then paste the key, which MOSAIC keeps
   as it does an Azure key, or give the URI of the Key Vault secret that holds it. The same request
   is `POST /api/v1/model-endpoints` with `provider` set to `awsBedrock`. MOSAIC also infers that
   provider from a Bedrock host given with a key or with model IDs.
3. **Declare the model IDs to serve**, up to 50, each with the Anthropic Messages API. A model ID
   is `anthropic.<model>`, or an inference profile such as `us.anthropic.<model>` or
   `global.anthropic.<model>`. A Bedrock API key can't list models, so **Sync models** doesn't
   apply.

What MOSAIC does with it:

- **It registers a host once.** Every AWS account in a region shares the region's hosts. To use
  another key, replace the endpoint's key. The region comes from the host. FIPS, China, and
  control-plane hosts are refused, and so is a model ARN, which provisioned throughput needs.
- **Pools remain supported.** Standalone governed publication is also available for the exact
  `anthropic.claude-sonnet-5` declaration on `https://bedrock-mantle.us-east-1.api.aws`,
  on an APIM v2 gateway. Other model/origin tuples are shown as not publishable with a reason.
  See [standalone Bedrock publication](model-publishing.md#standalone-bedrock).
- **Registration never sends the key to AWS.** AWS has no unbilled request a Bedrock API key can make, so
  registration and **Check access** only read the key from Key Vault, and say so. A pool member on
  the endpoint is never more than *not confirmed*, and the first authorized runtime request is the
  key's first test. Each gateway's verdict is whether its managed identity can read the key from
  Key Vault, as for an Azure key.
- **It reads capacity from the model ID.** Every ID MOSAIC accepts is pay-as-you-go. An
  `anthropic.` model is regional, a `global.` profile is global, and another geography's profile,
  such as `us.` or `eu.`, is a data zone. Any other ID's scope is unknown.
- **It leaves the price to an administrator.** MOSAIC can't tell which cloud an AWS host is in, so
  calls to these models are unpriced until an administrator names the endpoint's cloud and adds a
  [price](analytics-reference.md#pricing). Standalone cost remains incomplete even with a price:
  aggregate gateway usage cannot distinguish cache reads and writes. This does not change the
  price of an eligible neighboring pool using the same endpoint and deployment.
- **It warns that token counting may fail on `bedrock-runtime`.** AWS documents the Anthropic
  API's token counting only on `bedrock-mantle` hosts, so a pool's plan says a token count sent to
  a `bedrock-runtime` member may fail. A governed pool doesn't offer token counting.
