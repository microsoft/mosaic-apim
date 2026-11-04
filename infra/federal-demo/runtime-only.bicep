// Post-root stage: existing CAEs, registry and user-assigned identities are references only.
targetScope = 'subscription'

import { RuntimeConfigurations } from 'types.bicep'

@description('Completed Root CORE output value, not its ARM {type,value} wrapper.')
param core object
@description('Completed Root ENVIRONMENTS output value, ordered dev,stg,prod.')
@minLength(3)
@maxLength(3)
param environments array
@description('Completed Root RUNTIME_IDENTITIES output value, ordered dev,stg,prod.')
@minLength(3)
@maxLength(3)
param runtimeIdentities array
param runtimeConfigurations RuntimeConfigurations
@minLength(36)
@maxLength(36)
param modelRuntimeClientId string
param tenantId string = tenant().tenantId
@allowed(['eastus2'])
param location string = 'eastus2'
@description('False for protected-host provisioning; true only after live backend authentication checks pass.')
param backendAuthVerified bool = false

var estateEnvironments = [
  { key: 'dev', name: 'Development' }
  { key: 'stg', name: 'Staging' }
  { key: 'prod', name: 'Production' }
]
var backendAudiences = [
  runtimeConfigurations.dev.backendAudienceClientId
  runtimeConfigurations.stg.backendAudienceClientId
  runtimeConfigurations.prod.backendAudienceClientId
]
var backendAudiencesDistinct = length(union(backendAudiences, backendAudiences)) == 3
var coreScopePrefix = toLower('/subscriptions/${subscription().subscriptionId}/resourceGroups/rg-mosaic-federal-core/providers/Microsoft.ContainerRegistry/registries/')
var coreReferenceValid = core.resourceGroupName == 'rg-mosaic-federal-core' && startsWith(toLower(core.registry.id), coreScopePrefix)

module runtimes 'runtime-environment.bicep' = [for (env, i) in estateEnvironments: {
  name: 'mosaic-federal-${env.key}-runtime-only'
  scope: resourceGroup('rg-mosaic-federal-${env.key}')
  params: {
    environmentKey: env.key
    environmentName: env.name
    suffix: last(split(environments[i].gateway.name, '-'))
    location: location
    tenantId: tenantId
    tags: {
      product: 'MOSAIC'
      'mosaic-estate': 'federal-demo'
      'managed-by': 'mosaic-federal-demo'
      'mosaic-ownership': 'federal-demo-v1'
      'mosaic-environment': env.name
    }
    environmentId: environments[i].containerAppsEnvironment.id
    registryServer: core.registry.loginServer
    identities: runtimeIdentities[i]
    modelRuntimeClientId: modelRuntimeClientId
    runtimeConfiguration: runtimeConfigurations[env.key]
    backendAuthVerified: backendAuthVerified
    backendAudiencesDistinct: backendAudiencesDistinct
    referencesValidated: coreReferenceValid && environments[i].key == env.key && environments[i].resourceGroupName == 'rg-mosaic-federal-${env.key}' && runtimeIdentities[i].environment == env.key && startsWith(toLower(environments[i].containerAppsEnvironment.id), toLower('/subscriptions/${subscription().subscriptionId}/resourceGroups/rg-mosaic-federal-${env.key}/providers/Microsoft.App/managedEnvironments/')) && environments[i].gateway.name == 'apim-mosaic-fed-${env.key}-${last(split(environments[i].gateway.name, '-'))}'
  }
}]

output RUNTIMES array = [for (env, i) in estateEnvironments: {
  key: env.key
  resourceGroupName: 'rg-mosaic-federal-${env.key}'
  mcpApps: runtimes[i].outputs.mcpApps
  jobs: runtimes[i].outputs.jobs
}]
output ENVIRONMENTS array = [for (env, i) in estateEnvironments: union(environments[i], {
  mcpApps: runtimes[i].outputs.mcpApps
  jobs: runtimes[i].outputs.jobs
})]
output BACKEND_AUTH_VERIFIED bool = backendAuthVerified
