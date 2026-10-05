// AcrPull on the existing registry, for the identity the container apps pull with. Deployed at the
// registry's resource group, which is MOSAIC's, not the servers'. Teardown deletes this assignment
// by the ID this module outputs.

param registryName string
param principalId string

// AcrPull
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, acrPullRoleId)
  scope: registry
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
  }
}

output roleAssignmentId string = pull.id
output loginServer string = registry.properties.loginServer
