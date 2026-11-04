// Post-auth stage: only the same resource-scoped model assignments as the full Root profile.
targetScope = 'subscription'

param core object
@description('Completed Root ENVIRONMENTS output value, ordered dev,stg,prod.')
@minLength(3)
@maxLength(3)
param environments array
@description('Completed Root EFFECTIVE_LUNA_RESOURCE_ID value; either owned Production Luna or the reviewed reused account.')
@minLength(1)
param effectiveLunaResourceId string
@description('The exact reviewed Terra account ID from the private Root parameters.')
@minLength(1)
param reusedTerraResourceId string
@description('Required explicit assertion that backend protection checks passed. False is rejected before role writes.')
@allowed([true])
param backendAuthVerified bool

var bindings = [
  { key: 'dev-luna', environment: 'dev', accountResourceId: environments[0].model.id, gatewayPrincipalId: environments[0].gateway.principalId }
  { key: 'stg-luna', environment: 'stg', accountResourceId: environments[1].model.id, gatewayPrincipalId: environments[1].gateway.principalId }
  { key: 'prod-research', environment: 'prod', accountResourceId: environments[2].model.id, gatewayPrincipalId: environments[2].gateway.principalId }
  { key: 'prod-luna', environment: 'prod', accountResourceId: effectiveLunaResourceId, gatewayPrincipalId: environments[2].gateway.principalId }
  { key: 'prod-terra', environment: 'prod', accountResourceId: reusedTerraResourceId, gatewayPrincipalId: environments[2].gateway.principalId }
]
var accountIds = map(bindings, binding => toLower(binding.accountResourceId))
var distinctAccounts = length(union(accountIds, accountIds)) == 5
var referenceOrderValid = core.resourceGroupName == 'rg-mosaic-federal-core' && environments[0].key == 'dev' && environments[1].key == 'stg' && environments[2].key == 'prod'
var ownedAccountScopesValid = length(filter(environments, env => startsWith(toLower(env.model.id), toLower('/subscriptions/${subscription().subscriptionId}/resourceGroups/rg-mosaic-federal-${env.key}/providers/Microsoft.CognitiveServices/accounts/')))) == 3
var gatewayReferencesValid = length(filter(environments, env => startsWith(toLower(env.gateway.id), toLower('/subscriptions/${subscription().subscriptionId}/resourceGroups/rg-mosaic-federal-${env.key}/providers/Microsoft.ApiManagement/service/')) && startsWith(env.gateway.name, 'apim-mosaic-fed-${env.key}-') && length(env.gateway.principalId) == 36)) == 3

module modelAccess 'reused-model-access.bicep' = [for binding in bindings: {
  name: 'mosaic-federal-inference-${binding.key}'
  params: {
    accountResourceId: distinctAccounts && referenceOrderValid && ownedAccountScopesValid && gatewayReferencesValid ? binding.accountResourceId : ''
    apiPrincipalId: core.api.principalId
    gatewayPrincipalId: binding.gatewayPrincipalId
    enableInference: backendAuthVerified
  }
}]

output ROLE_ASSIGNMENTS array = [for (binding, i) in bindings: {
  key: binding.key
  environment: binding.environment
  accountResourceId: binding.accountResourceId
  gatewayPrincipalId: binding.gatewayPrincipalId
  reader: modelAccess[i].outputs.readerRoleAssignmentId
  inference: modelAccess[i].outputs.inferenceRoleAssignmentId
}]
