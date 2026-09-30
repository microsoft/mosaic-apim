# Connect to MCP servers published through MOSAIC

This guide is for people, agent builders and administrators who need to call an MCP server that
MOSAIC published through API Management. Published servers use Microsoft Entra tokens and API
Management enforcement. Imported MCP servers are different: MOSAIC records those grants, but the
imported server's own gateway policy decides who can call it.

## Before you start

You need an applied MCP grant, and the server must be published with gateway enforcement applied.
Copy these values from the grant's MCP connection details in the portal, or from
`GET /api/v1/me/entitlements/{id}/mcp-connection`:

| Field | What it's for |
| --- | --- |
| `tenantId` | The Entra tenant that issues the runtime token |
| `serverUrl` | The MCP streamable HTTP URL, ending in `/mcp` |
| `resourceMetadataUrl` | The protected resource metadata URL clients can fetch before sign-in |
| `entraAudience` | The runtime audience, normally `api://<model-runtime-client-id>` |
| `delegatedScope` | The user or agent-user scope, `api://<model-runtime-client-id>/Mcp.Invoke` |
| `applicationScope` | The application scope, `api://<model-runtime-client-id>/.default` |
| `requiredAppRole` | The app role an application or agent identity needs |
| `limits` | The call limits the gateway applies |

Connection details never include runtime tokens or upstream MCP credentials.

## People and VS Code

Add the published server URL to `.vscode/mcp.json` as an HTTP MCP server:

```json
{
  "servers": {
    "mosaic-example": {
      "type": "http",
      "url": "https://<gateway-host>/<api-path>/mcp"
    }
  }
}
```

When the client receives the gateway's `401`, it follows the `resource_metadata` URL in the
`WWW-Authenticate` header. The metadata names the tenant authorization server and the delegated
scope:

```text
api://<model-runtime-client-id>/Mcp.Invoke
```

Sign in to the tenant shown by the metadata. The VS Code Microsoft sign-in client, or any other
delegated client you use, needs consent for that scope. MOSAIC does not grant consent when it saves
your access grant. If sign-in shows **Need admin approval**, ask an administrator to consent the
client for `api://<model-runtime-client-id>/Mcp.Invoke`.

## Agent identities

An Entra Agent ID agent identity calls a published MCP server as an application. It requests:

```text
api://<model-runtime-client-id>/.default
```

The agent identity must receive the `Mcp.Invoke.Application` app role on the model-runtime
registration. The assignment can be direct to the agent identity, or inherited through the agent
identity blueprint when the runtime app is configured as an inheritable resource and the permission
is granted on the blueprint principal. Assigning the app role to a group is not enough for service
principals; app roles assigned through groups are not emitted in service-principal tokens.

Use the Agent ID token flow or SDK to get the token. Send it as a bearer token to `serverUrl`.

## Agent users

An agent user calls with delegated context through its parent agent identity. Request:

```text
api://<model-runtime-client-id>/Mcp.Invoke
```

The token must represent the agent user, and the client that obtains it is the parent agent
identity. MOSAIC grants the `agentUser` principal as a user subject, so direct grants and
security-group grants are evaluated against the token's user object ID and groups.

## Security groups

If your grant reaches you through an Entra security group, call the same `serverUrl` with an Entra
runtime token. The token must contain the group's object ID in its `groups` claim.

- People and agent users use `api://<model-runtime-client-id>/Mcp.Invoke`.
- Applications, managed identities and agent identities use
  `api://<model-runtime-client-id>/.default` and need `Mcp.Invoke.Application`.
- Security-group grants have no key path. MCP servers published through MOSAIC do not use APIM
  subscription keys.

Each member gets the group's full call allowance in the grant window. If you also have a direct
grant, the direct grant wins. If you match several groups, the gateway uses the most generous
enabled group grant.

Microsoft Entra omits `groups` from access tokens when the caller is in too many groups and marks
the token as an overage token. API Management cannot call Microsoft Graph during MCP invocation. If
your token has overage, ask for a direct grant.

## Limits

MCP grants use call limits only. The gateway can return `429` when a short request window or longer
quota is exhausted. There is no token budget for MCP calls, and MOSAIC does not inspect MCP message
bodies to count model tokens or tool payloads.

## Troubleshooting

| What you see | Cause and fix |
| --- | --- |
| HTTP 401 with `resource_metadata` | The request had no token or an invalid token. Let the client fetch the metadata URL, then sign in for the tenant and scope it returns. |
| The metadata URL does not return JSON | The server is not a MOSAIC-published MCP server, or the publication is not applied correctly. Ask an administrator to re-plan and apply the publication. |
| Consent or **Need admin approval** during sign-in | The client is not consented for `api://<model-runtime-client-id>/Mcp.Invoke`. An administrator consents that delegated scope for the client. |
| HTTP 403 with `insufficient_scope` | The token is valid, but it lacks `Mcp.Invoke`, lacks `Mcp.Invoke.Application`, or does not match an applied direct or group grant. Copy the scope from connection details and confirm the grant was applied. |
| HTTP 403 mentioning group overage | The token does not contain usable group IDs. Ask for a direct grant to the user, application or agent identity. |
| HTTP 403 **Access unavailable** | The last apply failed, the publication is not applied, or the server was unpublished. An administrator needs to review the publication status and apply access again. |
| Imported server grant is visible but the gateway denies it | Imported servers are recorded in MOSAIC but not enforced by MOSAIC. The server's existing API Management policy decides access. |

## What MOSAIC does not enforce

MOSAIC enforces only MCP servers it publishes and whose publication is applied. It does not enforce
grants on imported MCP servers, customer-owned MCP APIs, SSE-only upstreams, or API-key upstreams in
this phase.
