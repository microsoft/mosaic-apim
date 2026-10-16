# Manage Azure Government from commercial MOSAIC

One commercial MOSAIC deployment can keep its existing commercial sign-in, directory, grants,
cost centers, and reports while managing resources in Azure Government through a separate
Government service principal. **AzGov is a cloud, not an environment.** A Government production
offering shows both **AzGov** and **Production**; its environment does not choose its credentials.

This is a cross-cloud management and reporting arrangement, not a sovereign deployment of MOSAIC.
Government resource metadata, access records, telemetry, and the management credential are
processed or stored in commercial Azure. Obtain your organization's approval for that boundary
before connecting an estate. These controls do not establish regulatory compliance or authorize
transferring restricted data.

## Four independent identity boundaries

| Hop | Credential and authorization |
| --- | --- |
| Browser to MOSAIC | Existing commercial Entra sign-in and MOSAIC `Admin` or `User` role |
| MOSAIC to Government management APIs and logs | Service principal in the Government tenant, using its Government authority and scoped Azure role assignments |
| Client to Government APIM | APIM subscription key owned by the exact applied direct MOSAIC grant and its cost center |
| Government APIM to a Government model backend | The gateway's own Government managed identity and backend role; a separately supported backend-key configuration where applicable |

A commercial control-plane token is not a Government gateway credential. Entering a Government
email address does not prove ownership of a Government account, and MOSAIC does not look it up or
link it. Government user sign-in, Government Graph expansion, and verified account linking are
not part of this flow. A subscription key attributes a call to its grant; it cannot prove which
human used a copied key.

## Configure a management connection

1. Provision the separate commercial management-credentials vault. `infra/main.bicep` creates it,
   grants **only the MOSAIC API identity** Key Vault Secrets Officer, and sets
   `MOSAIC_MANAGEMENT_KEY_VAULT_URI`. For an existing deployment, provision the infrastructure
   update or supply an equivalent isolated vault and role assignment. Do not reuse
   `MOSAIC_KEY_VAULT_URI`: APIM can read that backend-secret vault.
2. In the Government tenant, an authorized operator creates an application/service principal
   and a client secret. Record its tenant ID and application/client ID. Its service-principal
   object ID is different from both; use the object ID for Azure role assignments.
3. Grant management access only at the required scopes. APIM read/discovery needs read access;
   publication, governed subscriptions, and key operations require the corresponding APIM
   contributor permissions. Backend inventory and permission checks need resource/role-assignment
   read access. Resource-centric Log Analytics queries and telemetry checks need the monitoring
   permissions reported by the gateway's preflight. Grant those to the Government service
   principal, not the commercial host identity.
4. In **Settings > Azure connections**, add a named **Azure Government** connection with those
   IDs and the secret **value**, not its secret ID. The input is write-only. MOSAIC stores only
   vault references and safe metadata in Cosmos; it never returns the value.
5. **Test authentication**, then select this connection during gateway or model onboarding.
   A successful token test is not proof of subscription visibility, resource write permissions,
   backend access, or telemetry readiness. Review each resource's actual preflight results and
   precise remediation before switching a gateway from observe to manage.

Existing commercial records without connection fields retain their commercial host identity.
One connection can serve several resources in its cloud. Connections cannot be removed while
registered resources use them. Renaming a connection does not move resources or change cloud.
Replacing its secret verifies the candidate before switching the stored reference. If a later
cleanup fails, retry the operation; retained cleanup references prevent forgetting old secrets.
Removing the connection deletes its stored credential, not the Government service principal,
application, role assignments, or Azure resources.

Management credentials are never placed in a runtime named value, sent to an APIM backend, or
returned to the end-user portal. Government backend secrets must stay in a compatible Government
backend vault, not either commercial MOSAIC vault.

**Current limits:** Government gateways are registered by their explicit ARM resource ID.
Government backend-secret vault flows and unverified Government Anthropic endpoint shapes are
blocked explicitly; no commercial credential or hostname is substituted. Use a supported
Government managed-identity backend rather than configuring one of those blocked flows.

## Publish and grant access

Register Government gateways and backends with the Government connection and classify their
environment independently. Keep the model backend in the same cloud as its gateway. MOSAIC's
reviewed plan/apply gate and observe/manage distinction still apply.

