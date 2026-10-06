// M-protected: a Python function app on the Flex Consumption plan, the serverless plan that runs
// the MCP extension and App Service Authentication (Easy Auth). Easy Auth admits only an Entra
// token for the server's own audience, from an allowed client application: API Management's
// managed identity, which calls the server, and MOSAIC API's, which registers it and syncs its
// tools. Everything else gets 401 or 403 before the Functions host sees the request.

param location string
param tags object
param namePrefix string
param logAnalyticsWorkspaceId string

@description('The Entra tenant that issues the tokens.')
param tenantId string
@description('The client ID of the audience\'s app registration. Its v2 tokens name it as aud.')
param audienceClientId string
@description('The audience\'s application ID URI, such as api://<audience-app-id>.')
param audienceAppIdUri string
@description('The client IDs that may call: the gateway\'s managed identity and MOSAIC API\'s.')
param allowedClientIds array

var suffix = uniqueString(resourceGroup().id)
var functionAppName = '${namePrefix}-protected-${suffix}'
var deploymentContainer = 'app-package'
// Storage Blob Data Owner and Storage Queue Data Contributor: what the Functions host and the MCP
// extension need on their storage account, with identity-based connections.
var storageRoleIds = [
  'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
]

resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: take('st${replace(namePrefix, '-', '')}${suffix}', 24)
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storage
  name: 'default'
}

resource packageContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: deploymentContainer
  properties: {
    publicAccess: 'None'
  }
}

resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: '${namePrefix}-protected-plan'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    tier: 'FlexConsumption'
    name: 'FC1'
  }
  properties: {
    reserved: true
  }
}

resource site 'Microsoft.Web/sites@2024-11-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      minTlsVersion: '1.2'
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storage.properties.primaryEndpoints.blob}${deploymentContainer}'
          authentication: {
            type: 'SystemAssignedIdentity'
          }
        }
      }
      scaleAndConcurrency: {
        // The smallest instance, and one at most: enough for a test run's few calls.
        instanceMemoryMB: 512
        maximumInstanceCount: 1
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
    }
  }
}

resource storageRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleId in storageRoleIds: {
    name: guid(storage.id, site.id, roleId)
    scope: storage
    properties: {
      principalId: site.identity.principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
    }
  }
]

resource appSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: site
  name: 'appsettings'
  properties: {
    AzureWebJobsStorage__accountName: storage.name
    WEBSITE_AUTH_AAD_ALLOWED_TENANTS: tenantId
  }
}

resource easyAuth 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: site
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: true
      unauthenticatedClientAction: 'Return401'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
          clientId: audienceClientId
        }
        validation: {
          allowedAudiences: [
            audienceClientId
            audienceAppIdUri
          ]
          defaultAuthorizationPolicy: {
            allowedApplications: allowedClientIds
          }
        }
        isAutoProvisioned: false
      }
    }
    login: {
      tokenStore: {
        enabled: false
      }
    }
  }
}

resource siteLogs 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'logs-to-workspace'
  scope: site
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'FunctionAppLogs'
        enabled: true
      }
    ]
  }
}

output functionAppName string = site.name
output protectedUrl string = 'https://${site.properties.defaultHostName}/runtime/webhooks/mcp'
