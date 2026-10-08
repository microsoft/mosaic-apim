// The Phase 11 MCP test servers, in a dedicated resource group the deployment creates.
//
// MOSAIC's container registry and Log Analytics workspace stay where they are, in MOSAIC's
// resource group: the servers' images are pulled from that registry with a managed identity, and
// every server's logs go to that workspace. deploy.py runs this template; see the README.
targetScope = 'subscription'

@description('The dedicated resource group to create for the servers. Teardown deletes it.')
param resourceGroupName string

@description('The region for every server, such as eastus2.')
param location string

@description('Starts every resource name, and the image repositories: lowercase letters, digits and hyphens.')
@minLength(3)
@maxLength(16)
param namePrefix string = 'mosaic-mcp'

@description('Resource ID of the existing Log Analytics workspace every server logs to.')
param logAnalyticsWorkspaceId string

@description('Resource ID of the existing container registry the images are built in.')
param containerRegistryId string

@description('The tag the images were built with.')
param imageTag string

@description('False deploys only the resource group, the pull identity and its AcrPull grant.')
param deployServers bool = true

@description('M-protected: the Entra tenant that issues the tokens it accepts.')
param tenantId string = tenant().tenantId

@description('M-protected: the client ID of its audience\'s app registration.')
param protectedAudienceClientId string

@description('M-protected: its audience\'s application ID URI.')
param protectedAudienceAppIdUri string = 'api://${protectedAudienceClientId}'

@description('M-protected: the client ID of API Management\'s system-assigned managed identity.')
param gatewayClientId string

@description('M-protected: the client ID of MOSAIC API\'s system-assigned managed identity.')
param mosaicApiClientId string

@description('M-agent: the model\'s endpoint from its MOSAIC connection details.')
param modelEndpoint string

@description('M-agent: the model\'s deployment name from its MOSAIC connection details.')
param modelDeployment string

@description('M-agent: the Azure OpenAI api-version for chat completions.')
param modelApiVersion string = '2024-10-21'

@description('M-agent: the client ID of MOSAIC\'s model runtime registration.')
param modelRuntimeClientId string

@description('M-agent: a cost center code to name on each model call, or empty for none.')
param modelCostCenter string = ''

@description('M-agent: the chat completions parameter that caps the answer\'s length.')
@allowed([
  'max_tokens'
  'max_completion_tokens'
])
param modelTokenParameter string = 'max_tokens'

@description('M-agent: the most tokens each answer may use.')
@minValue(1)
@maxValue(256)
param modelMaxTokens int = 16

@description('Replicas each container app keeps running. 1 keeps the servers warm for the gateway.')
@minValue(0)
@maxValue(1)
param minReplicas int = 1

// Teardown deletes the resource group only when it carries this tag.
var tags = {
  'mosaic-e2e-kit': 'mcp-test-servers'
}
var registry = split(containerRegistryId, '/')
var toolsImage = '${namePrefix}/m-tools:${imageTag}'
var agentImage = '${namePrefix}/m-agent:${imageTag}'

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module pullIdentity 'modules/pull-identity.bicep' = {
  name: '${namePrefix}-pull-identity'
  scope: group
  params: {
    name: '${namePrefix}-pull'
    location: location
    tags: tags
  }
}

module registryPull 'modules/registry-pull.bicep' = {
  name: '${namePrefix}-registry-pull'
  scope: resourceGroup(registry[2], registry[4])
  params: {
    registryName: registry[8]
    principalId: pullIdentity.outputs.principalId
  }
}

module containerApps 'modules/container-apps.bicep' = if (deployServers) {
  name: '${namePrefix}-container-apps'
  scope: group
  params: {
    location: location
    tags: tags
    namePrefix: namePrefix
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
    registryServer: registryPull.outputs.loginServer
    pullIdentityId: pullIdentity.outputs.id
    toolsImage: toolsImage
    agentImage: agentImage
    minReplicas: minReplicas
    tenantId: tenantId
    protectedAudienceClientId: protectedAudienceClientId
    protectedAudienceAppIdUri: protectedAudienceAppIdUri
    protectedAllowedClientIds: [
      gatewayClientId
      mosaicApiClientId
    ]
    modelEndpoint: modelEndpoint
    modelDeployment: modelDeployment
    modelApiVersion: modelApiVersion
    modelRuntimeClientId: modelRuntimeClientId
    modelCostCenter: modelCostCenter
    modelTokenParameter: modelTokenParameter
    modelMaxTokens: modelMaxTokens
  }
}

output resourceGroupName string = group.name
output registryName string = registry[8]
output toolsImage string = toolsImage
output agentImage string = agentImage
output acrPullRoleAssignmentId string = registryPull.outputs.roleAssignmentId
output toolsUrl string = containerApps.?outputs.toolsUrl ?? ''
output toolsSseUrl string = containerApps.?outputs.toolsSseUrl ?? ''
output agentUrl string = containerApps.?outputs.agentUrl ?? ''
output agentPrincipalId string = containerApps.?outputs.agentPrincipalId ?? ''
output protectedUrl string = containerApps.?outputs.protectedUrl ?? ''
output protectedAudience string = protectedAudienceAppIdUri
