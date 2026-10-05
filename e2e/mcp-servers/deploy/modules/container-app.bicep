// One MCP server on Container Apps: one replica, always on, so the gateway never waits for a cold
// start, at the smallest size Container Apps offers.

param name string
param location string
param tags object
param environmentId string
param registryServer string
param image string
param pullIdentityId string
param env array = []
@description('Give the app a system-assigned identity too, as M-agent needs for its model calls.')
param systemAssignedIdentity bool = false
@minValue(0)
@maxValue(1)
param minReplicas int = 1

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: systemAssignedIdentity ? 'SystemAssigned,UserAssigned' : 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentityId}': {}
    }
  }
  properties: {
    environmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryServer
          identity: pullIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'server'
          image: '${registryServer}/${image}'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: env
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: 1
      }
    }
  }
}

output fqdn string = app.properties.configuration.ingress.fqdn
output principalId string = systemAssignedIdentity ? app.identity.?principalId ?? '' : ''
