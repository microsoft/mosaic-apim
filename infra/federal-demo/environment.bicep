import { RuntimeSettings } from 'types.bicep'

@allowed(['dev', 'stg', 'prod'])
param environmentKey string
@allowed(['Development', 'Staging', 'Production'])
param environmentName string
@minLength(6)
@maxLength(6)
param suffix string
param location string
param tenantId string
param tags object
param publisherName string
param publisherEmail string
param workspaceId string
param appInsightsId string
param appInsightsConnectionString string
param apiPrincipalId string
param registryServer string
param modelRuntimeClientId string
param provisionProductionLuna bool = false
@allowed(['DataZoneStandard', 'GlobalStandard'])
param productionLunaSkuName string = 'DataZoneStandard'
param deployRuntime bool = false
param runtimeConfiguration RuntimeSettings?
param backendAuthVerified bool = false
param backendAudiencesDistinct bool = true

var apimName = 'apim-mosaic-fed-${environmentKey}-${suffix}'
var modelName = environmentKey == 'prod' ? 'gpt-5.1' : 'gpt-5.6-luna'
var createProductionLuna = provisionProductionLuna && environmentKey == 'prod'
var runtimeImage = runtimeConfiguration.?image ?? ''
// Module parameter validation rejects an empty image before creating runnable apps/jobs.
var runtimeImageHasVersion = contains(runtimeImage, '@sha256:') || contains(last(split(runtimeImage, '/')), ':')
var runtimeImageValid = startsWith(runtimeImage, '${registryServer}/') && runtimeImageHasVersion && !endsWith(runtimeImage, ':latest')
var gatewayRoutePrefix = 'https://${apimName}.azure-api.net/'
var runtimeModelRouteValid = startsWith(runtimeConfiguration.?modelEndpoint ?? '', gatewayRoutePrefix)
var runtimeWorkloads object = runtimeConfiguration.?workloads ?? {}
var mcpProfiles = [
  { key: 'doe-energy-data', shortKey: 'doe-energy', costCenter: 'DOE-LABS' }
  { key: 'doe-lab-research', shortKey: 'doe-research', costCenter: 'DOE-LABS' }
  { key: 'faa-airport-reference', shortKey: 'faa-airports', costCenter: 'FAA-RND' }
  { key: 'faa-regulatory-reference', shortKey: 'faa-guidance', costCenter: 'FAA-RND' }
  { key: 'usda-crop-insights', shortKey: 'usda-crops', costCenter: 'USDA-DATA' }
  { key: 'usda-conservation-guide', shortKey: 'usda-conserve', costCenter: 'USDA-DATA' }
  { key: 'nasa-mission-archive', shortKey: 'nasa-missions', costCenter: 'NASA-MISSION' }
  { key: 'nasa-earthdata-explorer', shortKey: 'nasa-earthdata', costCenter: 'NASA-MISSION' }
]
var workloadProfiles = environmentKey == 'prod' ? [
  { key: 'nasa-brief', costCenter: 'NASA-MISSION' }
  { key: 'faa-airspace', costCenter: 'FAA-OPS' }
  { key: 'faa-standards', costCenter: 'FAA-RND' }
  { key: 'doe-research', costCenter: 'DOE-LABS' }
  { key: 'usda-field', costCenter: 'USDA-DATA' }
] : []

module gateway '../modules/apim.bicep' = {
  name: 'gateway'
  params: {
    name: apimName
    location: location
    publisherName: publisherName
    publisherEmail: publisherEmail
    skuName: 'Developer'
    skuCapacity: 1
    appInsightsConnectionString: appInsightsConnectionString
    appInsightsResourceId: appInsightsId
    tags: tags
  }
}

resource gatewayResource 'Microsoft.ApiManagement/service@2022-08-01' existing = {
  name: apimName
  // ARM v2 reads existing aliases independently; first-create must wait for the creating module.
  dependsOn: [gateway]
}

resource apiGatewayContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gatewayResource.id, apiPrincipalId, 'ApiManagementServiceContributor')
  scope: gatewayResource
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '312a565d-c81f-4fd8-895a-4e21e48d571c')
  }
  dependsOn: [gateway]
}

resource apiGatewayMonitoring 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(gatewayResource.id, apiPrincipalId, 'MonitoringReader')
  scope: gatewayResource
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '43d0d8ad-25c7-4714-9337-8ba259a9fe05')
  }
  dependsOn: [gateway]
}

