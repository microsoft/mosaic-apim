# MCP test servers for Phase 11

Four small MCP servers, and the kit that deploys them, for Phase 11 of the live end-to-end run:
journeys M1 to M10 in the [roadmap](../../docs/e2e/roadmap.md). The servers are generic and
deterministic, so each journey can check exactly what a call returns. Nothing here deploys
anything on its own. The coordinator runs [the deployment kit](#deploy-batch-5b) after the
environment owner approves it.

## The servers

| Server | Runs on | MCP endpoint | Upstream authentication in MOSAIC | Used by |
| --- | --- | --- | --- | --- |
| M-tools | Container Apps | Streamable HTTP at `/mcp` | **None** | M1 to M8 and M10 |
| M-tools SSE-only | Container Apps, the same image | HTTP+SSE only: `GET /sse`, `POST /messages/` | Not publishable | M1's negative case |
| M-protected | Container Apps, M-tools' image, behind built-in authentication | Streamable HTTP at `/mcp` | **Managed identity** | M1, M2, M5 and M10 |
| M-agent | Container Apps | Streamable HTTP at `/mcp` | **None** | M9, after M1 to M3 |

M-tools and M-protected run the same code, so they offer the same three tools, with the same
results:

| Tool | Returns |
| --- | --- |
| `echo(text)` | The text, unchanged |
| `utc_now()` | The current UTC time in ISO 8601, to the second, such as `2026-10-05T18:06:46Z` |
| `add(a, b)` | The sum of two numbers. `add(2, 3)` returns `5`, and `add(1.5, 2)` returns `3.5` |

`add` refuses `true` and `false`, anything else that isn't a number, and a sum that isn't finite.

### M-tools

The main server for the gateway journeys: registration and tool sync, a reviewed publication,
grants, portal requests, calls from MCP clients, call limits, revocation, usage and unpublishing.
It's built on version 1.30.0 of the official MCP Python SDK, the newest release of its 1.x line.
That line speaks the protocol's handshake era, as MOSAIC
([ADR 0007](../../docs/adr/0007-mcp-server-registration.md)) and API Management do, and its
newest revision, `2025-11-25`, is the one MOSAIC offers. The SDK's 2.x line defaults to the
stateless `2026-07-28` revision.

It takes no authentication. The gateway is its intended caller, but the server is public, as
MOSAIC's upstream authentication **None** assumes.

With `MCP_TRANSPORT=sse`, the same image serves only the deprecated HTTP+SSE transport. MOSAIC's
client reads a 400, 404 or 405 to its streamable HTTP `initialize` as that transport, and records
the server as an unsupported transport rather than registering it. This variant answers 405 at
`/sse` and 404 at `/mcp`, while an SSE client can still use it.

| Setting | Default | What it does |
| --- | --- | --- |
| `MCP_TRANSPORT` | `streamable-http` | `sse` serves the SSE-only variant |
| `MCP_SERVER_NAME` | `M-tools` | The name the server gives clients when they connect. M-protected sets `M-protected` |
| `MCP_STATELESS` | `false` | `true` runs the SDK's stateless streamable HTTP, with no `Mcp-Session-Id` |
| `MCP_JSON_RESPONSE` | `false` | `true` answers each POST with JSON rather than a one-event stream |
| `PORT` | `8000` | The port the server listens on |

By default each POST is answered with a server-sent event stream and the session is held in
memory, as most SDK servers behave, so the run exercises streaming and session headers through
the gateway. The two flags change that without a rebuild, if the gateway needs it. A request other
than `initialize` that names no session gets 400 and leaves no session behind, so the MCP
verifier's pooled-quota proof can probe the gateway with one.

### M-protected

M-tools' image, run as its own container app with `MCP_SERVER_NAME=M-protected`. It shows the
gateway attaching its managed identity's token to a server that requires it. The server's code
checks no token. Container Apps' built-in authentication (Easy Auth) runs beside it on every
replica and checks each request before passing it on.

The authentication admits only a v2 Entra token, issued by the tenant
(`https://login.microsoftonline.com/<tenant-id>/v2.0`), whose audience is the server's own: either
`<audience-app-id>` or `api://<audience-app-id>`. The token's client application must be one of
two:

- **API Management's system-assigned managed identity**, which calls the server for every
  published request. MOSAIC's MCP policy attaches its token with
  `<authentication-managed-identity resource="api://<audience-app-id>"/>`.
- **MOSAIC API's system-assigned managed identity**, which registers the server and syncs its
  tools. MOSAIC connects to a registered server itself, with its own identity, to read its tools.

A managed-identity MCP server must accept both identities: MOSAIC API's for registration and tool
sync, and the gateway's for calls. Without MOSAIC API's, MOSAIC records the server as degraded and
can't sync its tools.

The authentication answers 401 to a request with no valid token, on every path and never with a
redirect to a sign-in page, and 403 to a valid token from any other client. M-protected never
signs anyone in, so it has no client secret and keeps no tokens: checking a bearer token's
signature, issuer, audience and lifetime takes only the signing keys the tenant publishes. Entra
adds its own check: the audience's service principal requires assignment, and only those two
identities hold its app role, so nobody else can get a token for it. MOSAIC's publication attaches
only the managed identity's token.

**Why Container Apps.** M-protected first ran on Azure Functions' Flex Consumption plan, with the
Functions MCP extension at `/runtime/webhooks/mcp`. There, requests on fresh connections
alternated between a 401, often after about 20 seconds, and no answer at all, with or without a
token, so MOSAIC's connection check found it unreachable. Restarts and four times the memory
didn't change that, which pointed to the platform's request routing rather than the app. It now
runs like the other servers. [Replacing M-protected's Functions host](#replacing-m-protecteds-functions-host)
removes what the old host left.

### M-agent

One tool, `ask_model(question)`, that answers by calling one governed model through MOSAIC, as
the server's own application (journey M9 and [ADR 0025](../../docs/adr/0025-mcp-model-calls-on-a-persons-behalf.md)).
It follows [MCP servers that call models](../../docs/mcp-servers-that-call-models.md):

- It gets a token for `api://<model-runtime-client-id>/.default` from the container app's
  system-assigned managed identity, which holds `Models.Invoke.Application` and its own MOSAIC
  application grant on the model.
- It calls Azure OpenAI chat completions through MOSAIC's gateway, at the endpoint and deployment
  from the model's MOSAIC connection details, with `api-version` and `max_tokens` of 16.
- It passes on the `x-mosaic-on-behalf-of` value of the MCP request it's serving, exactly as it
  arrived. When none arrived, it sends none. It never invents, parses or stores one.
- It names a cost center with `x-mosaic-cost-center` only when one is configured.
- It returns the model's answer text. When the call fails, the tool returns an error that names
  the HTTP status and, when the response has one, MOSAIC's reason for the refusal, such as
  `The model call failed with HTTP 403: Model access denied.` An error never includes the token,
  and IDs, URLs and email addresses in a reason are replaced.

| Setting | Required | What it is |
| --- | --- | --- |
| `MOSAIC_MODEL_ENDPOINT` | Yes | `endpoint` from the model's connection details: `https://<gateway-host>/<model-api-path>` |
| `MOSAIC_MODEL_DEPLOYMENT` | Yes | `deploymentName` from the connection details |
| `MOSAIC_MODEL_API_VERSION` | Yes | The Azure OpenAI `api-version`, such as `2024-10-21` |
| `MOSAIC_RUNTIME_SCOPE` | Yes | `api://<model-runtime-client-id>/.default` |
| `MOSAIC_COST_CENTER` | No | A cost center code, sent as `x-mosaic-cost-center` |
| `MOSAIC_MODEL_TOKEN_PARAMETER` | No | `max_tokens`, the default, or `max_completion_tokens` for models that need it |
| `MOSAIC_MODEL_MAX_TOKENS` | No | The answer's token limit, 16 by default |

`MCP_STATELESS`, `MCP_JSON_RESPONSE` and `PORT` work as they do for M-tools.

M-agent is public and takes no authentication, like M-tools, so anyone who finds its URL can make
it call the model as its application. Its application grant's limits cap what that can cost, each
call is capped at 16 tokens and a 500-character question, and teardown removes it after the run.

### Logs

The servers log only status codes, durations and tool names: a line per HTTP request, a line per
tool call and, for M-agent, a line per model call. They never log a question, an answer, a header
value, a session ID, a URL or a token. A message from the MCP SDK or an HTTP library, which can
carry session IDs and host names, is reduced to its level and its exception's type. M-protected is
M-tools' code, so it logs the same lines.

## Layout

```text
e2e/mcp-servers/
  compile_requirements.py   Regenerates every requirements file below
  ruff.toml                 Lints this folder with the repository's rules
  m-tools/                  M-tools, its SSE-only variant and M-protected: m_tools/, tests/, Dockerfile
  m-agent/                  M-agent: m_agent/, tests/, Dockerfile
  deploy/                   The deployment kit
    main.bicep              The subscription-scope template deploy.py runs
    modules/                The pull identity, its AcrPull grant, the container apps and M-protected's authentication
    deploy.py               plan, deploy, outputs, smoke, teardown and remove-functions-host
    smoke.py                Smoke checks for the deployed servers, standard library only
    parameters.example.json The kit's inputs, to copy to parameters.local.json
```

Each server folder stands alone, with its own code, tests and pinned requirements, so it builds
and tests without the others. The servers aren't part of the repository's uv workspace and don't
change `uv.lock`, because the MCP SDK isn't in it.

## Run and test locally

Each folder has `requirements.txt`, which pins everything the server runs with sha256 hashes,
and `requirements-dev.txt`, which adds pytest. Install either into the folder's own environment.
`--no-config` keeps the workspace's uv settings, such as its `exclude-newer` cutoff, out of it.

```powershell
Set-Location e2e/mcp-servers/m-tools        # or m-agent, deploy
uv venv --no-config --python 3.13 .venv
uv pip install --no-config --python .venv --require-hashes -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

CI does the same for each folder, in the **MCP test servers** jobs, and lints the folder with
the workspace's ruff. Its **MCP test servers (m-protected)** job builds M-tools' image, runs it as
M-protected and checks it with the smoke checks.

**M-tools.** `.venv\Scripts\python -m m_tools` serves `http://127.0.0.1:8000/mcp`. Set
`MCP_TRANSPORT=sse` for the SSE-only variant at `/sse`. The tests start the real server on a free
port and call it over streamable HTTP with the SDK's client, with MOSAIC's own handshake, and with
the deployment kit's smoke checks. Check a running server with:

```powershell
python ..\deploy\smoke.py tools http://127.0.0.1:8000/mcp
python ..\deploy\smoke.py sse http://127.0.0.1:8000/sse    # with MCP_TRANSPORT=sse
```

**M-agent.** Set the `MOSAIC_*` settings, then run `.venv\Scripts\python -m m_agent`. Away from
Azure there's no managed identity, so `ask_model` returns an error saying the server couldn't get
a token. The tests mock the gateway with httpx and the identity with a fake credential, and cover
the on-behalf value passed on, a request without one, MOSAIC's refusals, and keeping the token
out of every error.

**M-protected.** Run M-tools with `MCP_SERVER_NAME=M-protected`:

```powershell
$env:MCP_SERVER_NAME = "M-protected"
.venv\Scripts\python -m m_tools                       # http://127.0.0.1:8000/mcp
python ..\deploy\smoke.py tools http://127.0.0.1:8000/mcp
```

Locally there's no built-in authentication in front of it, so anyone on the machine can call it,
the `tools` checks pass, and the `protected` checks fail with HTTP 200. M-tools' tests cover the
name. The deployment kit's tests check the authentication in the Bicep, and run the `protected`
checks against stand-in servers that answer 401, redirect, or don't answer in time.

**Requirements.** Pin new versions in a folder's `requirements.in`, then regenerate the
`.txt` files with `python e2e/mcp-servers/compile_requirements.py`. Pass `--index-url` to resolve
from a mirror. Each pinned version was at least a week old when it was chosen.

## Deploy (Batch 5b)

### Before you deploy: Entra (Batch 5a)

The coordinator makes these changes separately, with the environment owner's approval:

1. **MOSAIC's model runtime registration** gets the `Mcp.Invoke` delegated scope and the
   `Mcp.Invoke.Application` app role, beside `Models.Invoke` and `Models.Invoke.Application`.
2. **The MOSAIC model client**, and any other client a person calls through, gets consent for
   `api://<model-runtime-client-id>/Mcp.Invoke`.
3. **M-protected's audience** is its own single-tenant app registration:
   - identifier URI `api://<audience-app-id>`, issuing v2 access tokens
     (`requestedAccessTokenVersion: 2`);
   - one app role, such as `McpServer.Call`, held only by API Management's system-assigned
     managed identity and MOSAIC API's;
   - a service principal that requires assignment, so Entra issues tokens for it to no one else;
   - no client secret: M-protected's authentication only checks the tokens it's sent.
4. **M-agent's managed identity**, once the kit has created it, gets the
   `Models.Invoke.Application` app role on MOSAIC's runtime registration. Its principal ID is in
   the kit's outputs. MOSAIC then records it, grants it the model, and names it as the model
   caller on M-agent's publication.

The kit takes the client ID of each managed identity. To find one from its principal (object)
ID, such as MOSAIC's `APIM_PRINCIPAL_ID` and `API_APP_PRINCIPAL_ID` outputs, run
`az ad sp show --id <principal-id> --query appId --output tsv`.

### Inputs

Copy `deploy/parameters.example.json` to `deploy/parameters.local.json`, which git ignores, and
fill it in. The kit refuses a file that still holds a `<placeholder>`.

| Field | What it is |
| --- | --- |
| `subscriptionId`, `location` | Where the servers go, such as `eastus2` |
| `resourceGroup` | A new resource group, only for the servers. The kit creates and tags it, and teardown deletes it. The kit refuses MOSAIC's resource group, and any group it didn't create |
| `namePrefix` | Starts every resource name and image repository, `mosaic-mcp` by default |
| `logAnalyticsWorkspaceId` | Resource ID of the existing workspace, in MOSAIC's resource group, that every server logs to |
| `containerRegistryId` | Resource ID of the existing registry, in MOSAIC's resource group, the images are built in. Its admin user stays off |
| `imageTag` | Optional. The commit the images are built from, by default |
| `minReplicas` | 1 keeps each container app warm, 0 lets it scale to zero |
| `protected.tenantId` | The tenant that issues the tokens |
| `protected.audienceClientId`, `protected.audienceAppIdUri` | M-protected's audience, `<audience-app-id>` and `api://<audience-app-id>` |
| `protected.gatewayClientId` | The client ID of API Management's system-assigned managed identity |
| `protected.mosaicApiClientId` | The client ID of MOSAIC API's system-assigned managed identity |
| `agent.modelEndpoint`, `agent.modelDeployment` | From the model's MOSAIC connection details |
| `agent.modelApiVersion` | The Azure OpenAI `api-version`, `2024-10-21` by default |
| `agent.modelRuntimeClientId` | The client ID of MOSAIC's model runtime registration |
| `agent.costCenter` | Optional. A cost center code for M-agent's model calls |
| `agent.tokenParameter`, `agent.maxTokens` | `max_tokens` or `max_completion_tokens`, and 16 |

### Run it

`deploy.py` needs Python 3.12 or later and the Azure CLI, signed in to the tenant. It uses only
the standard library, and prints every command that changes anything before it runs it.
On Windows, when the Azure CLI's bundled Python is available beside its `az.cmd` launcher, the
kit runs it in isolated mode with explicit UTF-8 enabled (`-I -B -X utf8 -m azure.cli`). Environment
variables alone cannot enable UTF-8 under `-I`. Captured CLI output is decoded as UTF-8, replacing
invalid bytes. Build logs still stream to the console, and a failed CLI command still stops the
kit; it does not suppress logs or assume a remote build succeeded after a local failure.

```powershell
Set-Location e2e/mcp-servers/deploy
python deploy.py plan                  # the dry run
python deploy.py deploy
python deploy.py outputs
python deploy.py smoke
python deploy.py teardown --dry-run
python deploy.py teardown
python deploy.py remove-functions-host --dry-run
python deploy.py remove-functions-host
```

- **plan** is the dry run. It lists every resource deploy creates and the images it builds, then
  runs Azure Resource Manager's what-if. It changes nothing. `deploy --dry-run` does the same.
- **deploy** works in four steps. It deploys the resource group, a user-assigned identity and that
  identity's AcrPull grant on the registry. Then it builds both images in the registry with
  `az acr build`, so no local Docker is needed, which also gives the grant time to take effect.
  Then it deploys the servers, trying once more after 90 seconds if that fails. Last, it prints
  the outputs and runs the smoke checks.
- **outputs** and **smoke** print the outputs, and run the smoke checks, again.
- **remove-functions-host** deletes what M-protected's old Azure Functions host left in the
  resource group. See [Replacing M-protected's Functions host](#replacing-m-protecteds-functions-host).

The smoke checks speak MCP as MOSAIC's client does. They expect M-tools to negotiate a revision
MOSAIC accepts and to answer all three tools, the SSE-only variant to refuse streamable HTTP but
open an SSE stream, M-protected to answer 401 without a token and with a malformed one, each
within 10 seconds, and M-agent to list `ask_model`. They never send a real token and never call
`ask_model`, so they spend no model quota.

### What it creates

In the new resource group:

- a Container Apps environment on the Consumption workload profile, whose console and system
  logs go to the workspace through a diagnostic setting;
- M-tools, its SSE-only variant, M-protected and M-agent: one replica each, always running, at
  0.25 vCPU and 0.5 GiB, the smallest size, with external HTTPS ingress. M-protected runs M-tools'
  image behind the built-in authentication [described above](#m-protected), and M-agent also has
  a system-assigned managed identity;
- a user-assigned identity the four apps pull their images with.

On MOSAIC's registry, the kit adds the AcrPull grant, the deployment record
`<namePrefix>-registry-pull` in MOSAIC's resource group, and the image repositories
`<namePrefix>/m-tools` and `<namePrefix>/m-agent`.

**Scale and cost.** One warm replica per container app keeps the servers responsive: the gateway
and MOSAIC never wait for a scale from zero, and M1's negative case can't time out. A replica
that isn't serving requests is billed at the idle rate, about $6 a month at this size, so the
four container apps cost about $24 a month, or $0.80 a day, before the Container Apps monthly free
grant. Logs and builds add cents. Setting `minReplicas` to 0 removes most of the cost, at the
price of cold starts.

### Outputs

| Output | What to do with it |
| --- | --- |
| M-tools URL | Register it with upstream authentication **None** |
| M-tools SSE-only URL | Try to register it: MOSAIC must refuse it (M1) |
| M-protected URL | Register it with upstream authentication **Managed identity** and the audience below |
| M-protected audience | `api://<audience-app-id>` |
| M-agent URL | Register it with upstream authentication **None** |
| M-agent principal ID | Assign it `Models.Invoke.Application`, then record and grant it in MOSAIC |

### Replacing M-protected's Functions host

Until October 2026 the kit ran M-protected on Azure Functions. A deployment only adds and updates
resources, so deploying this version puts the new M-protected beside the old one and leaves the
old function app, its plan, its storage account, its diagnostic setting and its two storage grants
in place. To replace it, in order:

1. Run `deploy`. It builds the images, deploys M-protected's container app with its
   authentication, and runs the smoke checks: M-protected must answer 401 within 10 seconds, both
   without a token and with a malformed one.
2. In MOSAIC, point M-protected's registration at the new **M-protected URL** from the outputs,
   with the same upstream authentication, **Managed identity**, and the same audience. Its
   connection check must pass, and syncing its tools must find `echo`, `utc_now` and `add`.
3. Run `remove-functions-host --dry-run`. It lists what's left of the old host, and the commands
   it would run to delete it. Then run `remove-functions-host`.

`remove-functions-host` finds the old host in the kit's resource group by the names the old
template gave it, and deletes exactly these, in this order:

1. the diagnostic setting `logs-to-workspace` on the function app, and the function app's two
   grants on its storage account, Storage Blob Data Owner and Storage Queue Data Contributor.
   Azure keeps both after the resources they're on are deleted, so they go first;
2. the function app, `<namePrefix>-protected-<suffix>`, where the suffix is 13 lowercase letters
   and digits;
3. its plan, `<namePrefix>-protected-plan`;
4. its storage account, `st<namePrefix without hyphens><suffix>`, cut to 24 characters;
5. the deployment record `<namePrefix>-function-app`.

It refuses a resource group the kit didn't create, as teardown does. Before it changes anything,
it also refuses whatever it can't be sure of: a match that doesn't carry the kit's tag, more than
one match of a kind, a storage account whose suffix isn't the function app's, a resource outside
the group, or grants of those two roles on the storage account when the function app has no
identity to tell its own by. It never touches the new container apps. It lists anything else named after
the old host, such as an Application Insights component made outside the kit, as left alone, for
teardown to delete with the group. Running it again finishes what an earlier run left. Neither it
nor the new host changes M-protected's audience registration or the identities that hold its role.

### Teardown

`teardown --dry-run` lists what teardown would delete and changes nothing. `teardown` deletes
exactly what deploy created:

1. the AcrPull grant on the registry;
2. the resource group, and everything in it, but only when it carries the kit's tag. That
   includes anything M-protected's old Functions host left there;
3. the deployment records `<namePrefix>-registry-pull` and `<namePrefix>-servers`.

It leaves the images in the registry and prints the `az acr repository delete` commands that
remove the two repositories it pushed. `teardown --delete-images` runs them too. Before tearing
down, unpublish the servers in MOSAIC and remove the grants for M-agent's identity. Its app role
assignment goes with its service principal when the container app is deleted.
