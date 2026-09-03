param name string
param location string
param tags object = {}

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: name
  location: location
  kind: 'GlobalDocumentDB'
  tags: tags
  properties: {
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    databaseAccountOfferType: 'Standard'
    disableLocalAuth: true
    locations: [
      {
        failoverPriority: 0
        isZoneRedundant: false
        locationName: location
      }
    ]
    minimalTlsVersion: 'Tls12'
    publicNetworkAccess: 'Enabled'
  }
}

resource sqlDatabase 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: account
  name: 'mosaic'
  properties: {
    resource: {
      id: 'mosaic'
    }
  }
}

resource desiredStateContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: sqlDatabase
  name: 'desired-state'
  properties: {
    resource: {
      id: 'desired-state'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/tenantId'
        ]
      }
    }
  }
}

resource syncOperationsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: sqlDatabase
  name: 'sync-operations'
  properties: {
    resource: {
      id: 'sync-operations'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/tenantId'
        ]
      }
    }
  }
}

resource auditEventsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: sqlDatabase
  name: 'audit-events'
  properties: {
    resource: {
      id: 'audit-events'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/tenantId'
        ]
      }
    }
  }
}

// Observed gateway inventory. Kept apart from desired state because it is disposable, rebuilt on
// every sync, and churns far more than administrator-authored governance intent.
resource observedStateContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: sqlDatabase
  name: 'observed-state'
  properties: {
    resource: {
      id: 'observed-state'
      partitionKey: {
        kind: 'Hash'
        paths: [
          '/tenantId'
        ]
      }
    }
  }
}

output id string = account.id
output endpoint string = account.properties.documentEndpoint
output name string = account.name
output databaseName string = 'mosaic'
