// Opt-in estate. The ordinary azd entrypoint remains ../main.bicep.
// Initial deployment creates infrastructure and identities, not runnable placeholder workloads.
targetScope = 'subscription'

import { RuntimeConfigurations } from 'types.bicep'

@description('East US 2 for shared and environment resources. Only App Service hosting may use appServiceLocation.')
@allowed(['eastus2'])
param location string = 'eastus2'

@description('Region for only the B1 App Service plan and three sites. Defaults to location; Central US is the explicit US quota-recovery option.')
@allowed(['eastus2', 'centralus'])
param appServiceLocation string = location

@description('Must match the local azd environment name so normal azd service discovery/deploy still finds the shared core.')
param environmentName string = 'mosaic-federal-demo'

param tenantId string = tenant().tenantId
param apiAppClientId string
param apiServicePrincipalObjectId string
param spaAppClientId string
param portalAppClientId string
param apiApplicationIdUri string
param apiScope string
@minLength(36)
@maxLength(36)
param modelRuntimeClientId string
param modelClientId string = ''
param entraGroupClaims string = 'true'
param apimPublisherName string
param apimPublisherEmail string
param appServicePlanSku string = 'B1'

@description('Reviewed original tags to preserve ONLY on Cosmos and the three core vaults. Defaults empty. Retain the same originally approved expiration; the operator expiry fence, not this template, enforces expiry and must never renew it.')
param coreBackingResourceTags object = {}

@description('Core API image on every deployment. Bootstrap default matches main.bicep; persist the reviewed, explicitly tagged owned-ACR image in private parameters after the build to avoid reverting the site on later redeploys.')
param apiPlaceholderImage string = 'mcr.microsoft.com/azuredocs/aci-helloworld:latest'
@description('Core console image on every deployment. Bootstrap default matches main.bicep; retain the reviewed owned-ACR image in private parameters for later runtime/inference stages.')
param webPlaceholderImage string = 'mcr.microsoft.com/appsvc/staticsite:latest'
@description('Core portal image on every deployment. Bootstrap default matches main.bicep; retain the reviewed owned-ACR image in private parameters for later runtime/inference stages.')
param portalPlaceholderImage string = 'mcr.microsoft.com/appsvc/staticsite:latest'

@description('Explicit ARM ID of the reused Production Luna account in this subscription. Required by the default reuse contract; ignored when provisionProductionLuna is true. No changes are made to that reused account.')
@minLength(1)
param reusedLunaResourceId string
@description('Opt in to a new owned Production Luna account/deployment instead of the reused Luna ID. Requires fresh quota review; never recreates or mutates a missing reused account.')
param provisionProductionLuna bool = false
@description('SKU for only the new owned Production Luna deployment. GlobalStandard is an explicit reviewed quota alternative for fictional/public demo data; never resizes unrelated deployments.')
@allowed(['DataZoneStandard', 'GlobalStandard'])
param productionLunaSkuName string = 'DataZoneStandard'
@description('Explicit ARM ID of the existing Production Terra Microsoft.CognitiveServices/accounts resource in this subscription. Supplied privately; no account or deployment changes are made.')
@minLength(1)
param reusedTerraResourceId string

@description('Object ID of the approved operator. Only this principal gets onboarding-vault secret access.')
@minLength(36)
@maxLength(36)
param onboardingOperatorObjectId string
@allowed(['User', 'ServicePrincipal'])
param onboardingOperatorPrincipalType string = 'User'

@description('Opt in only after immutable images, three backend audiences, MI client IDs and published gateway routes are ready. No model-caller grants are created by this template.')
param deployRuntime bool = false
@description('Required when deployRuntime is true. Client IDs are resolved from the first deployment, never guessed from principal IDs.')
param runtimeConfigurations RuntimeConfigurations?
@description('Set true only after each protected backend has passed live anonymous-401 and wrong-caller rejection checks. Enables runtime model helpers, not model permissions.')
param backendAuthVerified bool = false
@description('Reviewed inference enablement. Requires deployRuntime and backendAuthVerified; runtime/MOSAIC model grants are a separate operator action. False does not revoke prior incremental-deployment role assignments.')
param enableGatewayInference bool = false

