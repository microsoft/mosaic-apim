param registryName string
param backendVaultName string
param pullPrincipalIds array
param gatewayPrincipalIds array

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource backendVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: backendVaultName
}

resource registryPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in pullPrincipalIds: {
  name: guid(registry.id, principalId, 'AcrPull')
  scope: registry
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}]

resource backendRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principalId in gatewayPrincipalIds: {
  name: guid(backendVault.id, principalId, 'KeyVaultSecretsUser')
  scope: backendVault
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
}]

output registryPullRoleAssignmentIds array = [for (principalId, i) in pullPrincipalIds: registryPull[i].id]
output backendVaultRoleAssignmentIds array = [for (principalId, i) in gatewayPrincipalIds: backendRead[i].id]
