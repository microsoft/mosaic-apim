# Optional MOSAIC Teams integration

This package is a **personal-only, one-way notification app**, not a conversational assistant.
It has no approval, command, file, tool, or shared-channel capability. All links reopen
authenticated MOSAIC. The shared-channel transport is separate and does not need this app.
All external sends and tenant changes below are explicit operator actions, not automated tests.

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
3. The deployment operator must allowlist the **exact hostname** using
   `WorkflowEndpointPolicy(allowed_hosts=frozenset({...}))`. No wildcard configuration,
   request-supplied allowlist, arbitrary URL, redirect, or legacy connector is accepted.
   Apply outbound network controls as defense in depth. TLS verification must remain enabled.
4. Inject the existing `KeyVaultSecretReader` and `KeyVaultSecretWriter`. The MOSAIC identity
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
6. Explicitly POST `/api/v1/settings/teams/destinations/{id}/test`. A 2xx from Microsoft means
   **provider accepted**, not Workflow execution, channel delivery, or a read receipt.
   Check Workflow run history and Teams manually. Workflow HTTP errors/bodies are never returned
   or persisted, and redirects are never followed. Ambiguous outcomes are not automatically
   retried as if exactly-once delivery existed.

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
3. Configure outbound OAuth explicitly:
   - Managed identity: inject `ManagedIdentityBotTokenSource(credential, bot_app_id=...)`.
     `credential` must be `azure.identity.aio.ManagedIdentityCredential(client_id=bot_app_id)`
     for the exact user-assigned identity registered on Azure Bot and assigned to the host.
     Do not silently reuse a system-assigned/default credential with a different identity.
   - Secret reference: inject `SecretBotTokenSource(client, reader, tenant_id=...,
     bot_app_id=..., secret_reference="https://<vault>.vault.azure.net/secrets/<name>")`.
     Put the Entra app credential in Key Vault outside MOSAIC. The adapter obtains an actual
     single-tenant client-credentials token for `https://api.botframework.com/.default`.
     No literal bot secrets are stored in settings, environment examples or the manifest.
     Configure rotation/expiry monitoring. Token acquisition failure is a real failed send,
     never a successful stub.
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
5. Publish/upload and allow the app through the organization's Teams app permission/setup
   policies. Do not request tenant-wide Graph installation permissions merely for notifications.
   Configure `/api/v1/settings/teams/personal` with enabled, appId (catalog UUID), botAppId
   (OAuth client UUID), and cloud `public`. Enabling requires the exact OAuth source to be
   injected. Separately enable the tenant `teamsPersonal` channel.
6. A person installs MOSAIC **in personal scope**. Only authenticated installation or
   bot-membership lifecycle events can bind the activity's Entra object ID to its personal
   conversation; portal callers cannot set conversation IDs. Fresh timestamps and conditional
   repository writes fence stale lifecycle updates. An active binding cannot silently switch
   conversations; uninstall the old app before reinstalling in a different conversation.
7. The person explicitly opts in to Teams in their own notification preferences. GET
   `/api/v1/me/notifications/teams` shows only that caller's binding state, without its internal
   conversation ID or service URL. POST `/api/v1/me/notifications/teams/test` explicitly sends
   a harmless test to that caller only. Then confirm the actual message in Teams. A Connector
   HTTP success is **provider acceptance only**, not confirmed delivery.
8. Authenticated removal disables the binding. Connector 403/404/410 marks it blocked and
   suppresses further personal sends until a fresh install. There is **no shared-channel
   fallback**. A bad OAuth registration may also cause 403; correct it and reinstall/test.
   Email/in-app remain independent, according to the person's choices.
   A configured bot registration still authenticates lifecycle events while personal delivery
   is disabled, so an uninstall cannot leave an incorrectly active binding on re-enable.

The console also has administrator-only self-scoped aliases
`/api/v1/notifications/me/teams` and `/api/v1/notifications/me/teams/test`. These always use
the authenticated administrator's binding and console links, never a portal recipient.

## Host/worker integration contract

The foundation deliberately does not edit the application host. The host provides a bounded,
TLS-verifying `httpx.AsyncClient(timeout=...)` (closed on shutdown), the existing notification
repository and available Key Vault instances. Disabled/unconfigured local hosts can pass
their actual `local-development` tenant, `console_origin=None`, `portal_origin=None`, and no
OAuth source or vault dependencies: no fake UUID or production URL is required.
Enabling personal delivery requires a real tenant UUID, matching bot OAuth, and HTTPS console
**and** portal origins. Enabling shared destinations requires an HTTPS console origin.
These conditions are rechecked before sending previously saved settings too.

```python
service = TeamsService(
    notification_repository,
    tenant_id=tenant_id,
    secret_reader=key_vault_reader,
    secret_writer=key_vault_writer,
    transport=TeamsHttpTransport(client, WorkflowEndpointPolicy(frozenset(exact_hosts))),
    authenticator=TeamsLifecycleAuthenticator(client),
    token_source=bot_token_source_or_none,
    console_origin=console_origin,
    portal_origin=portal_origin,
)
app.state.teams_service = service
app.include_router(teams_router)
# Merge into the notification worker's existing dispatcher map:
dispatchers["teamsChannel"] = TeamsChannelDispatcher(service)
dispatchers["teamsPersonal"] = TeamsPersonalDispatcher(service)
```

Imports are from `mosaic_api.integrations.teams`, `mosaic_api.services.teams`, and
`mosaic_api.teams_api`. Both dispatchers implement `NotificationDispatcher`, with
`supports_idempotency = False`. `recipient_reference` is the raw destination ID for shared
sends, or the authenticated user's object ID for personal sends. The engine must recheck
preferences/audience/visibility through its existing authorizer; these adapters do not replace
that authorization layer. The public personal test is explicitly self-scoped and does not
accept any recipient or message body.

For composed health use `await service.capability_checks(tenant_id)` administratively and
`await service.capability_checks(tenant_id, user_object_id=auth.object_id)` for a person's
health. These probes do not send messages or claim saved configuration is verified.
This module does not register `/notifications/health` or `/me/notifications/health`.
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