var suffix = substring(uniqueString(subscription().subscriptionId, 'mosaic-federal'), 0, 6)
var estateTags = {
  product: 'MOSAIC'
  'mosaic-estate': 'federal-demo'
  'managed-by': 'mosaic-federal-demo'
  'mosaic-ownership': 'federal-demo-v1'
}
var environments = [
  { key: 'dev', name: 'Development' }
  { key: 'stg', name: 'Staging' }
  { key: 'prod', name: 'Production' }
]
var devGatewayName = 'apim-mosaic-fed-dev-${suffix}'
var backendAudiences = deployRuntime ? [
  runtimeConfigurations!.dev.backendAudienceClientId
  runtimeConfigurations!.stg.backendAudienceClientId
  runtimeConfigurations!.prod.backendAudienceClientId
] : []
var backendAudiencesDistinct = !deployRuntime || length(union(backendAudiences, backendAudiences)) == 3
var effectiveLunaResourceId = provisionProductionLuna ? environmentStacks[2].outputs.productionLuna.id : reusedLunaResourceId

resource coreGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-mosaic-federal-core'
  location: location
  tags: union(estateTags, { 'mosaic-environment': 'Core' })
}

resource environmentGroups 'Microsoft.Resources/resourceGroups@2024-03-01' = [for env in environments: {
  name: 'rg-mosaic-federal-${env.key}'
  location: location
  tags: union(estateTags, { 'mosaic-environment': env.name })
}]

module core '../main.bicep' = {
  name: 'mosaic-federal-core'
  scope: coreGroup
  params: {
    environmentName: environmentName
    location: location
    appServiceLocation: appServiceLocation
    mcpDiscoveryTimeoutSeconds: 60
    additionalTags: union(estateTags, { 'mosaic-environment': 'Core' })
    coreBackingResourceTags: coreBackingResourceTags
    deployApim: false
    externalBootstrapApimResourceId: resourceId(subscription().subscriptionId, 'rg-mosaic-federal-dev', 'Microsoft.ApiManagement/service', devGatewayName)
    tenantId: tenantId
    apiAppClientId: apiAppClientId
    apiServicePrincipalObjectId: apiServicePrincipalObjectId
    spaAppClientId: spaAppClientId
    portalAppClientId: portalAppClientId
    apiApplicationIdUri: apiApplicationIdUri
    apiScope: apiScope
    modelRuntimeClientId: modelRuntimeClientId
    modelClientId: modelClientId
    entraGroupClaims: entraGroupClaims
    apimPublisherName: apimPublisherName
    apimPublisherEmail: apimPublisherEmail
    appServicePlanSku: appServicePlanSku
    apiPlaceholderImage: apiPlaceholderImage
    webPlaceholderImage: webPlaceholderImage
    portalPlaceholderImage: portalPlaceholderImage
  }
}

module onboarding 'onboarding-vault.bicep' = {
  name: 'mosaic-federal-onboarding'
  scope: coreGroup
  params: {
    name: 'kv-mf-onboard-${suffix}'
    location: location
    tenantId: tenantId
    tags: union(estateTags, { 'mosaic-environment': 'Core' }, coreBackingResourceTags)
    operatorObjectId: onboardingOperatorObjectId
    operatorPrincipalType: onboardingOperatorPrincipalType
    workspaceId: core.outputs.LOG_ANALYTICS_WORKSPACE_ID
  }
}

module environmentStacks 'environment.bicep' = [for (env, i) in environments: {
  name: 'mosaic-federal-${env.key}'
  scope: environmentGroups[i]
  params: {
    environmentKey: env.key
    environmentName: env.name
    suffix: suffix
    location: location
    tenantId: tenantId
    tags: union(estateTags, { 'mosaic-environment': env.name })
    publisherName: apimPublisherName
    publisherEmail: apimPublisherEmail
    workspaceId: core.outputs.LOG_ANALYTICS_WORKSPACE_ID
    appInsightsId: core.outputs.APPLICATION_INSIGHTS_RESOURCE_ID
    appInsightsConnectionString: core.outputs.APPLICATION_INSIGHTS_CONNECTION_STRING
    apiPrincipalId: core.outputs.API_APP_PRINCIPAL_ID
    registryServer: core.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
    modelRuntimeClientId: modelRuntimeClientId
    provisionProductionLuna: provisionProductionLuna
    productionLunaSkuName: productionLunaSkuName
    deployRuntime: deployRuntime
    backendAuthVerified: backendAuthVerified
    backendAudiencesDistinct: backendAudiencesDistinct
    runtimeConfiguration: deployRuntime ? runtimeConfigurations![env.key] : null
  }
}]

