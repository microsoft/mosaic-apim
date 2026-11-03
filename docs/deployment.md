# Deploy MOSAIC to Azure

[Documentation](README.md) | [MOSAIC](../README.md)

Check the [prerequisites](development.md#prerequisites) first. These commands provision the
reference hosting deployment; they are separate from publishing a model through a managed gateway.
For identities and network paths, see [architecture](architecture.md).

- [Deploy with `azd`](#deploy-with-azd)
- [Optional cost-center group management](#optional-cost-center-group-management)

## Deploy with `azd`

Sign in and create the normal development environment:

```powershell
az login
azd auth login
az account set --subscription <your-subscription-id>
azd env new mosaic-dev
azd env set AZURE_SUBSCRIPTION_ID <your-subscription-id>
azd env set AZURE_LOCATION eastus2
azd env set MOSAIC_APIM_PUBLISHER_NAME "MOSAIC administrator"
azd env set MOSAIC_APIM_PUBLISHER_EMAIL "your-admin-address@example.com"
azd env set MOSAIC_PYTHON_INDEX_URL "https://pypi.org/simple"
azd up
```

The preprovision hook idempotently creates separate single-tenant Entra registrations:

- `mosaic-dev-api`: `access_as_user` delegated scope, plus an `Admin` and a `User` app role
- `mosaic-dev-spa`: administrator console SPA redirects and delegated permission to the API
- `mosaic-dev-portal`: end-user portal SPA redirects and delegated permission to the API
- `mosaic-dev-model-runtime`: the separate audience for APIM model calls, with the
  `Models.Invoke` delegated scope, `Models.Invoke.Application` application permission,
  `Mcp.Invoke` delegated scope and `Mcp.Invoke.Application` application permission. Bootstrap
  configures `groupMembershipClaims: SecurityGroup` so user, application and agent runtime tokens
  can carry Entra security-group object IDs.
- `mosaic-dev-model-client`: a public client people sign in with to get model-runtime tokens. It
  has no secrets, certificates or app roles, and its only permissions are delegated
  `Models.Invoke` and `Mcp.Invoke`, with tenant-wide admin consent. Interactive
  (`http://localhost`) and device code sign-in both work.

It assigns the deploying user the initial `Admin` role. The postprovision hook adds the deployed
web redirect and the deployed portal redirect, the latter from the `PORTAL_APP_URL` output of
the portal App Service. A directory authorization failure stops deployment and identifies the
failed operation; identity setup is never skipped. Graph directory lookup needs admin consent for
`User.ReadBasic.All`, `GroupMember.Read.All` and `AgentIdentity.Read.All` on the API managed
identity. Application and managed-identity search additionally needs `Application.Read.All`;
optional people and administrator photos need `ProfilePhoto.Read.All`. Neither additional
permission is granted automatically; see [identity photos and permissions](identity-photos.md).
The model client's delegated consent also needs Cloud Application Administrator,
Application Administrator or Privileged Role Administrator. Without one of those roles the hook
prints warnings with exact commands for an administrator, and deployment continues. People can't
get tokens with the model client until its consent exists, and directory lookup stays degraded
until the Graph consent exists.

To skip the model client, run `azd env set MOSAIC_ENTRA_MODEL_CLIENT false` before provisioning.
The hook then leaves any existing model client registration and consent alone, and keeps
`MOSAIC_MODEL_CLIENT_ID` as set. So you can point it at a client you manage yourself, as long as
that client is consented for `Models.Invoke` and `Mcp.Invoke` when it is used for both models and
MCP. Also set `MOSAIC_ENTRA_MODEL_CLIENT` to `false` before you revoke the model client's consent,
or the next `azd provision` grants it again.

Assign the `User` app role — normally to an Entra group — to everyone who should reach the portal.
Tenant membership alone does not grant it.

People with a user grant sign in with the model client to request
`api://<model-runtime-client-id>/Models.Invoke`. Bootstrap writes its client ID as
`MOSAIC_MODEL_CLIENT_ID`, and connection details show it as `entraClientId`. See
[Call a published model with an Entra token](call-models-with-entra-tokens.md). Any other
delegated client needs its own consent for that scope. Applications use the
`Models.Invoke.Application` permission and request `api://<model-runtime-client-id>/.default`.
Agent identities use the same `.default` runtime scope through the Agent ID token flow, and agent
users use the delegated scope through their parent agent identity. See
[Call a published model with Microsoft Entra Agent ID](call-models-with-agent-identities.md).
For published MCP servers, people and agent users request
`api://<model-runtime-client-id>/Mcp.Invoke`; applications and agent identities request
`api://<model-runtime-client-id>/.default` and need `Mcp.Invoke.Application`. See
[Connect to MCP servers published through MOSAIC](connect-to-mcp-servers.md).
Assign these permissions through normal Entra administration. A MOSAIC resource grant does not silently
consent a client, create an identity, assign app roles, or grant Microsoft Graph permissions. The
only delegated runtime consent bootstrap creates is for the model client's `Models.Invoke` and
`Mcp.Invoke` grant. Bootstrap exposes
`MOSAIC_MODEL_RUNTIME_CLIENT_ID`; this must not be the MOSAIC control-plane API's client ID.

## Optional cost-center group management

The directory-read baseline above remains read-only. Group management is an **optional,
separately consented API-identity capability**, not an APIM requirement or a side effect of normal
deployment. A MOSAIC Admin role and Azure subscription access do not grant directory consent.

1. Identify the API host's actual service principal in the configured tenant. Review existing
   permissions before granting more; do not grant these permissions to the console, portal,
   model client, runtime audience, or gateway identity instead.
2. Choose only the capabilities needed:

   | Capability | Explicit Graph application consent | Bootstrap opt-in, `postprovision` only |
   | --- | --- | --- |
   | Attach/verify an eligible existing group and read membership | Existing directory-read baseline; no new write consent | No group-write switch |
   | Create a center group | Optional `Group.Create` | `--grant-group-create` |
   | Add/remove user members from a bound group | Optional `GroupMember.ReadWrite.All` | `--grant-group-member-write` |
   | Add service principals, including managed identities and app-only agents | Additional separately consented `Application.ReadWrite.All` | `--grant-service-principal-member-write`, together with `--grant-group-member-write` |

3. Have a suitably privileged tenant administrator explicitly grant the selected application
   permissions to that API service principal. Use normal Entra/Graph permission administration
   or the bootstrap helper's explicit switches above; inspect
   `python scripts\mosaic_entra.py --help` before using them. Do not infer consent from
   saved deployment configuration. These permissions can be tenant-wide even though MOSAIC
   accepts mutations only for a center's verified bound group.
4. Inspect the reported creation, user-member, and workload-member capabilities independently.
   Capability checks are read-only, not test group creation/deletion. Permission denial and
   propagation delays must remain visible.
5. In center setup, attach an ordinary cloud-managed, assigned-membership, security-enabled,
   mail-disabled, non-role-assignable group, or explicitly create one. Review the warning that
   membership edits may affect other applications using an attached group. A group can bind to
   only one center.

After the API managed identity exists, an authorized administrator can preview the selected
creation/user-membership consent changes without applying them:

```powershell
python scripts\mosaic_entra.py postprovision --dry-run `
  --grant-group-create --grant-group-member-write
```

Only remove `--dry-run` after reviewing and authorizing the tenant consent changes. Workload-member
consent is a further, broad opt-in: add `--grant-service-principal-member-write` only when needed
and explicitly approved, together with `--grant-group-member-write`. These switches do not
create a cost-center group or add its initial members. Omitted switches leave optional consent
unrequested; they do not revoke permissions previously granted.

MOSAIC-created groups explicitly name the API service principal as owner; `Group.Create` must
not result in an ownerless group. Adding human owners is a separate choice with appropriate
lookup permissions, not something inferred from cost-center contact emails. Microsoft's
[creation contract](https://learn.microsoft.com/en-us/graph/api/group-post-groups?view=graph-rest-1.0)
documents additional lookup permissions for assigning other owners/members at creation.
`Application.Read.All` for optional lookup is **not** a substitute for the separately consented
`Application.ReadWrite.All` required by the
[service-principal add-member contract](https://learn.microsoft.com/en-us/graph/api/group-post-members?view=graph-rest-1.0).

Save a center before group creation. Missing consent or failed/uncertain creation leaves it
setup-required and recoverable; never retry an uncertain create as a new group blindly. A verified
attachment without membership-write consent supports a read-only Members view. No workflow
automatically consents app registrations, assigns runtime/Azure roles, or deletes an Entra group.
Membership removal deletes only the `/$ref` relationship, never the directory object.

APIM needs **no Graph permissions** for this feature. It matches validated JWT group IDs against
applied grants and does no per-inference directory lookup. Resource token/key enablement and
individual key paths remain explicit. Government is excluded from the new workflow.

**Preview limitation:** the direct Members list uses Graph **beta**, Microsoft's
[workaround for v1.0 omitting service principals](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-1.0).
That listing API is subject to change and
[not supported for production use](https://learn.microsoft.com/en-us/graph/api/group-list-members?view=graph-rest-beta).
Do not treat the member editor as a production-supported directory integration. This limitation
is separate from APIM's stable JWT group-claim authorization; no live Entra/APIM interoperability
has been proved for this redesign.

Old MVP custom centers require explicit reconfiguration/recreation; there is no production
migration or silent member copy/data reset. Use fictional adapters for local tests and screenshots,
and obtain separate authorization for any live non-production group mutation or gateway check.
See [cost centers](cost-centers.md), [development fixtures](development.md#cost-center-development-and-old-mvp-data),
and [ADR 0030](adr/0030-entra-backed-cost-centers.md).

## Hosting and cost notes

The console and portal containers serve `index.html` and every SPA route with
`Cache-Control: no-cache`, `/config.js` with `no-store`, and the content-hashed files under
`/assets/` with `public, max-age=31536000, immutable`. After a redeploy, the next page load
revalidates `index.html` and picks up the new bundle and runtime configuration. The policy is in
`apps/web/nginx.conf` and `apps/portal/nginx.conf`, which are kept identical.

APIM Developer is the dominant cost (illustratively roughly USD 51/month at continuous use) and can take
30–60 minutes or longer to provision. The shared B1 Linux plan is roughly USD 13–15/month; Basic ACR
is roughly USD 5/month. Cosmos serverless, Key Vault, and monitoring are consumption-based. Expect
an illustrative idle baseline near USD 70/month before meaningful telemetry volume. These are not
a current quote: verify regional SKU prices and expected usage before provisioning.

Remove the environment when not in use:

```powershell
azd down --purge
```
