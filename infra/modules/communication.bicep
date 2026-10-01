// Azure Communication Services Email, which MOSAIC sends budget warnings and blocks through. See
// ADR 0023. MOSAIC signs in to it as its own managed identity, so the resource refuses access keys.

@description('The Communication Services resource name.')
param name string

@description('The Email Communication Services resource name.')
param emailServiceName string

@description('Where Communication Services keeps its data at rest, like United States or Europe.')
param dataLocation string

param tags object

resource emailService 'Microsoft.Communication/emailServices@2025-05-01' = {
  name: emailServiceName
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

// An Azure-managed domain sends as DoNotReply@<generated>.azurecomm.net with no DNS to set up. A
// custom domain can replace it later: MOSAIC only needs the sender address, set in Settings.
resource managedDomain 'Microsoft.Communication/emailServices/domains@2025-05-01' = {
  parent: emailService
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource communication 'Microsoft.Communication/communicationServices@2025-05-01' = {
  name: name
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    disableLocalAuth: true
    linkedDomains: [
      managedDomain.id
    ]
  }
}

output id string = communication.id
output name string = communication.name
output endpoint string = 'https://${communication.properties.hostName}'
output sender string = 'DoNotReply@${managedDomain.properties.mailFromSenderDomain}'
