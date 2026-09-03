targetScope = 'resourceGroup'

@description('The azd environment name.')
param environmentName string

@description('The Azure region for all resources.')
param location string = 'eastus2'

@description('The App Service Plan SKU.')
param appServicePlanSku string = 'B1'

@description('The API Management SKU in Name_Capacity format.')
param apimSkuName string = 'Developer_1'

@description('The publisher name surfaced by API Management.')
param apimPublisherName string

@description('The publisher email surfaced by API Management.')
param apimPublisherEmail string

@description('The Entra tenant identifier.')
param tenantId string

@description('The MOSAIC API application (client) ID.')
param apiAppClientId string

@description('The MOSAIC API service principal object ID.')
param apiServicePrincipalObjectId string

@description('The MOSAIC SPA application (client) ID.')
param spaAppClientId string

@description('The exposed application ID URI for the MOSAIC API.')
param apiApplicationIdUri string

@description('The delegated scope exposed by the MOSAIC API.')
param apiScope string

@description('Allowed development origins for browser-based access.')
param localhostOrigins array = [
  'http://localhost:3000'
  'http://localhost:5173'
]

@description('The API health check path.')
param apiHealthCheckPath string = '/readyz'

@description('The web health check path.')
param webHealthCheckPath string = '/healthz'

@description('Placeholder image used before azd deploy publishes the API container image.')
param apiPlaceholderImage string = 'mcr.microsoft.com/azuredocs/aci-helloworld:latest'

@description('Placeholder image used before azd deploy publishes the web container image.')
param webPlaceholderImage string = 'mcr.microsoft.com/appsvc/staticsite:latest'

@description('The API container port.')
param apiContainerPort int = 8000

@description('The web container port.')
param webContainerPort int = 8080

var normalizedEnv = toLower(replace(replace(environmentName, '_', '-'), '.', '-'))
var envLabel = startsWith(normalizedEnv, 'mosaic-') ? substring(normalizedEnv, 7) : normalizedEnv
var envToken = toLower(replace(envLabel, '-', ''))
var suffix = substring(uniqueString(subscription().subscriptionId, resourceGroup().id, normalizedEnv), 0, 6)
var sharedTags = {
  'azd-env-name': environmentName
  'managed-by': 'azd'
  'mosaic-environment': environmentName
  product: 'MOSAIC'
}
var planName = take('asp-mosaic-${envLabel}-${suffix}', 40)
var acrName = toLower(take('mosaic${envToken}${suffix}acr', 50))
var apiWebAppName = take('mosaic-${envLabel}-api-${suffix}', 60)
var webWebAppName = take('mosaic-${envLabel}-web-${suffix}', 60)
var cosmosName = take('cosmos-mosaic-${envLabel}-${suffix}', 44)
var keyVaultName = toLower(take('kvmosaic${envToken}${suffix}', 24))
var logAnalyticsName = take('log-mosaic-${envLabel}-${suffix}', 63)
var appInsightsName = take('appi-mosaic-${envLabel}-${suffix}', 260)
var apimName = take('apim-mosaic-${envLabel}-${suffix}', 50)
var apimSkuParts = split(apimSkuName, '_')
var apimSkuTier = apimSkuParts[0]
var apimSkuCapacity = int(apimSkuParts[1])
var apiUrl = 'https://${apiWebAppName}.azurewebsites.net'
var webUrl = 'https://${webWebAppName}.azurewebsites.net'
var apimGatewayUrl = 'https://${apimName}.azure-api.net'
var authorityUrl = uri(environment().authentication.loginEndpoint, tenantId)
var apiCorsAllowedOrigins = concat(localhostOrigins, [
  webUrl
])
var apiAppSettings = [
  {
    name: 'MOSAIC_ENVIRONMENT'
    value: 'azure'
  }
  {
    name: 'MOSAIC_AUTH_MODE'
    value: 'entra'
  }
  {
    name: 'MOSAIC_REPOSITORY_BACKEND'
    value: 'cosmos'
  }
  {
    name: 'MOSAIC_AUTHORITY_URL'
    value: authorityUrl
  }
  {
    name: 'MOSAIC_TENANT_ID'
    value: tenantId
  }
  {
    name: 'MOSAIC_API_CLIENT_ID'
    value: apiAppClientId
  }
  {
    name: 'MOSAIC_SPA_CLIENT_ID'
    value: spaAppClientId
  }
  {
    name: 'MOSAIC_API_APPLICATION_ID_URI'
    value: apiApplicationIdUri
  }
  {
    name: 'MOSAIC_API_SCOPE'
    value: apiScope
  }
  {
    name: 'MOSAIC_FRONTEND_URL'
    value: webUrl
  }
  {
    name: 'MOSAIC_APIM_GATEWAY_URL'
    value: apimGatewayUrl
  }
  {
    name: 'MOSAIC_COSMOS_ENDPOINT'
    value: cosmos.outputs.endpoint
  }
  {
    name: 'MOSAIC_KEY_VAULT_URI'
    value: keyVault.outputs.vaultUri
  }
  {
    name: 'MOSAIC_CORS_ORIGINS'
    value: string(apiCorsAllowedOrigins)
  }
  {
    name: 'MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: monitoring.outputs.connectionString
  }
  {
    name: 'MOSAIC_APIM_SUBSCRIPTION_ID'
    value: subscription().subscriptionId
  }
  {
    name: 'MOSAIC_APIM_RESOURCE_GROUP'
    value: resourceGroup().name
  }
  {
    name: 'MOSAIC_APIM_SERVICE_NAME'
    value: apimName
  }
  {
    name: 'MOSAIC_COSMOS_OBSERVED_STATE_CONTAINER'
    value: 'observed-state'
  }
]
var webAppSettings = [
  {
    name: 'MOSAIC_API_BASE_URL'
    value: apiUrl
  }
  {
    name: 'MOSAIC_ENTRA_TENANT_ID'
    value: tenantId
  }
  {
    name: 'MOSAIC_ENTRA_CLIENT_ID'
    value: spaAppClientId
  }
  {
    name: 'MOSAIC_ENTRA_API_SCOPE'
    value: apiScope
  }
  {
    name: 'MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: monitoring.outputs.connectionString
  }
]

