@minLength(2)
@maxLength(64)
param accountName string
param apiPrincipalId string
param gatewayPrincipalId string
param enableInference bool = false

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource reader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, apiPrincipalId, 'Reader')
  scope: account
  properties: {
    principalId: apiPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
  }
}

resource inference 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (enableInference) {
  name: guid(account.id, gatewayPrincipalId, 'CognitiveServicesOpenAIUser')
  scope: account
  properties: {
    principalId: gatewayPrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  }
}

output readerRoleAssignmentId string = reader.id
output inferenceRoleAssignmentId string = enableInference ? inference!.id : ''
