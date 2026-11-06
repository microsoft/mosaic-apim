param name string
param location string
param tenantId string
param tags object
param operatorObjectId string
@allowed(['User', 'ServicePrincipal'])
param operatorPrincipalType string
param workspaceId string

module onboarding '../modules/key-vault.bicep' = {
  name: 'onboarding-vault'
  params: {
    name: name
    location: location
    tenantId: tenantId
    tags: tags
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: name
}

// Deliberately separate from both MOSAIC vaults. No API/gateway/runtime role is granted here.
resource operatorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, operatorObjectId, 'KeyVaultSecretsOfficer')
  scope: vault
  properties: {
    principalId: operatorObjectId
    principalType: operatorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  }
  dependsOn: [onboarding]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: vault
  properties: {
    workspaceId: workspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [{ categoryGroup: 'audit', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
  dependsOn: [onboarding]
}

output vault object = {
  name: onboarding.outputs.name
  id: onboarding.outputs.id
  uri: onboarding.outputs.vaultUri
}
output operatorRoleAssignmentId string = operatorAccess.id