module coreAccess 'core-access.bicep' = {
  name: 'mosaic-federal-core-access'
  scope: coreGroup
  params: {
    registryName: core.outputs.AZURE_CONTAINER_REGISTRY_NAME
    backendVaultName: core.outputs.KEY_VAULT_NAME
    pullPrincipalIds: [for (env, i) in environments: environmentStacks[i].outputs.pullIdentity.principalId]
    gatewayPrincipalIds: [for (env, i) in environments: environmentStacks[i].outputs.gateway.principalId]
  }
}

// Each module is scoped to the environment that owns the account. Enabling inference cannot
// race the protected-host auth configs, because these module outputs depend on the entire stack.
module modelAccess 'model-access.bicep' = [for (env, i) in environments: {
  name: 'mosaic-federal-${env.key}-model-access'
  scope: environmentGroups[i]
  params: {
    accountName: environmentStacks[i].outputs.model.name
    apiPrincipalId: core.outputs.API_APP_PRINCIPAL_ID
    gatewayPrincipalId: environmentStacks[i].outputs.gateway.principalId
    enableInference: enableGatewayInference && deployRuntime && backendAuthVerified
  }
}]

// The Luna access contract resolves to either the unchanged reused account or the new owned
// Production account. Module outputs ensure the owned account exists before its IAM is applied.
module existingLunaAccess 'reused-model-access.bicep' = {
  name: 'mosaic-federal-existing-luna-access'
  params: {
    accountResourceId: effectiveLunaResourceId
    apiPrincipalId: core.outputs.API_APP_PRINCIPAL_ID
    gatewayPrincipalId: environmentStacks[2].outputs.gateway.principalId
    enableInference: enableGatewayInference && deployRuntime && backendAuthVerified
  }
}

module existingTerraAccess 'reused-model-access.bicep' = {
  name: 'mosaic-federal-existing-terra-access'
  params: {
    accountResourceId: reusedTerraResourceId
    apiPrincipalId: core.outputs.API_APP_PRINCIPAL_ID
    gatewayPrincipalId: environmentStacks[2].outputs.gateway.principalId
    enableInference: enableGatewayInference && deployRuntime && backendAuthVerified
  }
}

