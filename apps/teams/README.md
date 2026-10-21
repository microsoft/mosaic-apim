# Optional MOSAIC Teams integration

This package is a **personal-only, one-way notification app**, not a conversational assistant.
It has no approval, command, file, tool, or shared-channel capability. All links reopen
authenticated MOSAIC. The shared-channel transport is separate and does not need this app.
All external sends and tenant changes below are explicit operator actions, not automated tests.
Start with [Notification setup](../../docs/notification-setup.md), which covers Graph email too.
The administrator console groups all three paths under **Settings > Notifications**; an end
user's private setup and preferences remain self-scoped in the portal.

## Shared channels: Teams Workflows

1. In the chosen Teams channel, create a Workflow using the **When a Teams webhook request is
   received** trigger and an action that posts its Adaptive Card attachments to that channel.
   Assign co-owners: a Workflow belongs to its owners, not to MOSAIC or the channel. Check the
   organization's Power Automate licensing, DLP, channel support, ownership and tenant policies.
   Microsoft's current documentation supports flow-bot posts in **shared channels**; posting
   as a flow bot in private channels remains a separate provider limitation.
2. This adapter supports the trigger's **Anyone** (secret URL) mode, not an Entra-protected
   trigger requiring an additional access token. Treat the full URL as a credential. Legacy
   Office 365 connector URLs are not supported. The Microsoft-hosted Workflows shapes supported
   are `/workflows/{id}/triggers/{trigger}/paths/invoke` on `*.logic.azure.com` and
   `/powerautomate/automations/direct/workflows/{id}/triggers/{trigger}/paths/invoke` on
   `*.api.powerplatform.com`. Other clouds/shapes fail closed until explicitly implemented.
3. The deployment operator must allowlist the **exact hostname** in the API's
   `MOSAIC_TEAMS_WORKFLOW_ALLOWED_HOSTS` JSON array, for example
   `["fictional.westus.logic.azure.com"]`. `infra/main.parameters.json` passes the same azd
   environment setting to Bicep. No wildcard configuration,
   request-supplied allowlist, arbitrary URL, redirect, or legacy connector is accepted.
   Apply outbound network controls as defense in depth. TLS verification must remain enabled.
4. Set `MOSAIC_KEY_VAULT_URI` to MOSAIC's existing vault and `MOSAIC_FRONTEND_URL` to the
   HTTPS console origin. The application host wires the Key Vault reader and writer. Its identity
   needs secret read plus write/delete in its vault (Secrets Officer includes both; apply least
   privilege at the appropriate scope). No new secret store is created. POST a destination to
   `/api/v1/settings/teams/destinations` with name, enabled, allowedCategories and webhookUrl.
   The URL is write-only. Cosmos and responses contain only its Key Vault reference.
   Update via PUT; omit webhookUrl to retain the credential. Rotation creates a new secret
   before the conditional repository update; old/uncommitted secrets are deleted best effort.
   Monitor/clean orphaned `mosaic-teams-*` secrets if vault deletion is unavailable.
5. Configure an enabled shared notification route referencing that destination and explicitly
   enable the tenant `teamsChannel` notification channel. Destination category restrictions
   are rechecked by the adapter. Shared routes are **admin audience only**; the common delivery
   authorizer must enforce audience, route eligibility and current visibility before sending.
6. In **Notifications > Shared Teams**, select the destination and choose **Check access**.
   The read-only GET `/api/v1/settings/teams/destinations/{id}/checks` actually reads its secret,
   validates the supported URL and exact host, and checks channel/route compatibility. It
   distinguishes missing reader, denied vault access, missing secret, vault unavailability and
   disallowed endpoint, without revealing the secret or calling the Workflow.
7. Choose **Send test**, or explicitly POST
   `/api/v1/settings/teams/destinations/{id}/test`. A 2xx from Microsoft means
   **provider accepted**, not Workflow execution, channel delivery, or a read receipt.
   Check Workflow run history and Teams manually. Workflow HTTP errors/bodies are never returned
   or persisted, and redirects are never followed. Last-test evidence persists with the
   destination configuration and current secret fingerprint. Changed setup or secret rotation
   invalidates old proof; a recent accepted test is still not a downstream receipt.
   Queued transient and uncertain sends retry automatically within normal finite limits;
   duplicate messages are acceptable. An explicit synthetic test itself makes one send attempt.

If the tenant prohibits **Anyone** triggers, stop: this adapter cannot authenticate an
Entra-protected Workflow. Do not weaken tenant policy or paste a different connector URL.

