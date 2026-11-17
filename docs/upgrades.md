# Upgrade MOSAIC and recover safely

[Documentation](README.md) | [MOSAIC](../README.md)

Use this runbook for an **existing** reference deployment. For first-time provisioning, use
[Deployment](deployment.md). Updating MOSAIC is separate from reviewing and applying a model,
pool, MCP, or access plan to API Management.

**Validation boundary:** the instructions below have been checked against repository source and
local CLI help, not a live upgrade or recovery exercise. Deployment, authentication, worker
continuity, runtime access, and restore outcomes remain unverified until separately exercised on
an approved non-production target. See [Evidence and unverified exercises](#evidence-and-unverified-exercises).

- [Choose the update mechanism](#choose-the-update-mechanism)
- [Before the upgrade](#before-the-upgrade)
- [Deploy application images](#deploy-application-images)
- [When provisioning is required](#when-provisioning-is-required)
- [Verify the upgrade](#verify-the-upgrade)
- [Roll back or recover](#roll-back-or-recover)
- [Evidence and unverified exercises](#evidence-and-unverified-exercises)

## Choose the update mechanism

[`azure.yaml`](../azure.yaml) defines three Docker services: `api`, `web` (administrator console),
and `portal`. Each uses an App Service host and a remote ACR build. The normal application update
is a service-specific `azd deploy` from a reviewed source checkout. A retained container image
can instead be supplied with `azd deploy <service> --from-package <image-reference>`.

The command forms in this guide were checked with `azd` 1.37.3. Check your installed CLI's help
and the [azd reference](https://learn.microsoft.com/azure/developer/azure-developer-cli/reference#azd-deploy)
before using them. A CLI accepting a command is not evidence that the deployment has succeeded.

Choose an explicit source commit approved for your environment and review its changes and
applicable [quality checks](development.md#quality-checks). This repository does not document a
supported-version/downgrade matrix or an automatic release channel. Do not invent a tag name,
follow a moving `latest` tag, or assume every commit on `main` is compatible with your state.

| What changes | Update boundary |
| --- | --- |
| Application code/images | Deploy the reviewed API, console, and portal. Check client/API and persisted-state compatibility first: application startup can resume work and write state. |
| Infrastructure, configuration, or identity | Review Bicep, parameters, host settings, and Entra hooks separately. `azd deploy` is not `azd provision`; deploying code does not apply the latter's changes. |
| Persisted state or external effects | Establish compatible readers/writers and a recovery path before allowing writes. Cosmos, APIM, Entra, Key Vault, and provider receipts do not roll back together. |

These categories can overlap. There is no atomic three-service update, deployment-slot swap, or
zero-downtime guarantee in this runbook. If old/new frontends and API cannot safely coexist,
arrange a maintenance window that excludes incompatible clients and writers before proceeding.
An azd environment selects the deployment; MOSAIC's [resource environments](environments.md)
classify offerings inside it and do not select the deployment target.

## Before the upgrade

### Select a reviewed revision and the existing target

Use the existing deployment's operator checkout, with its approved azd environment configuration
available. Obtain Azure/Entra access through your normal authorized process; this guide does not
grant permissions or acquire credentials. Review the [prerequisites](development.md#prerequisites).

All angle-bracketed values below are placeholders. Replace them with approved values locally,
never real inventory or secrets in a repository edit. Run commands from the repository root.
These are operator steps, not an unattended script: run one command at a time and check success
before continuing. For native `git`, `az`, and `azd` commands, require `$LASTEXITCODE -eq 0`.

First inspect the current checkout and the proposed change range:

```powershell
Set-Location "<mosaic-checkout>"
git status --short
git rev-parse HEAD
git --no-pager diff --stat "<current-source-commit>" "<approved-source-commit>"
git --no-pager diff "<current-source-commit>" "<approved-source-commit>" -- azure.yaml infra apps\api\src\mosaic_api\config.py docs
```

Inspect the complete relevant code/data/configuration diff, not just that summary or the selected
paths. Record unknown running-source provenance honestly and resolve compatibility before
proceeding. Health responses and the hard-coded OpenAPI version do not identify a source commit.
Stop if the checkout has unreviewed changes; do not discard someone else's work.

With the approved commit available locally, select it in this operator checkout, then select the
**existing** azd environment:

```powershell
git switch --detach "<approved-source-commit>"
git rev-parse HEAD
azd env select "<existing-environment>"
azd env get-value AZURE_SUBSCRIPTION_ID --environment "<existing-environment>"
azd env get-value API_APP_RESOURCE_ID --environment "<existing-environment>"
azd env get-value WEB_APP_RESOURCE_ID --environment "<existing-environment>"
azd env get-value PORTAL_APP_RESOURCE_ID --environment "<existing-environment>"
azd env get-value AZURE_CONTAINER_REGISTRY_NAME --environment "<existing-environment>"
```

This checkout selection is not a change to the contribution branch strategy. Compare the exact
source revision, subscription, resource groups, app resource IDs, and registry with the approved
deployment record and actual targets. Saved azd outputs can be stale. Stop on any missing or
unexpected target, failed command, or prompt to initialize/create another environment. Recover
the existing environment configuration through your approved process; do not use `azd env new`
or `azd init` to work around missing configuration.

### Record images and configuration

Keep a pre-upgrade change record in your protected operational records, not source control:

| Record | What to preserve |
| --- | --- |
| Version and tools | Current/target source revisions where known, reviewed changes, CLI versions, deployment time, and the intended three-service image set. |
| Container artifacts | Actual fully qualified image references and resolved digests for API, console, and portal; retained availability and managed-identity pull access. Do not infer repository/tag names from the environment name. |
| Remote-build inputs | The API's `MOSAIC_PYTHON_INDEX_URL` and required registry/build access. Use an approved package source without credentials embedded in its URL or build arguments. |
| Hosting/configuration | App targets, ports, health paths, Always On/minimum running instances, selected non-secret settings, worker enablement/intervals, and intended changes. |
| Identity and browser configuration | API tenant/audience, separate runtime audience/model client, console and portal registrations/scopes/redirects, CORS, and application origins. |
| Backing services | Cosmos endpoint/database/container mappings; backend and API-only management vault references; monitoring configuration. Record secret references, never secret values. |
| Governance/operations | Existing grants and applied access, publication/run/lock owners, pricing effective dates and History, approved billing publications, budgets/blocks, worker state, backlog, and provider receipts. |

For the reference App Service container configuration, inspect one known app at a time:

```powershell
az webapp config show `
  --subscription "<existing-subscription-id>" `
  --resource-group "<existing-resource-group>" `
  --name "<existing-app-name>" `
  --query "{image:linuxFxVersion,alwaysOn:alwaysOn,managedIdentityPull:acrUseManagedIdentityCreds,healthCheckPath:healthCheckPath}" `
  --output json
```

Repeat for all three apps. The reference template writes a `DOCKER|...` image in `linuxFxVersion`;
the prefix is App Service configuration, not part of an image reference passed to azd. If the
actual host uses a different container schema or returns an unexpected value, resolve that
configuration before treating it as a rollback artifact.

Resolve each actual retained repository/tag against the existing registry:

```powershell
az acr repository show `
  --subscription "<existing-subscription-id>" `
  --name "<existing-acr-name>" `
  --image "<actual-image-repository>:<actual-image-tag>" `
  --query digest --output tsv
```

A configured tag and its **current** registry digest do not prove which digest a running instance
last pulled. Reconcile them with the last successful deployment/artifact record. Preserve a
retained reference whose digest you can verify again before rollback. A moved tag or deleted image
is not an exact rollback; rebuilding an old commit can also pick up changed base images.

Compare host settings through your protected configuration-management process. Do not dump
`azd env get-values`, all app settings, tokens, or credential-bearing output into this guide,
logs, or issue comments. In Azure, preserve `MOSAIC_ENVIRONMENT=azure`,
`MOSAIC_AUTH_MODE=entra`, and `MOSAIC_REPOSITORY_BACKEND=cosmos`; local authentication and in-memory
storage are not recovery substitutes.

### Check durable-state compatibility and backup prerequisites

Review changes to document shapes, enum/ownership semantics, tenant partitioning, container names,
TTL, price seeds, and settings. Do not assume additive fields make an old reader/writer safe.
There is no general schema-downgrade command or automatic production migration promised here.

The reference [Cosmos template](../infra/modules/cosmosdb.bicep) uses database `mosaic` and
`/tenantId` partitions:

| Container | Recovery consideration |
| --- | --- |
| `desired-state` | Governance intent and additional durable records, including coordination, approval/notification, and pricing state. Preserve record relationships and ownership, not just model definitions. |
| `sync-operations` | Sync/publish runs and recovery evidence. A restored desired-state record alone cannot establish what an external operation completed. |
| `audit-events` | Configured audit history; preserve audit records wherever the current writer stores them. Do not erase history to make an older image start. |
| `observed-state` | Disposable observed inventory can be rebuilt by sync, unlike administrator-authored intent. Rebuilding it is not proof of runtime access or reconciliation. |
| `usage-rollups` | Usage/history, budget state, and compatibility ownership/outboxes. Restore/reconcile consistently with governance, pricing, and delivery state. |

`desired-state` and `usage-rollups` must have `defaultTtl: -1`: only items with an explicit `ttl`
expire. A positive default can delete ordinary settings, grants, or monthly history. Check actual
container metadata, not an application setting or just `/readyz`. See
[notification retention](approvals-and-notifications.md#hosting-settings) and
[usage retention](usage-analytics.md#history-backfill-and-retention).

The template does not select an explicit Cosmos backup policy or supply a restore procedure.
Cosmos has automatic backups, but the account's actual backup mode, retention, restorable point,
restore permissions, and supported restore route must meet your recovery requirements. Record
the recovery owner, acceptable data loss/recovery objectives, a consistent scope for related
containers, and the network/RBAC/configuration work a restore would require. See Microsoft's
[backup and restore guidance](https://learn.microsoft.com/azure/cosmos-db/online-backup-and-restore).
Do not assume point-in-time restore is available for every account or that independent exports
form a consistent backup. If compatibility or necessary recovery prerequisites are unknown, stop.

### Review in-flight work and quiescence

Arrange an operator-controlled pause in new administrative writes, applies/unpublishes, approvals,
billing publication, and credential operations during the compatibility-sensitive window.
Review existing run/lock owners, syncs, pending billing retrievals, notifications, and budget work.
Prefer allowing known work to finish before changing the host.

Use existing controls deliberately, retaining their previous values for later resumption:

| Work | Existing control and limitation |
| --- | --- |
| Approval policies | Disable new policies and pause evaluation through `PUT /api/v1/approval-evaluator` with `{"paused":true}` where required. `MOSAIC_APPROVAL_EVALUATOR_ENABLED` controls the hosted loop. Pausing does not revoke already approved grants or apply their access plans. |
| Notification processing | `MOSAIC_NOTIFICATIONS_ENABLED` controls the new processing loops. Preserve delivery/provider IDs, unresolved outcomes, attempts, and budget-notice ownership. Follow the [feature rollback protocol](approvals-and-notifications.md#upgrade-operations-and-rollback). |
| Billing retrieval | `MOSAIC_BILLING_REFRESH_ENABLED` controls background polling/schedules, not interactive **Check status** or a provider job already submitted. Stop new interactive work too. Persisted runs/renewable leases coordinate ownership; cancellation or ownership loss does not mean an external query/export stopped. |
| Usage and budgets | `MOSAIC_USAGE_ROLLUP_ENABLED` and `MOSAIC_BUDGETS_ENABLED` control those loops. Suspending them delays freshness and enforcement; a block already written to APIM remains. Do not disable them casually or infer that existing calls are now unblocked. |

These host settings take effect in the application's configuration at startup; changing them can
restart it. They are **not a global maintenance switch** and do not necessarily interrupt admitted
work. Startup also resumes automatic-key recovery and gateway bootstrap, independently of those
loop switches. Exclude new control-plane actions and stop old writers where true quiescence is
required, across all relevant instances; do not assume one recycled instance proves this.

Publication write locks do not expire automatically. They are not renewable scheduler/billing
leases, and neither a timeout nor a restart authorizes deleting or stealing one. Before confirmed
recovery, stop the original worker and establish that **every ARM operation it submitted is
terminal**. `azd deploy --timeout` only stops azd waiting; it does not cancel Azure-side deployment.
See [interrupted-operation recovery](access.md#recovering-an-interrupted-operation).

## Deploy application images

Proceed only after target, artifact, compatibility, backup, and maintenance decisions are recorded.
For an application-only change, **skip provisioning and bootstrap**.

From the reviewed repository-root checkout, deploy the API first only if it can safely coexist
with the current frontends, or incompatible clients have been excluded for maintenance:

```powershell
azd deploy api --environment "<existing-environment>"
```

Check command success, the actual selected image, and API health/readiness before continuing.
Then update the administrator console:

```powershell
azd deploy web --environment "<existing-environment>"
```

Check its image, fresh runtime configuration, and authorized access, then update the portal:

```powershell
azd deploy portal --environment "<existing-environment>"
```

Run each step separately and stop on failure. Remote builds can incur ACR charges. A partial
rollout is not success: keep the maintenance boundary and choose a compatible forward completion
or [rollback/recovery](#roll-back-or-recover). Do not blindly retry while a deployment or writer
may still be active. A successful image deployment is not proof that workers, authentication, or
existing publications still work.

## When provisioning is required

Use this path only for reviewed infrastructure/configuration/identity changes, not as the default
image update. Review [`infra\main.bicep`](../infra/main.bicep), its parameters/modules,
[`azure.yaml`](../azure.yaml), and [`scripts\mosaic_entra.py`](../scripts/mosaic_entra.py).
Authorize the specific resource and permission effects before executing:

```powershell
azd provision --environment "<existing-environment>"
```

Provisioning invokes Entra hooks. They can create/update registrations and service principals,
assign the deploying user's initial Admin role, consent the model client, patch console/portal
redirects, and request the API identity's Graph permissions. Bicep can change resources, managed
identities, Azure role assignments, diagnostics, container settings, and Cosmos TTL. A provision
preview must not be assumed to be a hook-free/read-only check.

Review preserved identity IDs and optional behavior as well as additions. For example, leaving
model-client bootstrap enabled can grant its consent again on the next provision. Optional
group-write permissions need separate deliberate consent; MOSAIC Admin or subscription access
is not that consent. Follow [deployment identity guidance](deployment.md#deploy-with-azd) and
[optional group-management setup](deployment.md#optional-cost-center-group-management), not an
automatic permission escalation to fix an upgrade.

The template supplies **placeholder images** and application settings. Do not assume provisioning
preserves the currently deployed image or every operator-managed setting. After the reviewed
provisioning work, deploy the real API, console, and portal images using the preceding sequence,
reconcile configuration with the change record, and complete verification. Do not leave a healthy
placeholder site mistaken for MOSAIC.

Do not use `azd up` as an unexplained combined update, or `azd down`/environment recreation as
recovery. Reapplying old Bicep wholesale is not a safe infrastructure rollback.

## Verify the upgrade

Record pass, fail, **unverified**, or **not enabled**, with the expected and observed evidence for
each check. Do not equate missing evidence with success.

Start with bounded, unauthenticated liveness/readiness probes:

```powershell
Invoke-RestMethod -Uri "https://<api-host>/healthz" -TimeoutSec 20
Invoke-RestMethod -Uri "https://<api-host>/readyz" -TimeoutSec 20
Invoke-RestMethod -Uri "https://<console-host>/healthz" -TimeoutSec 20
Invoke-RestMethod -Uri "https://<portal-host>/healthz" -TimeoutSec 20
```

Expected results are `{"status":"ok"}` for liveness and `{"status":"ready"}` for API readiness;
API repository-readiness failure returns HTTP 503. These probes do not prove version, auth,
retention, hosting, delivery, or inference.

| Check | Expected evidence |
| --- | --- |
| Three applications | Each intended image/digest is deployed. Console and portal root/deep links load the current assets. Neither app is a placeholder or serving stale HTML. |
| Authentication/authorization | An existing authorized Admin reaches the console; a User-only session cannot administer. An existing User reaches the portal. API protected routes reject unauthorized access. Do not create a role assignment simply to make this check pass. |
| Browser/API configuration | Inspect each frontend's fresh `/config.js`: `authMode` is `entra`, and `apiBaseUrl`, `entraTenantId`, `entraClientId`, and `entraApiScope` match its host's `MOSAIC_API_BASE_URL` and `MOSAIC_ENTRA_*` settings. Verify API audience/tenant, console/portal redirects and CORS, API `MOSAIC_FRONTEND_URL`/`MOSAIC_PORTAL_URL` origins, separate model-runtime audience/client, vault references, and Cosmos mappings against the approved record. Do not substitute the control-plane audience for APIM runtime tokens. |
| Cache behavior | HTML/deep links revalidate with `Cache-Control: no-cache`, `/config.js` has `no-store`, and content-hashed `/assets/` files remain immutable. A loaded old tab may need a fresh navigation; a runtime setting alone does not update its already-loaded JavaScript. |
| Storage/TTL | Repository readiness succeeds; actual `desired-state` and `usage-rollups` metadata shows item-only expiry. Ordinary desired state and retained history remain readable. |
| Notifications | Read `GET /api/v1/notifications/health`: enabled worker heartbeats are current without reported processing errors; inspect delivery health/backlog, unknown sends, and dead letters. Read-only readiness does not send a test message or prove receipt. |
| Other enabled workers | Read `GET /api/v1/approval-evaluator` and `GET /api/v1/pricing/billing/scheduler`; check intended pause/enabled state, successful activity, errors and in-flight work. Check [rollup freshness/results](usage-analytics.md) and [budgets/blocks](budgets-and-alerts.md). Do not expect every loop to expose the same heartbeat. |
| Hosting | Verify actual App Service Always On/minimum running-instance configuration and successful worker evidence. `MOSAIC_SCHEDULED_WORKERS_ALWAYS_ON=true` is only a declaration, not proof the API stays awake. |
| Pricing/history | Check representative overrides and past/current/future effective dates in [History](pricing.md#adding-and-correcting-prices), approved billing publications, expected unpriced usage, and report/budget effects of a new seed. Approved billing rates must have complete committed version membership; drafts must remain drafts. Do not publish a price to mask a verification failure. |
| Existing APIM access configuration | Compare model/MCP/pool publication ownership, applied snapshots, grants, connection details, pending/failed/unknown operations, and read-only gateway configuration with the baseline or approved changes. A MOSAIC deployment is not an access Apply; do not republish every offering automatically. Metadata alone does not prove a successful runtime call. |

Seed releases can supersede earlier manual overrides; review them after upgrading as
[Pricing](pricing.md#adding-and-correcting-prices) describes. Billing acquisition creates drafts
only. Approved rates use immutable effective-dated versions and one committed publication marker;
partial preparation must never appear as an approved price list. Backdating or seed/calculation
changes can change reported spend and budget decisions. Missing/unpriced evidence is not zero cost.

Resume only the intended controls once the corresponding checks pass. Verify renewed successful
worker activity, backlog, unresolved receipts, and budget blocks again. Configuration alone is
not proof that work resumed or an external provider delivered anything.

### Optional runtime smoke tests

Keep paid inference and external test messages **out of the default upgrade checks**. Continued
real runtime access remains unverified unless separately tested. Before any such test, approve a
specific non-production offering and existing identity, accepted auth method, finite number of
requests/retries, time limit, token/output limit, and spend allowance. Do not enable a grant,
issue/reveal/rotate a key, consent a client, or obtain credentials as an implicit upgrade step.

For an inference-only smoke, use the
[caller request shape](call-models-with-entra-tokens.md#call-the-model) with an existing approved
runtime token and one approved authentication method. Set an explicit supported output/token
limit and the approved request/time bounds before sending; do not include token acquisition or
optional key steps from the caller guide.

The [model verifier](verification.md#verifying-a-real-gateway) is **not inference-only**. For
selected key-enabled user/application grants, it can create missing keys and reveals primary
keys. It also sends denial probes and exercises every enabled authentication method. Running it
requires explicit approval for those credential operations, probes, and all methods exercised,
in addition to the overall request/time/token/spend bounds. Include the denial probes in that
budget; permission for bounded inference alone does not authorize these operations.

The verifier requires `--send-model-requests` and defaults to at most 8 output tokens **per call**,
not a total-cost or total-request limit. Establish its exact request count for the selected
grants/methods before running it; do not invent a request-limit flag or run it broadly. Exclude
quota/limit proofs, watch/revocation flows, and model-calling MCP tools from a routine smoke.
Even retrieval/diagnostic queries can incur cloud charges.

Use existing approved authorization and record the actual result without tokens, keys, or model
output. A model grant is independent of an MCP grant. If target, credentials, runtime support, or
bounds are unavailable, record **not run**, not passed.

## Roll back or recover

**A previous image does not restore previous Cosmos contents, prices, grants, APIM policies,
Entra relationships, app settings, or provider receipts.** Choose the recovery class first:

| Situation | Action |
| --- | --- |
| Application regression, retained artifacts, old readers/writers proven compatible | Quiesce relevant work, verify retained image references/digests, then redeploy the compatible application set. Recheck configuration, pricing and runtime state before resuming. |
| Infrastructure/configuration/identity regression | Review and revert only the identified compatible deltas through approved tooling. Do not remove consent indiscriminately or redeploy an old template wholesale. |
| Interrupted apply or retained/unknown ownership | Diagnose the exact owner; stop its worker and confirm all submitted ARM operations terminal before explicit recovery. Preserve denial and ownership evidence, then review a fresh plan. |
| New persisted semantics not understood by the old build | Do not downgrade. Keep compatible recovery code or use a forward fix; prepare explicit manual/state recovery if necessary. Starting successfully is not compatibility evidence. |
| Corrupted state or a necessary restore | Use a separately approved, account-specific recovery procedure and reconcile Cosmos with current external state before allowing writes. Restoring a backup is not an application rollback. |

### Reuse compatible retained images

First confirm old/new document, policy/compiler, frontend/API, and worker-ownership compatibility.
Preserve the current failure evidence and change record; do not delete newer records to force an
old reader to accept them. Resolve each retained tag's digest again and compare it with the
pre-upgrade artifact record.

With the same maintenance/compatibility gates as an upgrade, redeploy one service at a time and
check each outcome before continuing:

```powershell
azd deploy api --environment "<existing-environment>" --from-package "<retained-api-image-reference>"
```

```powershell
azd deploy web --environment "<existing-environment>" --from-package "<retained-web-image-reference>"
```

```powershell
azd deploy portal --environment "<existing-environment>" --from-package "<retained-portal-image-reference>"
```

Each placeholder is the **actual retained, fully qualified image tag**, verified against its
recorded digest, not a constructed version name or `latest`. The CLI help documents this image-tag
form; digest-reference deployment is not established by this guide. If a tag moved, the image was
deleted, pull access is missing, or compatibility is uncertain, stop rather than selecting a
different artifact silently. Rebuilding old source requires a new reviewed artifact decision.

Restore only compatible configuration deltas from protected records. An image rollback does not
restore runtime `config.js` values independently of host settings. Repeat the full applicable
[verification checklist](#verify-the-upgrade), including pricing/history and budget effects of
the older bundled seed, before declaring recovery complete.

### Preserve ownership and handle non-downgradable state

Follow [approval/notification rollback](approvals-and-notifications.md#upgrade-operations-and-rollback):
disable new approval policies, pause evaluation, and stop new notification processing first.
Preserve requests, grants, audit, attempts, provider IDs, and receipts. Before any downgrade to an
older inline budget sender, disable legacy budget email and keep it disabled until queued/unknown
notices and send ownership have been reconciled. Stopping only the new worker cannot prevent an
older sender from replaying notices.

An older price reader must understand committed billing publication markers and their **exact
immutable price-version membership**. Do not downgrade to a reader that exposes orphaned prepared
versions or hides committed history. Price versions are not edited/deleted to undo an approval:
corrections require reviewed effective-dated versions. Newer seed/manual/billing history and its
budget consequences need review even if no restore occurs. See
[billing recovery](pricing.md#schedules-late-data-and-recovery) and
[price history](pricing.md#adding-and-correcting-prices).

For a retained publication lock, begin with diagnostics in
[interrupted-operation recovery](access.md#recovering-an-interrupted-operation). Name the exact
returned owner, including a retained metadata-mutation owner where applicable. Diagnostic recovery
with `confirmQuiesced:false` does not release the lock. Only an operator who stopped the original
worker and confirmed every submitted ARM operation terminal may attest `confirmQuiesced:true`.
Confirmed recovery may deliberately deny access; failed safe-state establishment remains
unknown/locked. Refetch the publication and review a fresh plan before restoring access. The same
ownership boundary applies to [pools](model-pools.md#publishing-and-ownership) and
[MCP publications](publish-mcp-servers.md); never infer ownership from a resource's name.

Earlier internal-managed-group cost centers have an explicit
[old-MVP reconfiguration boundary](development.md#cost-center-development-and-old-mvp-data), not a
production migration or silent member copy. An old image cannot undo real Entra membership edits
or copied keys. Recreating records, moving charges to General, or deleting data is not rollback.

### Restore state only through a separate recovery procedure

If forward recovery cannot repair corrupted state, use the actual account's supported backup/
restore route with separate approval for its resource, data, permission, and availability effects.
Preserve current records/evidence before a restore, select a consistent recovery scope/point, and
record what writes or history would be lost. Some restore paths require another account plus
endpoint, network, and RBAC changes; do not assume a universal in-place rewind.

Before admitting writers or restoring runtime access, reconcile recovered intent, operation
owners/locks, grants and revocations, prices, audit/history, budget/usage state, and notification
receipts with **current** APIM policies/subscriptions, Entra relationships, Key Vault references,
and provider outcomes. A backup can predate a revocation while APIM or Entra has already changed;
blindly replaying it can broaden access or duplicate delivery. Expired operational records or
discarded raw billing uploads are not promised recovery material.

There is no generic MOSAIC restore command here. If the compatible reader/writer or reconciliation
procedure is unavailable, keep the affected state isolated and escalate for explicit manual
recovery rather than claiming that a successful container start made it safe.

## Evidence and unverified exercises

**No live upgrade, image rollback, identity change, runtime smoke, or Cosmos restore has been
executed to validate this runbook.** Source/CLI review is not production evidence. For a separately
authorized non-production exercise, keep this record in protected operational records:

| Exercise | Required evidence | Status for this guide |
| --- | --- | --- |
| Target/artifact/backup readiness | Existing-target match, source range, retained three-image set/digests, compatibility decision, actual backup/restore prerequisites. | Unverified live |
| Application-only update | Successful remote build/deploy for API, console, and portal, expected images/configuration, and the applicable verification results above. | Unverified live |
| Provisioning, if required | Reviewed hook/resource/permission effects match intent; real application images and intended configuration follow provisioning. | Unverified live |
| Worker and access continuity | Actual host/worker evidence, preserved pricing/history and ownership, existing APIM configuration; real runtime success only if explicitly exercised. | Unverified live |
| Compatible image rollback or interrupted-operation recovery | Retained-artifact deployment or exact-owner recovery after proven quiescence, reconciled state, and no unsafe grant/notification replay. | Unverified live |
| Cosmos restore, only if required | Actual account-specific restore, data-loss/recovery outcome, repaired endpoint/network/RBAC configuration, and external-state reconciliation. | Unverified live |
| Optional paid/external smoke | Separate approval, finite request/retry/token/time/spend bounds, actual result, and required cleanup. Otherwise record not run. | Not run |

Record target/revisions/artifacts, expected versus actual results, timestamps, redacted failure
evidence, recovery decisions, and the operator responsible. Mark unavailable checks unverified or
not enabled; do not promote a partly exercised journey to a tested production guarantee.
