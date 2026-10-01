# ADR 0018: Reach an Azure AI endpoint with an API key held in Key Vault

**Status:** Accepted. Amends [ADR 0006](0006-model-endpoint-onboarding.md) and
[ADR 0013](0013-runtime-readiness-by-data-actions.md) for endpoints registered this way. Amended by
[ADR 0021](0021-keys-mosaic-keeps.md): an administrator can also give MOSAIC the key itself, which
MOSAIC keeps in its own Key Vault.

## Context

ADR 0006 identifies every Azure OpenAI and Foundry endpoint by resource ID. MOSAIC reads its
deployments with its managed identity, and each gateway calls it with the gateway's managed
identity. That model can't reach a resource in another Microsoft Entra tenant. A managed identity's
token names its own tenant, and the endpoint refuses it: "Token tenant ... does not match resource
tenant". So a Foundry project that a partner hosts, with the Claude deployment an environment owner
needs, couldn't be registered, listed or published.

What such an owner does have is the endpoint's URL and its API key. The requirement was to publish
it through API Management with everything a managed-identity endpoint gets: the curated API shapes,
the reviewed plan and apply, governed access, limits, telemetry and grant attribution. The key must
never enter MOSAIC's storage, logs, responses or errors.

What an API key can do on the Foundry data plane decides most of the design.

- **It can't list deployments.** The Foundry project API that lists every deployment
  (`GET {project}/deployments`) accepts only a Microsoft Entra token for `https://ai.azure.com`:
  the AIProjects v1 specification defines OAuth2 and no API key. The Azure OpenAI data plane listed
  deployments only in api-version 2022-12-01, and the listing is gone from every later version.
  `GET /openai/models` lists the models a resource offers, not its deployments. Anthropic's
  `AnthropicFoundry` client disables its models endpoint outright.
- **It can be checked without a billed call.** `GET {resource}/openai/models?api-version=2024-10-21`
  (Models - List, GA) takes the `api-key` header, runs no model and costs nothing. It proves the key
  and the resource, not that any deployment exists.
- **Each API shape takes a known header.** Azure OpenAI routes and the Foundry Models API take
  `api-key`. Microsoft Learn's Claude-in-Foundry reference shows `x-api-key` for
  `/anthropic/v1/messages`, with `anthropic-version: 2023-06-01`. Anthropic's own `AnthropicFoundry`
  client sends `x-api-key`, and `api-key` as well "for backwards compatibility".
- **Keys belong to the resource.** The inference routes (`/openai`, `/models`, `/anthropic`) are
  served at the resource's host, and a Foundry project endpoint
  (`https://<resource>.services.ai.azure.com/api/projects/<project>`) carries only project APIs.

API Management already has the mechanism for a key it should read itself. A Key Vault-backed named
value holds a secret's identifier, and API Management reads the value with its own managed
identity. It needs Key Vault Secrets User on the vault, or Get and List in an access policy. It
refreshes a versionless reference within four hours of a rotation. Through a Key Vault firewall it
needs its system-assigned identity and "Allow trusted Microsoft services". A policy then refers to
the named value as `{{name}}`. API Management refuses to delete a named value a policy still names.
Anyone who can edit policies can read any named value through a policy, and a Key Vault value shows
in a request trace to anyone whose subscription allows tracing.

## Decision

**Registration by URL and Key Vault secret is an explicit alternative, not a second default.**
`POST /api/v1/model-endpoints` with an `endpoint` on an Azure AI host (`*.services.ai.azure.com`,
`*.cognitiveservices.azure.com` or `*.openai.azure.com`) and a `credentialSecretUri` registers a
key-authenticated Azure endpoint. The console offers it on its own tab and says to register by
resource ID whenever MOSAIC can. An Azure AI URL without a secret is refused with that advice.

- MOSAIC accepts the resource endpoint, one of the base paths the portals show beside it (`/openai`,
  `/openai/v1`, `/models`, `/anthropic`, `/anthropic/v1`), or a Foundry project endpoint. It stores
  the resource's origin as the endpoint, its subdomain as the account and the project as the
  project. It refuses `http`, a port, user information, a query, a fragment, an operation path, and
  a regional or other host. Only these hosts ever receive a key.
- The provider follows the host: `openai.azure.com` is Azure OpenAI; the other two are Foundry. The
  request may name either Azure provider instead. An Azure AI URL can no longer be registered as
  OpenAI-compatible, because it would duplicate the resource.