resource gatewayDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: gatewayResource
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
  dependsOn: [gateway]
}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-mosaic-fed-${environmentKey}-${suffix}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: 'ai-mosaic-fed-${environmentKey}-${suffix}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow' }
  }
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelName
  sku: {
    name: environmentKey == 'prod' ? 'DataZoneStandard' : 'GlobalStandard'
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: modelName
      version: environmentKey == 'prod' ? '2025-11-13' : '2026-07-09'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource productionLuna 'Microsoft.CognitiveServices/accounts@2025-06-01' = if (createProductionLuna) {
  name: 'ai-mosaic-fed-prod-luna-${suffix}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: 'ai-mosaic-fed-prod-luna-${suffix}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    networkAcls: { defaultAction: 'Allow' }
  }
}

resource productionLunaDeployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = if (createProductionLuna) {
  parent: productionLuna
  name: 'gpt-5.6-luna'
  sku: {
    name: productionLunaSkuName
    capacity: 10
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: 'gpt-5.6-luna'
      version: '2026-07-09'
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource containerEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-mf-${environmentKey}-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: { destination: 'azure-monitor' }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
    zoneRedundant: false
  }
}

resource environmentLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'logs-to-workspace'
  scope: containerEnvironment
  properties: {
    workspaceId: workspaceId
    logs: [
      { category: 'ContainerAppConsoleLogs', enabled: true }
      { category: 'ContainerAppSystemLogs', enabled: true }
    ]
  }
}

resource pullIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-mf-${environmentKey}-pull-${suffix}'
  location: location
  tags: tags
}

resource mcpIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for profile in mcpProfiles: {
  name: 'id-mf-${environmentKey}-${profile.shortKey}-${suffix}'
  location: location
  tags: tags
}]

resource workloadIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for profile in workloadProfiles: {
  name: 'id-mf-prod-job-${profile.key}-${suffix}'
  location: location
  tags: tags
}]

module mcpApps 'protected-mcp.bicep' = [for (profile, i) in mcpProfiles: if (deployRuntime) {
  name: 'mcp-${profile.shortKey}'
  params: {
    name: 'mcp-mf-${environmentKey}-${profile.shortKey}'
    location: location
    tenantId: tenantId
    tags: tags
    environmentId: containerEnvironment.id
    registryServer: registryServer
    image: runtimeImageValid && runtimeModelRouteValid ? runtimeImage : ''
    pullIdentityId: pullIdentity.id
    runtimeIdentityId: mcpIdentities[i].id
    runtimeIdentityClientId: mcpIdentities[i].properties.clientId
    audienceClientId: backendAudiencesDistinct ? runtimeConfiguration.?backendAudienceClientId ?? '' : ''
    allowedClientIds: [
      runtimeConfiguration.?gatewayClientId ?? ''
      runtimeConfiguration.?mosaicApiClientId ?? ''
    ]
    env: [
      { name: 'FEDERAL_MCP_PROFILE', value: profile.key }
      { name: 'FEDERAL_ENVIRONMENT', value: environmentName }
      { name: 'PORT', value: '8000' }
      { name: 'FEDERAL_BACKEND_AUTH_VERIFIED', value: backendAuthVerified ? 'true' : 'false' }
      { name: 'MOSAIC_MODEL_ENDPOINT', value: runtimeConfiguration.?modelEndpoint ?? '' }
      { name: 'MOSAIC_MODEL_DEPLOYMENT', value: runtimeConfiguration.?modelDeployment ?? '' }
      { name: 'MOSAIC_MODEL_API_VERSION', value: runtimeConfiguration.?modelApiVersion ?? '2024-10-21' }
      { name: 'MOSAIC_RUNTIME_SCOPE', value: 'api://${modelRuntimeClientId}/.default' }
      { name: 'MOSAIC_COST_CENTER', value: profile.costCenter }
      { name: 'MOSAIC_MODEL_TOKEN_PARAMETER', value: 'max_completion_tokens' }
      { name: 'MOSAIC_MODEL_MAX_TOKENS', value: '256' }
    ]
  }
}]