module monitoring './modules/monitoring.bicep' = {
  name: 'monitoring'
  params: {
    location: location
    tags: sharedTags
    workspaceName: logAnalyticsName
    appInsightsName: appInsightsName
  }
}

module acr './modules/container-registry.bicep' = {
  name: 'containerRegistry'
  params: {
    location: location
    name: acrName
    tags: sharedTags
  }
}

module plan './modules/app-service-plan.bicep' = {
  name: 'appServicePlan'
  params: {
    location: location
    name: planName
    skuName: appServicePlanSku
    tags: sharedTags
  }
}

module cosmos './modules/cosmosdb.bicep' = {
  name: 'cosmos'
  params: {
    location: location
    name: cosmosName
    tags: sharedTags
  }
}

module keyVault './modules/key-vault.bicep' = {
  name: 'keyVault'
  params: {
    location: location
    name: keyVaultName
    tags: sharedTags
    tenantId: tenantId
  }
}

module apiApp './modules/web-app.bicep' = {
  name: 'apiApp'
  params: {
    appSettings: apiAppSettings
    azdServiceName: 'api'
    containerImage: apiPlaceholderImage
    containerPort: apiContainerPort
    corsAllowedOrigins: apiCorsAllowedOrigins
    healthCheckPath: apiHealthCheckPath
    location: location
    name: apiWebAppName
    serverFarmId: plan.outputs.id
    tags: sharedTags
  }
}

module webApp './modules/web-app.bicep' = {
  name: 'webApp'
  params: {
    appSettings: webAppSettings
    azdServiceName: 'web'
    containerImage: webPlaceholderImage
    containerPort: webContainerPort
    corsAllowedOrigins: []
    healthCheckPath: webHealthCheckPath
    location: location
    name: webWebAppName
    serverFarmId: plan.outputs.id
    tags: sharedTags
  }
}

module apim './modules/apim.bicep' = {
  name: 'apim'
  params: {
    appInsightsConnectionString: monitoring.outputs.connectionString
    appInsightsResourceId: monitoring.outputs.appInsightsId
    location: location
    name: apimName
    publisherEmail: apimPublisherEmail
    publisherName: apimPublisherName
    skuCapacity: apimSkuCapacity
    skuName: apimSkuTier
    tags: sharedTags
  }
}

resource acrResource 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource apiSiteResource 'Microsoft.Web/sites@2023-12-01' existing = {
  name: apiWebAppName
}

resource webSiteResource 'Microsoft.Web/sites@2023-12-01' existing = {
  name: webWebAppName
}

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosName
}

resource keyVaultResource 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource logAnalyticsResource 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsName
}

resource appInsightsResource 'Microsoft.Insights/components@2020-02-02' existing = {
  name: appInsightsName
}

resource apimResource 'Microsoft.ApiManagement/service@2022-08-01' existing = {
  name: apimName
}

resource apiAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrName, apiWebAppName, 'AcrPull')
  scope: acrResource
  properties: {
    principalId: apiApp.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
  dependsOn: [
    acr
  ]
}