- The secret URI must be a Key Vault secret identifier:
  `https://<vault>.vault.azure.net/secrets/<name>`, optionally with a version, under the vault
  suffixes MOSAIC's reader accepts. MOSAIC stores it **without its version**, so MOSAIC and API
  Management both read the current key after a rotation.
- One resource answers on three hostnames that share its subdomain, so a resource is registered once
  whatever host names it. A key registration of a resource that another registration reaches, by
  resource ID, by key or as OpenAI-compatible, is refused. So is a resource-ID registration of a
  resource already reached by key.

**Deployments are declared, and marked as declared.** An administrator names each deployment to
publish (deployment name, model and version, and the curated API shape) when registering, or later
through `POST /api/v1/model-endpoints/{id}/declared-deployments` and
`DELETE .../declared-deployments/{name}`. The shapes follow ADR 0012, except that the administrator
chooses. The Azure OpenAI API suits any Azure AI resource. The Foundry Models API and the Anthropic
Messages API need a Foundry resource. Names are held to the characters Azure deployment names use,
because they become literal operation paths and a pinned request model. A declaration is immutable:
changing one means removing it and declaring it again. Removing one is refused while a publication
of it may own API Management resources, as removing an endpoint is. Sync is refused on these
endpoints, and publishable models carry `declared: true`.

**MOSAIC checks the key and never keeps it.** Registration and **Check access** read the secret with
MOSAIC's identity, send one unbilled `GET /openai/models` with the key in `api-key`, read only the
status code and drop the key. The key lives in a local variable for that call and goes nowhere
else. It isn't logged, stored, returned, cached or put into an error. The outcome is recorded, never
raised, so a vault that was briefly unreachable doesn't lose the registration:

| What happened | Status | What MOSAIC says |
| --- | --- | --- |
| Secret read, key accepted | `connected` | The endpoint accepts the key |
| Secret not readable | `unauthorized` | The `az role assignment create` command for Key Vault Secrets User on the vault |
| Secret missing or empty | `degraded` | Store the key there, or set the secret URI |
| Key starts or ends with a space or line break | `degraded` | Store the key alone; nothing is sent |
| Key refused (401 or 403) | `unauthorized` | Store the current key, and check key authentication is allowed |
| Vault or resource unreachable, or an answer that settles nothing | `unreachable` or `degraded` | Not evaluated; this isn't a denial |

This is the one place MOSAIC holds inference-capable access to a model endpoint. It is confined to
endpoints an administrator registered this way, and MOSAIC uses it for nothing but the check. ADR
0006's "no inference right, no key access" still holds for every endpoint registered by resource ID.

**API Management reads the key itself, through a named value each publication owns.** A publication
from a key-authenticated endpoint records `backendKeyName`: a Key Vault-backed secret named value,
`<backend name>-key`. MOSAIC creates it as a plan step with `secret: true` and the versionless
identifier. It sends no `identityClientId`, so API Management uses its system-assigned identity,
the only one it can use through a Key Vault firewall. MOSAIC hands API Management the identifier
and never the key, and it never calls `listValue`. Owning one per publication keeps ADR 0010's rule
that publications share no mutable API Management resource.

**The policy fragment sets the key after removing every caller credential.** In the legacy and the
governed fragments alike, a key-authenticated publication first removes the `api-key`, `x-api-key`,
`Authorization` and `Ocp-Apim-Subscription-Key` headers and the `api-key` and `subscription-key`
query parameters, each once. The shape's headers follow, then
`<set-header name="…" exists-action="override"><value>{{named value}}</value></set-header>` in place
of `authentication-managed-identity`, then routing, limits and metrics as before.

| Shape | Backend route | Header the gateway sets |
| --- | --- | --- |
| Azure OpenAI | `/openai/deployments/{deployment}/…` | `api-key` |
| Foundry Models | `/models/chat/completions` | `api-key` |
| Anthropic Messages | `/anthropic/v1/messages` | `x-api-key` |

So a caller can neither supply nor override the backend key, and it doesn't leak to the backend
owner, who may be another organization. The header is set in MOSAIC's fragment, after MOSAIC's
authorization, rather than as a credential on the backend entity. That way the key goes only on
requests MOSAIC's policy admitted, never on those of another API that routes to the same backend,
and it appears in the reviewed facets. Microsoft documents both options. A managed-identity
publication's policy is byte-for-byte what it was.

**The plan orders, verifies and removes the named value.**