Government runtime access is **subscription-key only**. Configure an eligible direct grant for
the commercial MOSAIC person or workload and a shared cost center that allows keys. Group-only
access is not converted into a shared key, and a cost center that disallows keys remains blocked.
Approval records desired access; it does not apply policy. After a successful reviewed apply,
initial key setup is automatic for eligible Government grants. Setup failures remain distinct
from ready access and can be retried without rotating an existing key. After startup recovery,
MOSAIC resumes unfinished setup every five minutes, independently of usage collection and budgets.

The holder explicitly reveals the key in **My access > Connection details**, and authorized
administrators can use the grant's key controls. Keys remain in APIM; automatic provisioning does
not reveal, copy, email, or store a plaintext key. Rotation, deletion, and revocation retain their
ownership and apply checks. Explicit deletion is not undone by an ordinary refresh or reapply.

Use the endpoint, model/API path, and cost-center instructions for the **exact offering and
grant**. A key for one cloud, API, cost center, or pool model grants no access to another. Product,
global, unknown, or otherwise unowned subscriptions are not a shortcut around an applied grant.
Bearer-only requests are not accepted as a fallback. Samples use a secure key input and
`Ocp-Apim-Subscription-Key`; they do not require commercial `az login`.

For a supported Government MCP publication, use an MCP client that supports a custom subscription
key header. Do not configure commercial OAuth discovery for that offering. A model call made by
the MCP workload needs its own eligible applied model grant/key; a reference header is only
correlation, never authentication.

## Network and capability checks

A key does not provide connectivity to a private Government gateway or backend. Arrange approved
DNS, routing, firewall/private-endpoint access, and outbound HTTPS connectivity separately.

| Management target | Government endpoint |
| --- | --- |
| Entra authority | `https://login.microsoftonline.us` |
| ARM and its matching token scope | `https://management.usgovcloudapi.net` |
| Log Analytics query endpoint | `https://api.loganalytics.us` |
| Azure portal | `https://portal.azure.us` |

Ordinary APIM hostnames end in `.azure-api.us`; custom gateway domains can differ. Verified Azure
AI host families include `.openai.azure.us` and `.cognitiveservices.azure.us`. A plausible hostname
does not establish provider, API-version, tier, policy, region, or native MCP support.
The [general MCP availability table](https://learn.microsoft.com/azure/api-management/mcp-server-overview#availability)
lists tiers, not Government regional parity. Microsoft's
[Government comparison guidance](https://learn.microsoft.com/azure/azure-government/compare-azure-government-global-azure#guidance-for-developers)
explicitly calls out service, feature, and configuration differences.

Unavailable or unverified Government features must remain explicitly blocked/unknown rather than
falling back to an ungoverned API, commercial credentials, or a commercial backend. Review the
resource's capability and preflight results. Offline tests and the fictional demo do not establish
Government regional service availability. Before production use, an authorized operator must
smoke-test authentication, inventory, supported reviewed publication, key rejection/acceptance,
backend identity, rotation/revocation, and telemetry on non-sensitive Government test resources.

## Usage, prices, and budgets

Commercial and Government offerings keep separate resource/grant identities but use the same
commercial MOSAIC tenant and cost-center IDs. **All clouds** combines their authorized usage and
cost; cloud filters retain provenance. A failed Government query must remain a freshness/partial
data condition, not zero usage or a reason to discard healthy commercial results.

Price selection follows the served deployment's cloud, region, model/version, SKU, and effective
date, not the browser host, environment tag, or selected connection name. Missing Government rates
remain explicitly unpriced/partial; commercial prices and zero are not substituted. Administrator
price overrides affect pricing only. Shared cost-center budgets aggregate both clouds, with
gateway enforcement routed through each gateway's own connection. Broader price-catalog refresh
is separate work.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Can't add a connection | Is the isolated commercial management vault configured and accessible to the API identity? |
| Authentication fails | Government tenant/application IDs, authority, secret value/expiry, and API vault access |
| Token test passes but a resource is inaccessible | Exact Government principal object ID, Azure role, scope, tenant, resource ID, and network path; token acquisition proves none of those permissions |
| Key access is unavailable | Managed gateway, successful apply, direct active grant, allowed cost center, supported capability, and completed key setup |
| Key works but a model hop fails | Government gateway identity's backend role/audience and backend/network compatibility |
| Government usage is missing | Government connection credentials, resource-centric monitoring permission, diagnostics, retention, and reported query freshness/error |

Never paste a client secret, access token, subscription key, or real resource capture into an
issue or log. Rotate a leaked credential at its source and replace/revoke it through the
appropriate connection or grant controls.