The Workflow owner also needs an authorized **Microsoft Teams connection** in the selected
Power Automate environment. A Teams license, Azure CLI session, or bot managed identity does
not create that connection. The owner signs in to Power Automate, opens **Connections**, and
adds or repairs the Microsoft Teams connection before completing the Workflow template.
See [Microsoft's connection setup steps](https://learn.microsoft.com/en-us/power-automate/add-manage-connections).
An empty connection list is scoped to the signed-in operator and environment; it does not
prove that no other owner has a connection. MOSAIC's read-only checks cannot authorize this
connection or sign in on an owner's behalf.

The transport suppresses automatic OpenTelemetry HTTP instrumentation for secret Workflow
URLs and private conversation sends, and redacts Teams URLs in standard httpx INFO logs.
Do not enable HTTP wire/debug logging, log webhook request bodies, OAuth bodies, callback
bodies, or provider response bodies. Configure ingress/custom APM and HTTP event hooks to
exclude/redact these secrets as well.

## Private messages: optional single-tenant Azure Bot

1. A tenant administrator creates an **Azure Bot** using either a **user-assigned managed
   identity** or a **single-tenant Entra application** (`AzureADMyOrg`) and enables its
   Microsoft Teams channel. Multi-tenant bots, emulator/skill authentication, sovereign-cloud
   registrations, arbitrary UPN recipients and Graph-created conversations are not supported
   by this implementation. No cloud deployment or real connectivity is implied by saving settings.
2. Host the API with HTTPS and set the Azure Bot messaging endpoint to the actual
   `https://<api-host>/api/v1/teams/messages`. Route this path to the API, not a frontend SPA.
   Exempt only this route from a reverse proxy's *MOSAIC user-login* requirement; do not bypass
   its Connector JWT validation. It accepts only Microsoft's RS256 signature from fixed Bot
   Connector OpenID keys, `https://api.botframework.com` issuer, exact bot audience, expiry/nbf,
   `msteams` key endorsement, and signed `serviceurl`. The authenticated activity must have
   both tenant identifiers equal to the configured tenant, a personal conversation, and the
   expected bot recipient. No anonymous or header-only "authenticated" lifecycle is accepted.
3. Configure outbound OAuth explicitly using the deployment settings below:
   - Managed identity: set `MOSAIC_TEAMS_BOT_AUTH_MODE=managedIdentity`,
     `MOSAIC_TEAMS_BOT_APP_ID` to the exact bot UAMI's **client ID**, and
     `MOSAIC_TEAMS_BOT_MANAGED_IDENTITY_RESOURCE_ID` to its Azure resource ID before
     provisioning. Bicep attaches that existing identity to the API host; the runtime uses
     `ManagedIdentityCredential(client_id=bot_app_id)`. Register the same UAMI on Azure Bot.
     This is separate from the API system-assigned identity used for Graph email.
   - Secret reference: set `MOSAIC_TEAMS_BOT_AUTH_MODE=clientSecret`, the single-tenant app's
     client ID in `MOSAIC_TEAMS_BOT_APP_ID`, and an HTTPS Key Vault secret URI in
     `MOSAIC_TEAMS_BOT_SECRET_REFERENCE`. Put the Entra app credential in Key Vault outside
     MOSAIC and grant the API identity read access. The adapter obtains an actual
     single-tenant client-credentials token for `https://api.botframework.com/.default`.
     No literal bot secrets are stored in settings, environment examples or the manifest.
     Configure rotation/expiry monitoring. Token acquisition failure is a real failed send,
     never a successful stub. Missing, denied, expired/rejected, and transient token-service
     failures have separate safe corrective guidance. Secret references may pin a version;
     recheck access after any out-of-band credential rotation.
4. Build the ZIP locally with the existing Python/Pillow dependencies:

   ```powershell
   python apps\teams\build_package.py `
     --app-id <teams-catalog-uuid> --bot-app-id <bot-client-uuid> `
     --website https://mosaic.example.invalid `
     --privacy https://mosaic.example.invalid/privacy `
     --terms https://mosaic.example.invalid/terms
   ```

   Supply operator-published privacy/terms URLs, not the example addresses. The script creates
   `apps\teams\dist\mosaic.zip` containing a customized manifest and original 192x192 color/
   32x32 transparent white outline icons. It performs no cloud changes. Validate the package
   in Teams Developer Portal/tenant app validation before publishing it to the tenant catalog.
   The template uses Microsoft's versioned 1.22 schema; packaging does not certify tenant
   policy approval or Marketplace readiness.
   A default Microsoft sign-in footer is not the operator's MOSAIC app policy, and a frontend
   SPA returning HTTP 200 for an invented `/privacy` path does not publish a policy page.
5. Publish/upload and allow the app through the organization's Teams app permission/setup
   policies. Do not request tenant-wide Graph installation permissions merely for notifications.
   Configure `/api/v1/settings/teams/personal` with enabled, appId (catalog UUID), botAppId
   (OAuth client UUID), and cloud `public`. Enabling requires the exact OAuth source to be
   configured on the API host. Separately enable the tenant `teamsPersonal` channel.
6. A person installs MOSAIC **in personal scope**. Only authenticated installation or
   bot-membership lifecycle events can bind the activity's Entra object ID to its personal
   conversation; portal callers cannot set conversation IDs. Fresh timestamps and conditional
   repository writes fence stale lifecycle updates. An active binding cannot silently switch
   conversations; uninstall the old app before reinstalling in a different conversation.
7. The person explicitly opts in to Teams in their own notification preferences. GET
   `/api/v1/me/notifications/teams` shows only that caller's binding state, without its internal
   conversation ID or service URL. **Check access**, GET
   `/api/v1/me/notifications/teams/checks`, acquires an actual runtime Bot Connector token
   without sending or installing anything. Registration, authenticated installation and
   category opt-in are reported separately, with instructions and the responsible administrator.
   POST `/api/v1/me/notifications/teams/test` explicitly sends
   a harmless test to that caller only. Then confirm the actual message in Teams. A Connector
   HTTP success is **provider acceptance only**, not confirmed delivery.
8. Authenticated removal disables the binding. Connector 403/404/410 marks it blocked and
   suppresses further personal sends until a fresh install. There is **no shared-channel
   fallback**. A bad OAuth registration may also cause 403; correct it and reinstall/test.
   Email/in-app remain independent, according to the person's choices.
   A configured bot registration still authenticates lifecycle events while personal delivery
   is disabled, so an uninstall cannot leave an incorrectly active binding on re-enable.

The console also has administrator-only self-scoped aliases
`/api/v1/notifications/me/teams`, `/api/v1/notifications/me/teams/checks` and
`/api/v1/notifications/me/teams/test`. These always use the authenticated administrator's
binding and console links, never a portal recipient. Last-test results persist by tenant,
caller and persona and are invalidated by registration or authenticated binding changes.
Only an accepted test less than five minutes old is marked recently verified.

If an operator's Graph catalog or installation query fails with **Missing scope permissions**,
see [operator access versus bot access](../../docs/notification-setup.md#operator-access-is-separate-from-bot-access).
Azure access and an administrator directory role do not grant every Graph scope to the
operator's client. Use an authorized Teams administration/personal installation path rather
than grant catalog, installation, or broad directory permissions to the runtime bot.

### Managed-identity resource creation example

After the resource owner approves the changes, an Azure operator can create the dedicated
identity, F0 Azure Bot, and Teams channel with these commands. Inspect the target resource group
first and choose unused, approved names; do not update an unrelated bot or identity. This creates
persistent resources, not an access check. It does not create a Teams channel in a team, publish
the personal app, attach the identity to the API, opt anyone in, or send a message.

```powershell
$SubscriptionId = '<approved-subscription-id>'
$TenantId = '<approved-tenant-id>'
$ResourceGroup = '<existing-MOSAIC-resource-group>'
$IdentityName = '<dedicated-bot-identity-name>'
$BotName = '<dedicated-bot-resource-name>'
$MessagingEndpoint = 'https://<api-host>/api/v1/teams/messages'

$Account = az account show --subscription $SubscriptionId --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $Account.tenantId -ne $TenantId) {
  throw 'Stop: the subscription does not belong to the approved tenant.'
}