- It comes first in `CREATE_ORDER`, before the fragment that names it. In a governed apply it comes
  right before the backend.
- After the write, MOSAIC reads the named value back. If API Management reports that it couldn't
  read the secret (`keyVault.lastStatus` other than `Success`), MOSAIC deletes it again and fails
  the step, with what to grant.
- Any Azure error for that step has the secret identifier replaced by `[redacted]`, in the run and
  in the log.
- The plan digest covers the named value and the secret, so pointing the endpoint at another secret
  needs a new review. Every other publication's digest is unchanged.
- MOSAIC refuses to take over a named value it didn't create, and refuses a gateway known to have no
  managed identity.
- Unpublish, including ADR 0010's reviewed unpublish plan, deletes the named value last, after the
  fragment that names it. The review lists it, saying the key stays in Key Vault. A failed apply's
  rollback removes it too.

**Readiness asks the questions a key endpoint actually has.** It replaces ADR 0006's Reader check
and ADR 0013's role check:

- **MOSAIC's access** is the key check above.
- **Each gateway's access** asks whether its managed identity can read the secret. MOSAIC
  evaluates the vault's role assignments exactly as ADR 0013 evaluates a model endpoint's: any
  role whose data actions include `Microsoft.KeyVault/vaults/secrets/getSecret/action` counts. That
  role can sit on the secret, the vault or above, which is reported as inherited. A condition MOSAIC
  can't prove, an unreadable definition or a deny assignment stops short of "can read". A vault that
  uses access policies is judged by whether one gives the gateway Get. The vault's firewall is
  unverified unless it admits trusted Microsoft services, or the gateway's addresses.
- **What to run.** A missing role gets the exact
  `az role assignment create ... --role "Key Vault Secrets User" --scope <vault>` command, and an
  access-policy vault gets `az keyvault set-policy`.
- **Finding the vault.** A secret URI doesn't name a vault's resource ID. MOSAIC knows its own
  vault's from `MOSAIC_KEY_VAULT_RESOURCE_ID` and looks any other up by name in the subscriptions
  it can read. Reading assignments needs `Microsoft.Authorization/roleAssignments/read` there:
  Reader on the vault, which grants no secret. When MOSAIC can't find or read the vault the row is
  *not confirmed*, never a denial. Its command resolves the scope with
  `$(az keyvault show --name <vault> --query id --output tsv)`, which runs as shown in Bash and
  PowerShell alike.
- **Nothing names the secret.** Responses name the vault, never the secret or its URI. Re-checking
  gateways never reads the key.

**Infrastructure grants what the environment's own vault needs.** `infra/main.bicep` gives API
Management's system-assigned identity Key Vault Secrets User on the environment vault, and MOSAIC's
API identity Reader on it. It also passes the vault's resource ID as `MOSAIC_KEY_VAULT_RESOURCE_ID`.
A key stored in that vault is then ready to publish, and its readiness can be evaluated, without
further grants.

**The mechanism is shaped for more than Foundry.** `integrations/backend_keys.py` holds the named
value naming, the credential stripping, the per-shape header and the named value body. The writer
creates and deletes named values, and the Key Vault readiness check takes any secret. An MCP server
or a future OpenAI-compatible, Gemini or Bedrock backend can publish with a key by owning a named
value and rendering the same statements. Only the Foundry and Azure OpenAI case is built here.
MCP key backends are still refused (ADR 0017).

## Consequences

- A resource in another tenant publishes with the same shapes, governed access, limits, telemetry
  and grant attribution as one MOSAIC reads by resource ID.
- MOSAIC can't confirm a declared deployment exists. A misspelt name surfaces as a 404 from the
  resource when the published API is called, and the console says so.
- MOSAIC's identity can read the keys of endpoints registered this way, and anyone who can edit API
  Management policies can read any named value. Both are stated plainly. MOSAIC-created
  subscriptions never allow tracing, so request traces don't show the key.
- A key rotation reaches API Management within four hours with no MOSAIC action. A new secret URI
  needs a re-plan, which the digest enforces.
- New fields (`declaredDeployments` on an endpoint, `backendKeyName` on a publication) are left out
  of stored documents while empty, so records unrelated to key endpoints stay readable by the
  previous release. A key endpoint, its publications, and plans or runs with a `namedValue` step
  aren't. Before rolling the API back, unpublish and remove key endpoints.
- A customer policy that refers to a MOSAIC named value blocks its deletion, and unpublish reports
  that step as failed with what is left.