module jobs 'manual-job.bicep' = [for (profile, i) in workloadProfiles: if (deployRuntime) {
  name: 'job-${profile.key}'
  params: {
    name: 'job-mf-prod-${profile.key}-${suffix}'
    location: location
    tags: tags
    environmentId: containerEnvironment.id
    registryServer: registryServer
    image: runtimeImageValid && startsWith(runtimeWorkloads[?profile.key].?modelEndpoint ?? '', gatewayRoutePrefix) && startsWith(runtimeWorkloads[?profile.key].?mcpGatewayUrl ?? '', gatewayRoutePrefix) ? runtimeImage : ''
    pullIdentityId: pullIdentity.id
    runtimeIdentityId: workloadIdentities[i].id
    runtimeIdentityClientId: workloadIdentities[i].properties.clientId
    env: [
      { name: 'FEDERAL_WORKLOAD_PROFILE', value: profile.key }
      { name: 'FEDERAL_ENVIRONMENT', value: environmentName }
      { name: 'MOSAIC_MODEL_ENDPOINT', value: runtimeWorkloads[?profile.key].?modelEndpoint ?? '' }
      { name: 'MOSAIC_MODEL_DEPLOYMENT', value: runtimeWorkloads[?profile.key].?modelDeployment ?? '' }
      { name: 'MOSAIC_MCP_GATEWAY_URL', value: runtimeWorkloads[?profile.key].?mcpGatewayUrl ?? '' }
      { name: 'MOSAIC_MODEL_API_VERSION', value: runtimeConfiguration.?modelApiVersion ?? '2024-10-21' }
      { name: 'MOSAIC_RUNTIME_SCOPE', value: 'api://${modelRuntimeClientId}/.default' }
      { name: 'MOSAIC_COST_CENTER', value: profile.costCenter }
      { name: 'MOSAIC_MODEL_TOKEN_PARAMETER', value: 'max_completion_tokens' }
      { name: 'MOSAIC_MODEL_MAX_TOKENS', value: '256' }
    ]
  }
}]

output gateway object = {
  id: gateway.outputs.id
  name: gateway.outputs.name
  url: 'https://${apimName}.azure-api.net'
  principalId: gateway.outputs.principalId
}
output model object = {
  name: account.name
  id: account.id
  endpoint: account.properties.endpoint
  deployment: modelDeployment.name
  model: modelName
  version: modelDeployment.properties.model.version
  sku: modelDeployment.sku.name
  capacity: modelDeployment.sku.capacity
}
output productionLuna object = createProductionLuna ? {
  owned: true
  name: productionLuna!.name
  id: productionLuna!.id
  endpoint: productionLuna!.properties.endpoint
  deployment: productionLunaDeployment!.name
  model: 'gpt-5.6-luna'
  version: '2026-07-09'
  sku: productionLunaSkuName
  capacity: 10
} : {}
output containerAppsEnvironment object = { id: containerEnvironment.id, name: containerEnvironment.name }
output pullIdentity object = { id: pullIdentity.id, name: pullIdentity.name, clientId: pullIdentity.properties.clientId, principalId: pullIdentity.properties.principalId }
output mcpIdentities array = [for (profile, i) in mcpProfiles: {
  key: profile.key
  id: mcpIdentities[i].id
  name: mcpIdentities[i].name
  clientId: mcpIdentities[i].properties.clientId
  principalId: mcpIdentities[i].properties.principalId
}]
output workloadIdentities array = [for (profile, i) in workloadProfiles: {
  key: profile.key
  costCenter: profile.costCenter
  id: workloadIdentities[i].id
  name: workloadIdentities[i].name
  clientId: workloadIdentities[i].properties.clientId
  principalId: workloadIdentities[i].properties.principalId
}]
output mcpApps array = [for (profile, i) in mcpProfiles: {
  key: profile.key
  name: 'mcp-mf-${environmentKey}-${profile.shortKey}'
  url: deployRuntime ? mcpApps[i]!.outputs.url : ''
  deployed: deployRuntime
}]
output jobs array = [for (profile, i) in workloadProfiles: {
  key: profile.key
  name: 'job-mf-prod-${profile.key}-${suffix}'
  id: deployRuntime ? jobs[i]!.outputs.id : ''
  deployed: deployRuntime
}]
output gatewayRoleAssignmentIds array = [
  apiGatewayContributor.id
  apiGatewayMonitoring.id
]