resource webAcrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acrName, webWebAppName, 'AcrPull')
  scope: acrResource
  properties: {
    principalId: webApp.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
  dependsOn: [
    acr
  ]
}

resource apiKeyVaultSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVaultName, apiWebAppName, 'KeyVaultSecretsUser')
  scope: keyVaultResource
  properties: {
    principalId: apiApp.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
  }
  dependsOn: [
    #disable-next-line no-unnecessary-dependson
    keyVault
  ]
}

resource apiApimReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apimName, apiWebAppName, 'ApiManagementServiceReader')
  scope: apimResource
  properties: {
    principalId: apiApp.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '71522526-b88f-4d52-b57f-d31fc3546d0d')
  }
  dependsOn: [
    apim
  ]
}

resource apiLogAnalyticsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(logAnalyticsName, apiWebAppName, 'LogAnalyticsReader')
  scope: logAnalyticsResource
  properties: {
    principalId: apiApp.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
  }
  dependsOn: [
    #disable-next-line no-unnecessary-dependson
    monitoring
  ]
}

resource apiMonitoringReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(appInsightsName, apiWebAppName, 'MonitoringReader')
  scope: appInsightsResource
  properties: {
    principalId: apiApp.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '43d0d8ad-25c7-4714-9337-8ba259a9fe05')
  }
  dependsOn: [
    #disable-next-line no-unnecessary-dependson
    monitoring
  ]
}

resource cosmosDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  name: guid(cosmosName, apiWebAppName, 'SqlDataContributor')
  parent: cosmosAccount
  properties: {
    principalId: apiApp.outputs.principalId
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    scope: cosmosAccount.id
  }
  dependsOn: [
    #disable-next-line no-unnecessary-dependson
    cosmos
  ]
}

resource acrDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: acrResource
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    workspaceId: monitoring.outputs.workspaceId
  }
  dependsOn: [
    acr
  ]
}

resource cosmosDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: cosmosAccount
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    workspaceId: monitoring.outputs.workspaceId
  }
  dependsOn: [
    cosmos
  ]
}

resource keyVaultDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: keyVaultResource
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'audit'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    workspaceId: monitoring.outputs.workspaceId
  }
  dependsOn: [
    keyVault
  ]
}

resource apimDiagnosticsSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: apimResource
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    workspaceId: monitoring.outputs.workspaceId
  }
  dependsOn: [
    apim
  ]
}

resource apiSiteDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: apiSiteResource
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    workspaceId: monitoring.outputs.workspaceId
  }
  dependsOn: [
    apiApp
  ]
}

resource webSiteDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'to-log-analytics'
  scope: webSiteResource
  properties: {
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
    workspaceId: monitoring.outputs.workspaceId
  }
  dependsOn: [
    webApp
  ]
}

output API_APP_NAME string = apiApp.outputs.name
output API_APP_URL string = apiUrl
output API_APP_PRINCIPAL_ID string = apiApp.outputs.principalId
output WEB_APP_NAME string = webApp.outputs.name
output WEB_APP_URL string = webUrl
output WEB_APP_PRINCIPAL_ID string = webApp.outputs.principalId
output APIM_NAME string = apim.outputs.name
output APIM_GATEWAY_URL string = apimGatewayUrl
output APIM_RESOURCE_ID string = apim.outputs.id
output APIM_PRINCIPAL_ID string = apimResource.identity.principalId
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = acr.outputs.loginServer
output AZURE_CONTAINER_REGISTRY_NAME string = acr.outputs.name
output COSMOSDB_ACCOUNT_NAME string = cosmos.outputs.name
output COSMOSDB_ENDPOINT string = cosmos.outputs.endpoint
output KEY_VAULT_NAME string = keyVault.outputs.name
output KEY_VAULT_URI string = keyVault.outputs.vaultUri
output LOG_ANALYTICS_WORKSPACE_NAME string = monitoring.outputs.workspaceName
output LOG_ANALYTICS_WORKSPACE_ID string = monitoring.outputs.workspaceId
output APPLICATION_INSIGHTS_NAME string = monitoring.outputs.appInsightsName
output APPLICATION_INSIGHTS_RESOURCE_ID string = monitoring.outputs.appInsightsId
output MOSAIC_TENANT_ID string = tenantId
output MOSAIC_API_CLIENT_ID string = apiAppClientId
output MOSAIC_API_SCOPE string = apiScope
output MOSAIC_SPA_CLIENT_ID string = spaAppClientId
output MOSAIC_API_SERVICE_PRINCIPAL_OBJECT_ID string = apiServicePrincipalObjectId
