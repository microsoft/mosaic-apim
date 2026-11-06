@minLength(2)
@maxLength(32)
param name string
param location string
param tenantId string
param tags object
param environmentId string
param registryServer string
@minLength(1)
param image string
param pullIdentityId string
param runtimeIdentityId string
param runtimeIdentityClientId string
@minLength(36)
@maxLength(36)
param audienceClientId string
@minLength(2)
@maxLength(2)
param allowedClientIds array
param env array

resource app 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${pullIdentityId}': {}
      '${runtimeIdentityId}': {}
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
      registries: [{ server: registryServer, identity: pullIdentityId }]
    }
    template: {
      containers: [{
        name: 'server'
        image: image
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: concat(env, [{ name: 'AZURE_CLIENT_ID', value: runtimeIdentityClientId }])
      }]
      scale: { minReplicas: 0, maxReplicas: 1 }
    }
  }
}

// Same protected-host contract as e2e/mcp-servers/deploy, on every path with no redirects.
// Caller model grants remain an operator gate until live 401/403/allowed-caller checks pass.
resource auth 'Microsoft.App/containerApps/authConfigs@2025-01-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: { enabled: true }
    globalValidation: { unauthenticatedClientAction: 'Return401' }
    httpSettings: { requireHttps: true }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
          clientId: audienceClientId
        }
        validation: {
          allowedAudiences: [audienceClientId, 'api://${audienceClientId}']
          defaultAuthorizationPolicy: { allowedApplications: allowedClientIds }
        }
      }
    }
    login: { tokenStore: { enabled: false } }
  }
}

output url string = 'https://${app.properties.configuration.ingress.fqdn}/mcp'
output id string = app.id