az identity create --subscription $SubscriptionId --resource-group $ResourceGroup `
  --name $IdentityName --output none
if ($LASTEXITCODE -ne 0) { throw 'Managed identity creation failed.' }

$Identity = az identity show --subscription $SubscriptionId --resource-group $ResourceGroup `
  --name $IdentityName --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or -not $Identity.clientId -or -not $Identity.id) {
  throw 'Stop: the dedicated identity could not be resolved.'
}

az bot create --subscription $SubscriptionId --resource-group $ResourceGroup `
  --name $BotName --app-type UserAssignedMSI --appid $Identity.clientId `
  --tenant-id $TenantId --msi-resource-id $Identity.id `
  --endpoint $MessagingEndpoint --sku F0 --output none
if ($LASTEXITCODE -ne 0) { throw 'Azure Bot creation failed.' }

az bot msteams create --subscription $SubscriptionId --resource-group $ResourceGroup `
  --name $BotName --output none
if ($LASTEXITCODE -ne 0) { throw 'Bot Teams channel creation failed.' }
```

Pass `$Identity.clientId` and `$Identity.id` through the deployment settings below and use the
approved API deployment process. The existing Bicep attaches that identity while preserving the
API's system-assigned identity for email. The bot does not need a subscription Contributor role
or runtime Teams catalog/installation permissions. Do not enable calling for this one-way app.
F0 bot provisioning does not provide a Teams or Exchange license. Finish package approval,
personal installation, opt-in, read-only runtime checks, and explicit receipt verification
separately; resource creation alone proves none of them.

## Deployment settings

| API or azd setting | Purpose |
| --- | --- |
| `MOSAIC_TEAMS_WORKFLOW_ALLOWED_HOSTS` | JSON array of exact Microsoft Workflow hosts; default `[]` fails closed. |
| `MOSAIC_KEY_VAULT_URI` | Existing runtime vault for write-only shared Workflow credentials and, when selected, the bot secret reference. |
| `MOSAIC_TEAMS_BOT_AUTH_MODE` | `disabled` (default), `managedIdentity`, or `clientSecret`. |
| `MOSAIC_TEAMS_BOT_APP_ID` | Exact bot OAuth client ID, not the Teams catalog app ID or identity object ID. |
| `MOSAIC_TEAMS_BOT_MANAGED_IDENTITY_RESOURCE_ID` | azd/Bicep input attaching an existing bot UAMI to the API host; required for managed-identity deployment. |
| `MOSAIC_TEAMS_BOT_SECRET_REFERENCE` | Key Vault HTTPS secret URI for `clientSecret`, never a literal credential. |
| `MOSAIC_FRONTEND_URL`, `MOSAIC_PORTAL_URL` | HTTPS console and portal origins used for authenticated notification links. |
| `MOSAIC_SCHEDULED_WORKERS_ALWAYS_ON` | Set truthfully after arranging an awake API host; a flag alone does not establish hosting. |

Bicep configures and attaches existing resources; it does not create an Azure Bot, approve a
Teams app, create a Workflow, enable MOSAIC channels or routes, install a personal app, or opt
anyone in. These require their respective operators. A disabled local host can keep its real
`local-development` tenant and missing origins without invented production values.

## Host/worker integration contract

`mosaic_api.main` composes the bounded TLS-verifying HTTP client, existing repository,
Key Vault access, exact-host policy, selected OAuth source and both digest-wrapped dispatchers,
then closes clients/credentials on shutdown. Personal delivery requires a real tenant UUID,
matching bot OAuth, and HTTPS console **and** portal origins; shared destinations require an
HTTPS console origin. Saved settings are rechecked at send time.
Both dispatchers implement `NotificationDispatcher`, with
`supports_idempotency = False`. `recipient_reference` is the raw destination ID for shared
sends, or the authenticated user's object ID for personal sends. The engine must recheck
preferences/audience/visibility through its existing authorizer; these adapters do not replace
that authorization layer. The public personal test is explicitly self-scoped and does not
accept any recipient or message body.

Composed notification health includes tenant and self-scoped prerequisite checks. The detailed
destination and personal check endpoints above distinguish read-only access evidence from
explicit-send history. No diagnostic grants permissions, installs an app or claims receipt.
Disable settings/routes/channels to roll back; no APIM apply or gateway changes are required.
Cards show at most ten summaries and three subject links, always retaining inbox/preferences
links. A conservative 24 KiB UTF-8 payload bound is enforced before any provider request;
oversized cards fail visibly rather than looping on the provider's message-size limit.
Administrator cards use structured subject metadata to open the exact historical model/MCP/
pool publication run or model/MCP endpoint/gateway sync run, with each ID percent-encoded.
Gateway historical sync selection requires `integration="gatewayInventory"`; incomplete
legacy gateway-apply metadata opens inventory without mislabeling an apply run as a sync run.
Personal portal cards never link to administrator diagnostics; their subject links remain
limited to the person's authenticated request/access views.

## Protocol and lifecycle references

- [Teams Workflows incoming webhook setup](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/how-to/add-incoming-webhook)
- [Teams proactive messages and installation requirements](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)
- [Connector REST authentication requirements](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication?view=azure-bot-service-4.0)
- [Teams app manifest schema](https://learn.microsoft.com/en-us/microsoftteams/platform/resources/schema/manifest-schema)

This adapter uses the documented REST protocol, not the retired Bot Framework SDK. Tenant
installation, Azure Bot registration, Workflow run/receipt, outbound OAuth and real message
delivery require operator-run staging checks; offline unit tests do not verify those systems.
