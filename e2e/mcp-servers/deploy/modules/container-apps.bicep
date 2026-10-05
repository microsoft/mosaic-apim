// The Container Apps environment, its logs, and the three servers that run on it: M-tools, its
// SSE-only variant, and M-agent.

param location string
param tags object
param namePrefix string
param logAnalyticsWorkspaceId string
param registryServer string
param pullIdentityId string
param toolsImage string
param agentImage string
param minReplicas int

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
output agentUrl string = 'https://${agent.outputs.fqdn}/mcp'
output agentPrincipalId string = agent.outputs.principalId
