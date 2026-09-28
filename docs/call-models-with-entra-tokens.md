# Call a published model with an Entra token

This guide is for people with a MOSAIC grant for a published model that accepts Microsoft Entra
tokens. You sign in with the MOSAIC model client, get a token for the model runtime, and send it to
API Management as a bearer token. Applications don't use this client. They sign in as themselves
with client credentials and the `Models.Invoke.Application` permission, requesting
`api://<model-runtime-client-id>/.default`.

## Before you start

You need an applied grant for the model, and Entra tokens must be enabled for it
(`appliedMethods.entraEnabled` is `true`). Copy these values from the grant's connection details
in the portal, or from `GET /api/v1/me/entitlements/{id}/connection`:

| Field | What it's for |
| --- | --- |
| `tenantId` | The Entra tenant you sign in to |
| `entraClientId` | The client ID you sign in with |
| `entraScope` | The scope to request, `api://<model-runtime-client-id>/Models.Invoke` |
| `endpoint` and an `operations[].path` | Together, the URL you call |
| `deploymentName` | The `model` value for routes that don't include a deployment in their path |
| `apiShape` | The API the model speaks: `azureOpenAi`, `foundryModels` or `anthropicMessages` |
| `subscriptionHeader` | The header name for your subscription key, if you also send one |

When `entraClientId` is present, the portal's **Get a token (Python)** sample under **Connection
details** has the tenant, client ID and scope filled in.

If `entraClientId` is empty, either your deployment doesn't use the MOSAIC model client
(`MOSAIC_ENTRA_MODEL_CLIENT=false`) or the model's access was applied for an earlier runtime
registration. Ask an administrator for the client ID to use, or to reapply the model's access plan.

Install the libraries used below:

```powershell
python -m pip install msal requests
```

## Get a token

Device code sign-in works anywhere, including over SSH or on a machine without a browser. It
prints a URL and a code, which you enter on any device.

```python
import msal

TENANT_ID = "<tenantId>"
CLIENT_ID = "<entraClientId>"
SCOPE = "<entraScope>"

app = msal.PublicClientApplication(
    CLIENT_ID, authority=f"https://login.microsoftonline.com/{TENANT_ID}"
)
flow = app.initiate_device_flow(scopes=[SCOPE])
if "user_code" not in flow:
    raise SystemExit(flow.get("error_description", "Device code sign-in could not start"))
print(flow["message"])
result = app.acquire_token_by_device_flow(flow)
if "access_token" not in result:
    raise SystemExit(f"{result.get('error')}: {result.get('error_description')}")
token = result["access_token"]
```

On a desktop you can sign in with the system browser instead. MSAL listens on a free
`http://localhost` port for the redirect:

```python
result = app.acquire_token_interactive(scopes=[SCOPE])
```

Access tokens expire after 60 to 90 minutes. MSAL caches them in memory for the life of the `app`
object, so try the cache before you prompt again:

```python
accounts = app.get_accounts()
result = app.acquire_token_silent([SCOPE], account=accounts[0]) if accounts else None
```

A token is a bearer credential. Keep it out of files, chats and tickets.

## Call the model

Send the token in the `Authorization` header. Use the connection's `endpoint` and an operation
`path`, plus an `api-version` the deployment supports:

```python
import requests

ENDPOINT = "<endpoint>"
PATH = "/openai/deployments/<deploymentName>/chat/completions"
API_VERSION = "<api-version>"

headers = {"Authorization": f"Bearer {token}"}
# Only if keys are also enabled and your client sends a key. It must belong to this same grant.
# headers["<subscriptionHeader>"] = "<your primary or secondary key>"

response = requests.post(
    f"{ENDPOINT.rstrip('/')}/{PATH.lstrip('/')}",
    params={"api-version": API_VERSION},
    headers=headers,
    json={"messages": [{"role": "user", "content": "Hello"}]},
    timeout=60,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"]["content"])
```

The token works on its own. When a model accepts both methods, you can also send your
subscription key under the `subscriptionHeader` name. Both credentials must then be valid and
belong to the same grant. The gateway never falls back to one credential when the other is
rejected. Both count against the same grant limits.

For the responses route, and for AI Services routes without a deployment in their path, set the
request body's `model` to `deploymentName`.

### Claude models

A Claude model's `apiShape` is `anthropicMessages`, and its only operation is
`/anthropic/v1/messages`. Send an Anthropic Messages request body with `model` set to
`deploymentName`. The route takes no `api-version`, and the gateway adds the `anthropic-version`
header when a request omits it. With the Anthropic Python SDK (`python -m pip install anthropic`),
pass the token as `auth_token`, which the SDK sends as a bearer token:

```python
import anthropic

client = anthropic.Anthropic(base_url=f"{ENDPOINT.rstrip('/')}/anthropic", auth_token=token)
message = client.messages.create(
    model="<deploymentName>",
    max_tokens=256,
    messages=[{"role": "user", "content": "Hello"}],
)
print(message.content[0].text)
```

The gateway removes any `x-api-key` header, so a subscription key goes in the `subscriptionHeader`
header, never in `api_key`.

## Troubleshooting

| What you see | Cause and fix |
| --- | --- |
| `AADSTS65001` or `AADSTS90094`, or a **Need admin approval** page | The model client doesn't have tenant-wide consent for `Models.Invoke`. An administrator runs `az ad app permission grant --id <entraClientId> --api <entraAudience> --scope Models.Invoke`. |
| The same errors after that consent exists | Your tenant might also block consent for the OpenID Connect sign-in permissions that MSAL always requests. An administrator runs `az ad app permission grant --id <entraClientId> --api 00000003-0000-0000-c000-000000000000 --scope "openid profile offline_access"`. |
| `AADSTS7000218` (the request needs `client_assertion` or `client_secret`) | Public client flows are off for the model client. An administrator sets **Allow public client flows** to **Yes** on the registration's **Authentication** page, or reruns `azd provision`. |
| `AADSTS50011` (redirect URI mismatch) | `http://localhost` was removed from the registration's **Mobile and desktop applications** redirect URIs. An administrator adds it back or reruns `azd provision`. |
| `AADSTS500011` (resource principal not found) | The scope doesn't match a registration in this tenant. Copy `entraScope` and `tenantId` exactly. |
| `AADSTS50105` | The model client requires user assignment. Ask an administrator to assign you. |
| HTTP 401 from the gateway | The gateway couldn't validate the credential. The token might be expired, or be for another audience, such as a MOSAIC portal or Azure CLI token. Or the model doesn't accept the method you used. |
| HTTP 403 from the gateway | The token is valid but doesn't match an applied grant. The grant might not be applied yet or has been revoked. You might be signed in as a different user. Or a key you also sent belongs to a different grant. |
