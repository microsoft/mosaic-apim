param name string
param location string
param skuName string = 'B1'
param tags object = {}

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: name
  location: location
  kind: 'linux'
  sku: {
    capacity: 1
    name: skuName
    tier: 'Basic'
  }
  tags: tags
  properties: {
    reserved: true
  }
}

output id string = plan.id
output name string = plan.name
