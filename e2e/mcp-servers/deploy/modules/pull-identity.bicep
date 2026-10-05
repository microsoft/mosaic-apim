// The identity every container app pulls its image with. The registry's admin user stays off: the
// identity holds AcrPull on the registry instead, granted by registry-pull.bicep.

param location string
param tags object
param name string

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

output id string = identity.id
output principalId string = identity.properties.principalId
