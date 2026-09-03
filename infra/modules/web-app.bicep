param name string
param location string
param serverFarmId string
param containerImage string
param containerPort int
param healthCheckPath string
param azdServiceName string
param appSettings array = []
param corsAllowedOrigins array = []
param tags object = {}

var siteAppSettings = concat(appSettings, [
  {
    name: 'WEBSITES_PORT'
    value: string(containerPort)
  }
])

resource site 'Microsoft.Web/sites@2023-12-01' = {
  name: name
  location: location
  kind: 'app,linux,container'
  identity: {
    type: 'SystemAssigned'
  }
  tags: union(tags, {
    'azd-service-name': azdServiceName
  })
  properties: {
    clientAffinityEnabled: false
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    serverFarmId: serverFarmId
    siteConfig: {
      acrUseManagedIdentityCreds: true
      alwaysOn: true
      appSettings: siteAppSettings
      cors: {
        allowedOrigins: corsAllowedOrigins
        supportCredentials: false
      }
      ftpsState: 'Disabled'
      healthCheckPath: healthCheckPath
      http20Enabled: true
      linuxFxVersion: 'DOCKER|${containerImage}'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      use32BitWorkerProcess: false
      webSocketsEnabled: false
    }
  }
}

output id string = site.id
output name string = site.name
output defaultHostName string = site.properties.defaultHostName
output principalId string = site.identity.principalId
