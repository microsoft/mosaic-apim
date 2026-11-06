@minLength(2)
@maxLength(32)
param name string
param location string
param tags object
param environmentId string
param registryServer string
@minLength(1)
param image string
param pullIdentityId string
param runtimeIdentityId string
param runtimeIdentityClientId string
param env array

resource job 'Microsoft.App/jobs@2025-01-01' = {
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
      triggerType: 'Manual'
      replicaTimeout: 300
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: registryServer, identity: pullIdentityId }]
    }
    template: {
      containers: [{
        name: 'workload'
        image: image
        command: ['python', '-m', 'federal_runtime.workload']
        resources: { cpu: json('0.25'), memory: '0.5Gi' }
        env: concat(env, [{ name: 'AZURE_CLIENT_ID', value: runtimeIdentityClientId }])
      }]
    }
  }
}

output id string = job.id
