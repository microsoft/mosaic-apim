# Notification setup and access checks

Open **Settings > Notifications** in the administrator console. Configure an integration, run
**Check access**, then explicitly send a synthetic test. Opening a page or refreshing checks
never sends a message, changes permissions, writes a secret, or installs an app.

| Channel | Runtime authorization | A successful test proves |
| --- | --- | --- |
| Email | API system-assigned managed identity; Exchange Application RBAC scoped to one mailbox | Graph accepted a send request, not delivery |
| Shared Teams | Secret-URL Teams Workflow; secret in MOSAIC's Key Vault | The Workflow trigger accepted the request, not that downstream actions ran |
| Private Teams | Dedicated single-tenant Azure Bot identity; authenticated personal installation | The Bot Connector accepted a message for that personal conversation |

Channel enablement, routing, preferences, and subject visibility are still checked for normal
notifications. An explicit self-test does not opt anyone in. Administrator and portal preferences
and private test evidence are separate; private delivery never falls back to a shared channel.

## Microsoft Graph email

Azure Communication Services Email retires on
[September 30, 2028](https://learn.microsoft.com/en-us/azure/communication-services/acs-retirement-and-breaking-changes-guide).
MOSAIC no longer provisions or calls it. Existing saved ACS configuration is marked **Migration
required**, never silently treated as Graph configuration. Existing cloud resources are not
deleted by this change; cleanup requires a separate resource-owner decision.

### Prepare the identity and mailbox

Use an existing, provisioned Exchange Online mailbox in the deployment's tenant. Choosing an
address does not create a mailbox or buy a license. Obtain its **Entra user object ID** and the
API host's **system-assigned managed identity** application/client ID and service-principal
object ID. These three identifiers are different. Neither the API sign-in app nor the private
Teams bot identity should be substituted.

The email transport explicitly uses the API host's managed identity, not a developer's Azure CLI
login, `/me`, or delegated `Mail.Send`. `MOSAIC_GRAPH_ENDPOINT` selects a supported Graph cloud
origin; the default is `https://graph.microsoft.com`. A Government Azure management connection
does not change the notification tenant or cloud. Private Teams currently supports public cloud
only; validate Exchange and Graph availability in the chosen cloud independently.

### Grant mailbox-scoped send access

An Exchange administrator with the required delegating role performs this setup, not MOSAIC.
Use [Exchange Application RBAC](https://learn.microsoft.com/en-us/exchange/permissions-exo/application-rbac),
not a tenant-wide Graph application grant. Inspect existing principals, scopes, and assignments
first; create only the missing objects.

The following is an operator-run template. Replace every placeholder, confirm that the preview
contains **exactly the intended mailbox**, then create the role assignment:

```powershell
Connect-ExchangeOnline
$ApiClientId = '<API-managed-identity-client-id>'
$ApiPrincipalId = '<API-managed-identity-service-principal-object-id>'
$MailboxAddress = '<approved-sender-primary-smtp-address>'
$ScopeName = 'MOSAIC-Notification-Sender'

New-ServicePrincipal -AppId $ApiClientId -ObjectId $ApiPrincipalId -DisplayName 'MOSAIC API'
New-ManagementScope -Name $ScopeName `
  -RecipientRestrictionFilter "PrimarySmtpAddress -eq '$MailboxAddress'"
Get-Recipient -RecipientPreviewFilter (Get-ManagementScope -Identity $ScopeName).RecipientFilter |
  Select-Object PrimarySmtpAddress, ExternalDirectoryObjectId
New-ManagementRoleAssignment -Name 'MOSAIC-Notification-MailSend' `
  -App $ApiPrincipalId -Role 'Application Mail.Send' -CustomResourceScope $ScopeName
Test-ServicePrincipalAuthorization -Identity $ApiPrincipalId -Resource $MailboxAddress
```

Confirm `Application Mail.Send` is in scope for this mailbox and out of scope for an unrelated
mailbox. The simulation checks Exchange RBAC, **not separate Entra grants**. Permissions are
additive: an organization-wide Entra `Mail.Send` grant would defeat mailbox scoping and must not
be added alongside this assignment. Runtime email does not require `Mail.Read`,
`MailboxSettings.Read`, or `Exchange.ManageAsApp`. RBAC-only application tokens may have no
`Mail.Send` role claim; MOSAIC does not incorrectly reject them for that reason.

### Save, check, and test

1. In **Notifications > Email**, choose **Configure sender** and save the mailbox's Entra user
   object ID. The optional address label is descriptive; it cannot override the sending mailbox.
2. Run **Check email access**. It verifies the runtime's app-only tenant/audience/identity and,
   when directory lookup is available, resolves the mailbox contact. Directory resolution is
   optional evidence, not proof of Exchange provisioning or send scope.
3. Review the authorization guidance. There is no harmless `sendMail` dry run with send-only
   permissions. Token acquisition cannot prove the mailbox-scoped role assignment.
4. Enter an authorized test recipient and explicitly send the synthetic email. Graph
   [`sendMail`](https://learn.microsoft.com/en-us/graph/api/user-sendmail) returns an empty
   **202 Accepted** response. Check the recipient mailbox and junk folder for actual receipt.
5. Enable general email in **Channels** and the intended routing/preferences. **Send budget
   email** is a separate switch for the existing explicit budget owner/recipient audience.

The last explicit email test records its outcome, time, Graph request ID when supplied, and
configuration revision. Changing settings makes older evidence stale. Concurrent settings
changes cannot stamp an old test as verification of the new configuration.

| Diagnostic | What to check |
| --- | --- |
| Identity/token failure | API host identity attachment, configured tenant, Graph cloud and expected identity; do not substitute a signed-in user token |
| Mailbox not enabled / 404 | Correct mailbox user object ID, tenant, Exchange provisioning and applicable licensing |
| Graph 403 | Application Mail.Send assignment, exact mailbox scope, permission propagation and authorization simulation; the response alone cannot prove the precise missing policy |
| 429 | Retry-After and provider throttling; queued notifications retry automatically |
| Timeout / 408 / 5xx | Acceptance is uncertain, not confirmed failure or delivery |

## Shared Teams Workflow

Follow the [Teams integration guide](../apps/teams/README.md#shared-channels-teams-workflows)
for the exact trigger, post-card action, ownership, supported URL shapes, deployment settings,
Key Vault roles, and app-policy boundaries.

In **Notifications > Shared Teams**, save a destination and select it. **Check shared Teams
access** reads its secret and validates the exact-host allowlist and HTTPS configuration without
calling the webhook. Results separately explain missing secret-reader setup, denied vault
access, missing secrets, unsupported/disallowed endpoints, and missing channel/routing setup.

**Test destination** sends a synthetic card only after an explicit action. The last test is
persisted against the destination and deployment fingerprint. Editing the destination or
changing relevant deployment configuration invalidates the prior verification. Secret values
and upstream provider bodies never appear in results.

A 2xx response is trigger acceptance. Verify the Workflow's run history, owner/co-owner,
enabled state, DLP policy, Teams connection, post action, and actual channel receipt separately.
If policy forbids the trigger's **Anyone** secret-URL mode, this adapter is unavailable; do not
weaken policy or use a retired Office 365 connector.

Before creating the Workflow, its approved owner must have an authorized Microsoft Teams
connection in that Power Automate environment. A Teams license or Azure administrator login
is not that connection. In Power Automate, open **Connections** and add or repair Microsoft
Teams using the approved owner account; follow the
[connection setup instructions](https://learn.microsoft.com/en-us/power-automate/add-manage-connections).
Read-only MOSAIC checks cannot sign in for the owner or authorize the connection. An operator's
empty connection inventory does not establish that other owners have no connections.

## Private Teams app

Follow the [private bot setup](../apps/teams/README.md#private-messages-optional-single-tenant-azure-bot).
Use a dedicated single-tenant Azure Bot, preferably with an exact user-assigned managed identity
attached to the API host. Publish an approved **personal-scope** package, enable its Teams
channel, and configure the authenticated `/api/v1/teams/messages` callback.

The console's **Private Teams** and **Profile** views, and the portal's **Notification
preferences**, show the authenticated caller's installation and last test. **Check my Teams
access** acquires a runtime Bot Connector token without sending; errors distinguish the bot
identity, Key Vault credential, rejected/expired OAuth credential, unavailable token endpoint,
installation, and opt-in stages. A provider 403 can mean identity, installation, or policy; it
is not presented as a proven single cause.

Install the approved app for yourself, test your own private conversation, then explicitly opt
in to categories. A catalog app ID, bot OAuth client ID, and Entra user object ID are not
interchangeable. No conversation ID is accepted from users. Private test evidence is isolated
by tenant, caller, persona, current registration, and authenticated binding; another user's or
persona's test does not verify yours. Reinstallation/configuration changes make old evidence
stale.

### Operator access is separate from bot access

An Azure subscription role, a successful Azure CLI login, or even the operator's Global
Administrator role does not prove that the operator's client has the Microsoft Graph scopes
needed to inspect or publish Teams apps. If a catalog or personal-installation query returns
`Forbidden` with **Missing scope permissions**, this is an authorization failure of that
operator client, not proof of a bad bot credential or a missing installation.

An authorized Teams administrator can publish and allow the package in the Teams admin center;
the person can then install it in Teams under the organization's app policies. For approved
administrative automation, use the endpoint-specific permissions documented for
[the app catalog](https://learn.microsoft.com/en-us/graph/api/appcatalogs-list-teamsapps) and
[personal installations](https://learn.microsoft.com/en-us/graph/api/userteamwork-list-installedapps).
Do not add directory-wide, catalog, or installation permissions to the MOSAIC API or bot just
to repair an operator's CLI session. Runtime delivery uses Bot Connector OAuth and authenticated
installation events, not those administrative Graph APIs. An unreadable catalog or installation
list remains **unverified**; it must not be reported as empty or successfully configured.

## Retries and evidence

Delivery is preferred over duplicate avoidance. Transient failures, uncertain provider outcomes,
and expired sending leases retry automatically with bounded backoff/Retry-After. Ordinary
deliveries default to five attempts; compatibility budget email retains three, including
already-consumed attempts. Once exhausted, uncertainty remains visible as **unknownOutcome**.
An audited manual retry can start another normal attempt budget, with no duplicate-risk hold or
special acknowledgement. Correlation/request IDs are diagnostic, not provider deduplication.

Read-only checks never retry sends. An explicit synthetic test is a single provider send and
records its observed result; normal queued notifications own automatic retries. No accepted
request is described as delivered or read without independent evidence.

Accepted email and Teams tests provide five minutes of revision-bound send evidence, not
delivery confirmation. A successful email test does not establish that Exchange access is
exclusive to the sender mailbox; the administrator must verify that scope separately.

Teams test evidence is dated when the test starts. If tests overlap, an older in-flight test
cannot replace a newer saved result; its response still reports the actual send outcome.
