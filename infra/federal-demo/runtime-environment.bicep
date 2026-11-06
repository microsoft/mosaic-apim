import { RuntimeSettings } from 'types.bicep'

@allowed(['dev', 'stg', 'prod'])
param environmentKey string
param environmentName string
@minLength(6)
@maxLength(6)
param suffix string
param location string
param tenantId string
param tags object
param environmentId string
param registryServer string
param identities object
param modelRuntimeClientId string
param runtimeConfiguration RuntimeSettings
param backendAuthVerified bool = false
param backendAudiencesDistinct bool
param referencesValidated bool

// Keep these service/job definitions identical to the full environment profile.
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
var mcpIdentities = toObject(identities.mcp, entry => entry.key, entry => entry)
var workloadIdentities = toObject(identities.workloads, entry => entry.key, entry => entry)
var identityScopePrefix = toLower('${resourceGroup().id}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/')
var validMcpKeys = length(identities.mcp) == 8 && length(filter(mcpProfiles, profile => contains(mcpIdentities, profile.key))) == 8
var validWorkloadKeys = length(identities.workloads) == length(workloadProfiles) && length(filter(workloadProfiles, profile => contains(workloadIdentities, profile.key))) == length(workloadProfiles)
var validMcpScopes = length(filter(identities.mcp, entry => !startsWith(toLower(entry.id), identityScopePrefix) || length(entry.clientId) != 36)) == 0
var validWorkloadScopes = length(filter(identities.workloads, entry => !startsWith(toLower(entry.id), identityScopePrefix) || length(entry.clientId) != 36)) == 0
var validPullScope = startsWith(toLower(identities.pull.id), identityScopePrefix)
var referencesValid = referencesValidated && validMcpKeys && validWorkloadKeys && validMcpScopes && validWorkloadScopes && validPullScope
var gatewayRoutePrefix = 'https://apim-mosaic-fed-${environmentKey}-${suffix}.azure-api.net/'
var runtimeImage = runtimeConfiguration.image
var runtimeImageHasVersion = contains(runtimeImage, '@sha256:') || contains(last(split(runtimeImage, '/')), ':')
var runtimeImageValid = referencesValid && startsWith(runtimeImage, '${registryServer}/') && runtimeImageHasVersion && !endsWith(runtimeImage, ':latest')
var runtimeModelRouteValid = startsWith(runtimeConfiguration.modelEndpoint, gatewayRoutePrefix)
var runtimeWorkloads object = runtimeConfiguration.?workloads ?? {}

module mcpApps 'protected-mcp.bicep' = [for profile in mcpProfiles: {
  name: 'mcp-${profile.shortKey}'
  params: {
    name: 'mcp-mf-${environmentKey}-${profile.shortKey}'
    location: location
    tenantId: tenantId
    tags: tags
    environmentId: environmentId
    registryServer: registryServer
    image: runtimeImageValid && runtimeModelRouteValid ? runtimeImage : ''
    pullIdentityId: identities.pull.id
    runtimeIdentityId: mcpIdentities[profile.key].id
    runtimeIdentityClientId: mcpIdentities[profile.key].clientId
    audienceClientId: backendAudiencesDistinct ? runtimeConfiguration.backendAudienceClientId : ''
    allowedClientIds: [
      runtimeConfiguration.gatewayClientId
      runtimeConfiguration.mosaicApiClientId
    ]
    env: [
      { name: 'FEDERAL_MCP_PROFILE', value: profile.key }
      { name: 'FEDERAL_ENVIRONMENT', value: environmentName }
      { name: 'PORT', value: '8000' }
      { name: 'FEDERAL_BACKEND_AUTH_VERIFIED', value: backendAuthVerified ? 'true' : 'false' }
      { name: 'MOSAIC_MODEL_ENDPOINT', value: runtimeConfiguration.modelEndpoint }
      { name: 'MOSAIC_MODEL_DEPLOYMENT', value: runtimeConfiguration.modelDeployment }
      { name: 'MOSAIC_MODEL_API_VERSION', value: runtimeConfiguration.?modelApiVersion ?? '2024-10-21' }
      { name: 'MOSAIC_RUNTIME_SCOPE', value: 'api://${modelRuntimeClientId}/.default' }
      { name: 'MOSAIC_COST_CENTER', value: profile.costCenter }
      { name: 'MOSAIC_MODEL_TOKEN_PARAMETER', value: 'max_completion_tokens' }
      { name: 'MOSAIC_MODEL_MAX_TOKENS', value: '256' }
    ]
  }
}]

module jobs 'manual-job.bicep' = [for profile in workloadProfiles: {
  name: 'job-${profile.key}'
  params: {
    name: 'job-mf-prod-${profile.key}-${suffix}'
    location: location
    tags: tags
    environmentId: environmentId
    registryServer: registryServer
    image: runtimeImageValid && startsWith(runtimeWorkloads[?profile.key].?modelEndpoint ?? '', gatewayRoutePrefix) && startsWith(runtimeWorkloads[?profile.key].?mcpGatewayUrl ?? '', gatewayRoutePrefix) ? runtimeImage : ''
    pullIdentityId: identities.pull.id
    runtimeIdentityId: workloadIdentities[profile.key].id
    runtimeIdentityClientId: workloadIdentities[profile.key].clientId
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

output mcpApps array = [for (profile, i) in mcpProfiles: {
  key: profile.key
  name: 'mcp-mf-${environmentKey}-${profile.shortKey}'
  id: mcpApps[i].outputs.id
  url: mcpApps[i].outputs.url
  deployed: true
}]
output jobs array = [for (profile, i) in workloadProfiles: {
  key: profile.key
  name: 'job-mf-prod-${profile.key}-${suffix}'
  id: jobs[i].outputs.id
  deployed: true
}]
