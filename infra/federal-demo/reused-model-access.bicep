targetScope = 'subscription'

@description('Canonical account resource ID: /subscriptions/<current-subscription>/resourceGroups/<group>/providers/Microsoft.CognitiveServices/accounts/<account>.')
@minLength(1)
param accountResourceId string
param apiPrincipalId string
param gatewayPrincipalId string
param enableInference bool = false

var resourceIdParts = split(accountResourceId, '/')
var suppliedSubscriptionId = resourceIdParts[?2] ?? ''
var suppliedResourceGroupName = resourceIdParts[?4] ?? ''
var suppliedAccountName = resourceIdParts[?8] ?? ''
var validShape = length(resourceIdParts) == 9 && (resourceIdParts[?0] ?? '!') == '' && !contains(accountResourceId, '?') && !contains(accountResourceId, '#')
var validProvider = toLower(resourceIdParts[?5] ?? '') == 'providers' && toLower(resourceIdParts[?6] ?? '') == 'microsoft.cognitiveservices' && toLower(resourceIdParts[?7] ?? '') == 'accounts'
var validScope = toLower(resourceIdParts[?1] ?? '') == 'subscriptions' && toLower(suppliedSubscriptionId) == toLower(subscription().subscriptionId) && toLower(resourceIdParts[?3] ?? '') == 'resourcegroups'
var validNames = length(suppliedResourceGroupName) >= 1 && length(suppliedResourceGroupName) <= 90 && length(suppliedAccountName) >= 2 && length(suppliedAccountName) <= 64
var validAccountResourceId = validShape && validProvider && validScope && validNames

// Invalid IDs deliberately fail the nested account-name parameter's minLength validation.
// Never silently skip access or reinterpret an ID from another subscription as a local account.
module access 'model-access.bicep' = {
  name: 'mosaic-federal-model-access-${uniqueString(accountResourceId)}'
  scope: resourceGroup(suppliedResourceGroupName)
  params: {
    accountName: validAccountResourceId ? suppliedAccountName : ''
    apiPrincipalId: apiPrincipalId
    gatewayPrincipalId: gatewayPrincipalId
    enableInference: enableInference
  }
}

output readerRoleAssignmentId string = access.outputs.readerRoleAssignmentId
output inferenceRoleAssignmentId string = access.outputs.inferenceRoleAssignmentId
