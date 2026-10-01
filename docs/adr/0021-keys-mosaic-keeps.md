# ADR 0021: Keep an API key an administrator gives MOSAIC in MOSAIC's own Key Vault

**Status:** Accepted. Amends [ADR 0018](0018-key-authenticated-backends.md) for endpoints
registered this way.

## Context

[ADR 0018](0018-key-authenticated-backends.md) lets MOSAIC publish an Azure AI resource it can't
reach with its managed identity, such as a Foundry project in another Microsoft Entra tenant. It
asked the administrator to store the resource's API key in Key Vault and give MOSAIC only the
secret's URI, so the key never passed through MOSAIC.

That doesn't fit how administrators work. The person registering a partner's endpoint has its URL
and key, and often no rights on any Key Vault: an RBAC vault grants none to a subscription owner
until someone assigns a data-plane role. The first person to try the feature wanted to paste the
URL and key into MOSAIC.

Three constraints shaped the design:

- **The key must still never enter MOSAIC's storage, logs, responses or errors.** MOSAIC's records
  are in Cosmos and its logs go to Application Insights. Neither should ever hold a key.
- **API Management must read the key the way ADR 0018 built**, through a Key Vault-backed named
  value with its own identity, so publishing, governed access and rotation don't change.
- **The environment's vault has purge protection**, with 90 days of soft delete. A deleted
  secret's name stays taken until then, so a name can't be reused for a new secret.

Two things FastAPI and structlog do by default would leak a key that passes through MOSAIC:

- A 422 response repeats each validation error's `input`. A rule that spans fields reports the
  whole request body as its input, key included.
- structlog's `dict_tracebacks` renders every frame's local variables when an exception is logged.
  A frame on the way to the error can hold a key.

## Decision

**MOSAIC takes the key, writes it into its own Key Vault, and keeps only the secret's identifier.**

- `POST /api/v1/model-endpoints` accepts `apiKey` in place of `credentialSecretUri` for an Azure
  OpenAI or Foundry URL: one or the other, never both. A resource-ID registration takes no key.
  MOSAIC stores a key itself only for an Azure AI resource, so an OpenAI-compatible endpoint still
  takes a secret URI.
- MOSAIC trims what was pasted around the key, and refuses anything else that isn't 16 to 512
  printable ASCII characters, with no space or line break inside.
- MOSAIC writes the key with its managed identity into a new secret in the vault it was deployed
  with (`MOSAIC_KEY_VAULT_URI`). The secret is named `mosaic-apikey-<resource>-<8 random hex>` and
  tagged `managedBy: MOSAIC` and `mosaicModelEndpoint: <endpoint id>`. The random part means a
  removed endpoint can be registered again while its old secret is still kept.
- The endpoint records `keyStoredByMosaic: true`. Its credential reference holds the versionless
  identifier, exactly like a secret the administrator stored. From there, ADR 0018 applies
  unchanged: the key check, gateway readiness, the named value, the policy and unpublish.
- The write comes after every check that could refuse the registration: a duplicate resource,
  declarations and the environment. If registration fails after the write, MOSAIC deletes the
  secret again. If that delete fails too, MOSAIC logs which endpoint and vault, without a
  traceback, and the tags let an operator find the secret.
- A deployment with no vault of its own (no `MOSAIC_KEY_VAULT_URI`) refuses a key with a `409`,
  and asks for a secret URI instead.

**A key MOSAIC keeps is replaced in place and deleted with its endpoint.**

- **Replace API key** in the console, or `PATCH /api/v1/model-endpoints/{id}` with `apiKey`,
  writes the next version of the same secret, under the endpoint's lease. The identifier doesn't
  change, so publication digests don't either, and no plan needs a review. MOSAIC checks the new
  key at once. API Management picks it up within four hours, so the console says to paste the
  resource's other key and regenerate the old one afterwards.
- Removing the endpoint deletes the secret. The delete comes after the publication checks and
  before anything else is removed, so a vault that refuses it changes nothing. A secret that's
  already gone doesn't block the removal. The vault keeps the deleted secret, recoverable, for its
  retention period.
- Modes don't mix. MOSAIC refuses a new key for an endpoint whose secret the administrator
  manages, and a new secret URI for one whose key MOSAIC keeps. To switch, remove the endpoint
  and register it again, which unpublishing already guards.

**The key has one path through MOSAIC: request body, `SecretStr`, the writer's request body.**

- `apiKey` is a write-only `SecretStr` (`format: password`, `writeOnly` in the OpenAPI document).
  It's excluded from every dump, and its repr is masked.
- The request models set `hide_input_in_errors`, so a validation error's text never shows a key.
- `KeyVaultSecretWriter` puts the value in one request body. It never reads Key Vault's response,
  which repeats the value. Its errors name the vault and the status, never the value or what the
  vault said. They're raised `from None`, so no chained exception carries the frames that held
  the key.
- **422 responses no longer repeat the request.** A handler drops `input` from every validation
  error, for every route. Each error keeps its `loc`, `msg` and `type`, so where and why are
  unchanged. Nothing in MOSAIC's console, portal or e2e harness reads `input`. The console now
  shows these errors as sentences, where it previously showed an unreadable message.
- **Logged tracebacks carry no local variables.** Exceptions are rendered with
  `ExceptionDictTransformer(show_locals=False)` rather than `dict_tracebacks`. This also covers
  the MCP preflight and every other `logger.exception`, whose frames could hold a token or a key.

**MOSAIC's identity can now write secrets in its own vault.** `infra/main.bicep` gives the API
identity Key Vault Secrets Officer on the environment vault, beside Secrets User and Reader. The
role is scoped to that vault alone. API Management keeps only Secrets User.

## Consequences

- An administrator can register a partner's Foundry endpoint with nothing but its URL and key.
  The secret-URI path stays, for administrators who keep the key out of MOSAIC entirely.
- A pasted key passes through MOSAIC's API once, in memory and over TLS. A secret URI never sends
  the key through MOSAIC at all, so administrators who need that guarantee should use one.
- MOSAIC's identity can create, overwrite and delete any secret in its own vault, not only the ones
  it creates. MOSAIC writes only names it generated and recorded, but the role allows more.
  Keep other teams' secrets in other vaults.
- Each removal or failed registration leaves a soft-deleted secret, which the vault purges after
  90 days. That doesn't cost a name, because every secret MOSAIC creates is new.
- A rotated key reaches API Management within four hours, as in ADR 0018. MOSAIC doesn't force
  a refresh of the named values.
- `keyStoredByMosaic` is left out of stored documents while false, so records unrelated to it stay
  readable by the previous release. An endpoint whose key MOSAIC keeps isn't. Before rolling back,
  remove those endpoints, which deletes their secrets.
- 422 bodies changed shape slightly: they no longer include `input`. A client that read it must
  now rely on `loc` and `msg`.
