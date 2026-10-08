// The Container Apps environment, its logs, and the four servers that run on it: M-tools, its
// SSE-only variant, M-protected and M-agent. M-protected is M-tools' image under its own name,
// behind Container Apps' built-in authentication.

param location string
param tags object
param namePrefix string
param logAnalyticsWorkspaceId string
param registryServer string
param pullIdentityId string
param toolsImage string
param agentImage string
param minReplicas int

@description('M-protected: the Entra tenant that issues the tokens it accepts.')
param tenantId string
@description('M-protected: the client ID of its audience\'s app registration. Its v2 tokens name it as aud.')
param protectedAudienceClientId string
@description('M-protected: its audience\'s application ID URI, such as api://<audience-app-id>.')
param protectedAudienceAppIdUri string
@description('M-protected: the client IDs that may call it: the gateway\'s managed identity and MOSAIC API\'s.')
param protectedAllowedClientIds array

@description('The model\'s endpoint from its MOSAIC connection details: https://<gateway-host>/<api-path>.')
param modelEndpoint string
param modelDeployment string
param modelApiVersion string
param modelRuntimeClientId string
param modelCostCenter string
param modelTokenParameter string
param modelMaxTokens int

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: '${namePrefix}-env'
  location: location
  tags: tags
  properties: {
    // Console and system logs reach the workspace through the diagnostic setting below, so the
    // environment never holds the workspace's shared key.
    appLogsConfiguration: {
      destination: 'azure-monitor'
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

resource environmentLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'logs-to-workspace'
  scope: environment
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'ContainerAppConsoleLogs'
        enabled: true
      }
      {
        category: 'ContainerAppSystemLogs'
        enabled: true
      }
    ]
  }
}

module tools 'container-app.bicep' = {
  name: 'm-tools'
  params: {
    name: '${namePrefix}-tools'
    location: location
    tags: tags
    environmentId: environment.id
    registryServer: registryServer
    image: toolsImage
    pullIdentityId: pullIdentityId
    minReplicas: minReplicas
    env: [
      {
        name: 'MCP_TRANSPORT'
        value: 'streamable-http'
      }
    ]
  }
}

module toolsSse 'container-app.bicep' = {
  name: 'm-tools-sse'
  params: {
    name: '${namePrefix}-tools-sse'
    location: location
    tags: tags
    environmentId: environment.id
    registryServer: registryServer
    image: toolsImage
    pullIdentityId: pullIdentityId
    minReplicas: minReplicas
    env: [
      {
        name: 'MCP_TRANSPORT'
        value: 'sse'
      }
    ]
  }
}

var protectedName = '${namePrefix}-protected'

module protected 'container-app.bicep' = {
  name: 'm-protected'
  params: {
    name: protectedName
    location: location
    tags: tags
    environmentId: environment.id
    registryServer: registryServer
    image: toolsImage
    pullIdentityId: pullIdentityId
    minReplicas: minReplicas
    env: [
      {
        name: 'MCP_TRANSPORT'
        value: 'streamable-http'
      }
      {
        name: 'MCP_SERVER_NAME'
        value: 'M-protected'
      }
    ]
  }
}

resource protectedApp 'Microsoft.App/containerApps@2025-01-01' existing = {
  name: protectedName
}

// M-protected's upstream authentication. Every request passes through the platform's
// authentication before it reaches the container. It admits only a v2 Entra token from the
// tenant's issuer, for M-protected's audience, obtained by API Management's managed identity, which
// calls the server, or MOSAIC API's, which registers it and syncs its tools. A request without a
// valid token gets 401 on every path, never a redirect, and a valid token from any other client
// gets 403. The app never signs anyone in, so it needs no client secret: checking a bearer token
// takes only the signing keys the issuer publishes. On the app's first creation, the container can
// answer for the moments before this applies, as M-tools always does; deploy's smoke checks run
// after it.
resource protectedAuth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = {
  parent: protectedApp
  name: 'current'
  dependsOn: [
    protected
  ]
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'Return401'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${az.environment().authentication.loginEndpoint}${tenantId}/v2.0'
          clientId: protectedAudienceClientId
        }
        validation: {
          // A v2 token for api://<audience-app-id> names the client ID as its aud.
          allowedAudiences: [
            protectedAudienceClientId
            protectedAudienceAppIdUri
          ]
          defaultAuthorizationPolicy: {
            allowedApplications: protectedAllowedClientIds
          }
        }
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

var agentSettings = {
  MOSAIC_MODEL_ENDPOINT: modelEndpoint
  MOSAIC_MODEL_DEPLOYMENT: modelDeployment
  MOSAIC_MODEL_API_VERSION: modelApiVersion
  MOSAIC_RUNTIME_SCOPE: 'api://${modelRuntimeClientId}/.default'
  MOSAIC_MODEL_TOKEN_PARAMETER: modelTokenParameter
  MOSAIC_MODEL_MAX_TOKENS: string(modelMaxTokens)
}

module agent 'container-app.bicep' = {
  name: 'm-agent'
  params: {
    name: '${namePrefix}-agent'
    location: location
    tags: tags
    environmentId: environment.id
    registryServer: registryServer
    image: agentImage
    pullIdentityId: pullIdentityId
    minReplicas: minReplicas
    systemAssignedIdentity: true
    env: [
      for setting in items(empty(modelCostCenter)
        ? agentSettings
        : union(agentSettings, { MOSAIC_COST_CENTER: modelCostCenter })): {
        name: setting.key
        value: setting.value
      }
    ]
  }
}

output toolsUrl string = 'https://${tools.outputs.fqdn}/mcp'
output toolsSseUrl string = 'https://${toolsSse.outputs.fqdn}/sse'
output protectedUrl string = 'https://${protected.outputs.fqdn}/mcp'
output agentUrl string = 'https://${agent.outputs.fqdn}/mcp'
output agentPrincipalId string = agent.outputs.principalId
