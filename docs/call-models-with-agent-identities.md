# Call a published model with Microsoft Entra Agent ID

This guide is for agent builders and administrators who want an Entra Agent ID identity to call a
MOSAIC-published model through API Management. It covers both app-only agent identity tokens and
delegated tokens for an agent's user account.

## Before you start

You need a published model with governed Entra access applied, and these values from MOSAIC
connection details:

| Field | What it's for |
| --- | --- |
| `tenantId` | The tenant where the agent identity exists |
| `entraAudience` | The model-runtime audience, normally `api://<model-runtime-client-id>` |
| `entraScope` | Delegated scope for agent users, or `.default` for application callers |
| `requiredAppRole` | The app role an agent identity needs for app-only calls |
| `endpoint` and an `operations[].path` | Together, the URL the agent calls |
| `deploymentName` | The `model` value for routes that don't include a deployment in the path |

Connection details are available in the console on the entitlement, and through
`GET /api/v1/entitlements/{id}/connection` for administrators. The console never shows real tokens
or secrets.

## Agent identity, app-only

An agent identity is a service principal whose app ID and object ID are the same value. It has no
credentials of its own. Its parent agent identity blueprint obtains an exchange token and the agent
identity then requests the model-runtime token with its own client ID.

The resource scope is:

```text
api://<model-runtime-client-id>/.default
```

The agent identity must have the `Models.Invoke.Application` app role for the model-runtime
registration. That assignment can be direct on the agent identity, or inherited from the blueprint
principal when the model-runtime app is configured as an inheritable resource and the permission is
granted on the blueprint principal. A group app-role assignment is not enough for service
principals.

The protocol shape is:

```http
POST https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id=<agent-blueprint-client-id>
&scope=api://AzureADTokenExchange/.default
&fmi_path=<agent-identity-client-id>
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<blueprint-credential-or-managed-identity-token>
&grant_type=client_credentials
```

Then exchange that token as the agent identity:

```http
POST https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id=<agent-identity-client-id>
&scope=api://<model-runtime-client-id>/.default
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<exchange-token>
&grant_type=client_credentials
```

Use Microsoft.Identity.Web or the Entra Agent ID SDKs where possible instead of hand-writing the
token exchange.

## Agent user, delegated

An agent's user account is a user account assigned to exactly one agent identity. The agent identity
gets delegated tokens for that user through its parent blueprint. For MOSAIC model access, request:

```text
api://<model-runtime-client-id>/Models.Invoke
```

The agent identity or its blueprint needs consent for the delegated `Models.Invoke` scope. In an
interactive agent flow, the agent can request user or administrator consent. In an agent-user flow,
the blueprint and agent identity perform the documented token chain, and the resulting model token
has user-context claims for the agent user.

```http
POST https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token
Content-Type: application/x-www-form-urlencoded

client_id=<agent-identity-client-id>
&scope=api://<model-runtime-client-id>/Models.Invoke
&client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer
&client_assertion=<blueprint-exchange-token>
&user_federated_identity_credential=<agent-user-exchange-token>
&username=<agent-user-upn>
&grant_type=user_fic
&requested_token_use=on_behalf_of
```

## Grant access in MOSAIC

In the administrator console, open **Identity**, go to the **Agents** tab, and choose **Add agent**.
The directory search finds agent identities and agent users through Microsoft Graph. After the
identity is recorded, create a model entitlement:

- grant an `agentIdentity` directly when the agent calls app-only;
- grant an `agentUser` directly when the agent calls with delegated user context;
- grant an Entra `securityGroup` when the agent identity or agent user is a member and you want the
  group to carry the model grant.

Security-group grants use Entra tokens only. They do not issue APIM subscription keys.

## Call the model

The request is the same as for any Entra runtime token. Use the endpoint and operation from
connection details:

```python
import requests

ENDPOINT = "<endpoint>"
PATH = "/openai/deployments/<deploymentName>/chat/completions"
API_VERSION = "<api-version>"
TOKEN = "<runtime-access-token>"

response = requests.post(
    f"{ENDPOINT.rstrip('/')}/{PATH.lstrip('/')}",
    params={"api-version": API_VERSION},
    headers={"Authorization": f"Bearer {TOKEN}"},
    json={"messages": [{"role": "user", "content": "Hello"}]},
    timeout=60,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"]["content"])
```

For Foundry Models, Responses and Anthropic Messages routes, set the request body's `model` to the
`deploymentName` shown in connection details. Claude models use `/anthropic/v1/messages` and do not
take an `api-version` query parameter.

## Troubleshooting

| What you see | Cause and fix |
| --- | --- |
| HTTP 401 from the gateway | The token could not be validated. Check that it is a runtime token for the model audience, not a MOSAIC control-plane token, Graph token or expired token. |
| HTTP 403 from the gateway | The token is valid but does not match an applied grant. Reapply the model access plan, confirm the grant is enabled, and check whether you are calling as the agent identity or the agent user you granted. |
| Missing `Models.Invoke.Application` | Assign the app role directly to the agent identity, or configure inheritable permissions and grant the role on the blueprint principal. Group app-role assignment does not flow to service principals. |
| Group grant does not work | Get a fresh runtime token and confirm the `groups` claim contains the granted security-group object ID. If the token has group overage, use a direct grant. |
| Overage warning in the portal or smoke verifier | The caller is in too many groups for Entra to emit group IDs in the token. APIM cannot query Graph during model invocation, so a direct grant is required. |
| Grant not yet applied | Saving a grant is desired state only. Review and apply the publication's access plan, then wait for APIM propagation before retrying. |