output CORE object = {
  resourceGroupName: coreGroup.name
  api: { name: core.outputs.API_APP_NAME, url: core.outputs.API_APP_URL, id: core.outputs.API_APP_RESOURCE_ID, principalId: core.outputs.API_APP_PRINCIPAL_ID }
  web: { name: core.outputs.WEB_APP_NAME, url: core.outputs.WEB_APP_URL, id: core.outputs.WEB_APP_RESOURCE_ID, principalId: core.outputs.WEB_APP_PRINCIPAL_ID }
  portal: { name: core.outputs.PORTAL_APP_NAME, url: core.outputs.PORTAL_APP_URL, id: core.outputs.PORTAL_APP_RESOURCE_ID, principalId: core.outputs.PORTAL_APP_PRINCIPAL_ID }
  registry: { name: core.outputs.AZURE_CONTAINER_REGISTRY_NAME, id: core.outputs.AZURE_CONTAINER_REGISTRY_RESOURCE_ID, loginServer: core.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT }
  workspace: { name: core.outputs.LOG_ANALYTICS_WORKSPACE_NAME, id: core.outputs.LOG_ANALYTICS_WORKSPACE_ID }
  applicationInsights: { name: core.outputs.APPLICATION_INSIGHTS_NAME, id: core.outputs.APPLICATION_INSIGHTS_RESOURCE_ID }
  backendVault: { name: core.outputs.KEY_VAULT_NAME, id: core.outputs.KEY_VAULT_RESOURCE_ID, uri: core.outputs.KEY_VAULT_URI }
  managementVault: { name: core.outputs.MANAGEMENT_KEY_VAULT_NAME, id: core.outputs.MANAGEMENT_KEY_VAULT_RESOURCE_ID, uri: core.outputs.MANAGEMENT_KEY_VAULT_URI }
  onboardingVault: onboarding.outputs.vault
}
output ENVIRONMENTS array = [for (env, i) in environments: {
  key: env.key
  name: env.name
  resourceGroupName: environmentGroups[i].name
  gateway: environmentStacks[i].outputs.gateway
  containerAppsEnvironment: environmentStacks[i].outputs.containerAppsEnvironment
  model: environmentStacks[i].outputs.model
  productionLuna: environmentStacks[i].outputs.productionLuna
  backendAudienceClientId: deployRuntime ? runtimeConfigurations![env.key].backendAudienceClientId : ''
  mcpApps: environmentStacks[i].outputs.mcpApps
  jobs: environmentStacks[i].outputs.jobs
}]
output RUNTIME_IDENTITIES array = [for (env, i) in environments: {
  environment: env.key
  pull: environmentStacks[i].outputs.pullIdentity
  mcp: environmentStacks[i].outputs.mcpIdentities
  workloads: environmentStacks[i].outputs.workloadIdentities
}]
output ROLE_ASSIGNMENTS object = {
  onboardingOperator: onboarding.outputs.operatorRoleAssignmentId
  registryPull: coreAccess.outputs.registryPullRoleAssignmentIds
  backendVaultGateways: coreAccess.outputs.backendVaultRoleAssignmentIds
  reusedLuna: {
    resourceId: effectiveLunaResourceId
    reader: existingLunaAccess.outputs.readerRoleAssignmentId
    inference: existingLunaAccess.outputs.inferenceRoleAssignmentId
  }
  reusedTerra: {
    reader: existingTerraAccess.outputs.readerRoleAssignmentId
    inference: existingTerraAccess.outputs.inferenceRoleAssignmentId
  }
}
output ENVIRONMENT_ROLE_ASSIGNMENTS array = [for (env, i) in environments: {
  environment: env.key
  gateway: environmentStacks[i].outputs.gatewayRoleAssignmentIds
  modelReader: modelAccess[i].outputs.readerRoleAssignmentId
  modelInference: modelAccess[i].outputs.inferenceRoleAssignmentId
}]
output ONBOARDING_KEY_VAULT_NAME string = onboarding.outputs.vault.name
output EFFECTIVE_LUNA_RESOURCE_ID string = effectiveLunaResourceId
output PRODUCTION_LUNA object = environmentStacks[2].outputs.productionLuna
output ONBOARDING_KEY_VAULT_URI string = onboarding.outputs.vault.uri
output ONBOARDING_KEY_VAULT_RESOURCE_ID string = onboarding.outputs.vault.id
output API_APP_NAME string = core.outputs.API_APP_NAME
output API_APP_URL string = core.outputs.API_APP_URL
output API_APP_PRINCIPAL_ID string = core.outputs.API_APP_PRINCIPAL_ID
output WEB_APP_NAME string = core.outputs.WEB_APP_NAME
output WEB_APP_URL string = core.outputs.WEB_APP_URL
output PORTAL_APP_NAME string = core.outputs.PORTAL_APP_NAME
output PORTAL_APP_URL string = core.outputs.PORTAL_APP_URL
output AZURE_CONTAINER_REGISTRY_NAME string = core.outputs.AZURE_CONTAINER_REGISTRY_NAME
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = core.outputs.AZURE_CONTAINER_REGISTRY_ENDPOINT
output LOG_ANALYTICS_WORKSPACE_ID string = core.outputs.LOG_ANALYTICS_WORKSPACE_ID
output KEY_VAULT_URI string = core.outputs.KEY_VAULT_URI
output MANAGEMENT_KEY_VAULT_URI string = core.outputs.MANAGEMENT_KEY_VAULT_URI
output APIM_NAME string = environmentStacks[0].outputs.gateway.name
output APIM_RESOURCE_ID string = environmentStacks[0].outputs.gateway.id
output APIM_PRINCIPAL_ID string = environmentStacks[0].outputs.gateway.principalId
output APIM_GATEWAY_URL string = environmentStacks[0].outputs.gateway.url
