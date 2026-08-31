param name string
param location string
param publisherName string
param publisherEmail string
param skuName string = 'Developer'
param skuCapacity int = 1
param appInsightsConnectionString string
param appInsightsResourceId string
param tags object = {}

resource service 'Microsoft.ApiManagement/service@2022-08-01' = {
  name: name
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    capacity: skuCapacity
    name: skuName
  }
  tags: tags
  properties: {
    customProperties: {
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Protocols.Server.Http2': 'true'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Backend.Protocols.Tls11': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Ssl30': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls10': 'false'
      'Microsoft.WindowsAzure.ApiManagement.Gateway.Security.Protocols.Tls11': 'false'
    }
    publicNetworkAccess: 'Enabled'
    publisherEmail: publisherEmail
    publisherName: publisherName
  }
}

resource appInsightsLogger 'Microsoft.ApiManagement/service/loggers@2022-08-01' = {
  parent: service
  name: 'appinsights'
  properties: {
    credentials: {
      connectionString: appInsightsConnectionString
    }
    description: 'Workspace-based Application Insights logger for MOSAIC.'
    isBuffered: true
    loggerType: 'applicationInsights'
    resourceId: appInsightsResourceId
  }
}

resource applicationInsightsDiagnostic 'Microsoft.ApiManagement/service/diagnostics@2022-08-01' = {
  parent: service
  name: 'applicationinsights'
  properties: {
    alwaysLog: 'allErrors'
    backend: {
      request: {
        body: {
          bytes: 0
        }
        headers: []
      }
      response: {
        body: {
          bytes: 0
        }
        headers: []
      }
    }
    frontend: {
      request: {
        body: {
          bytes: 0
        }
        headers: []
      }
      response: {
        body: {
          bytes: 0
        }
        headers: []
      }
    }
    httpCorrelationProtocol: 'W3C'
    loggerId: appInsightsLogger.id
    metrics: true
    sampling: {
      percentage: 100
      samplingType: 'fixed'
    }
    verbosity: 'information'
  }
}

output id string = service.id
output name string = service.name
