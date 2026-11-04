@export()
type RuntimeSettings = {
  @description('Built, non-placeholder image URI in the core ACR, including an explicit version tag or digest; latest is rejected.')
  @minLength(1)
  image: string
  @minLength(36)
  @maxLength(36)
  backendAudienceClientId: string
  @description('The matching APIM system-assigned identity CLIENT ID, not its principal/object ID.')
  @minLength(36)
  @maxLength(36)
  gatewayClientId: string
  @description('The MOSAIC API system-assigned identity CLIENT ID, not the API registration client ID.')
  @minLength(36)
  @maxLength(36)
  mosaicApiClientId: string
  @description('Published environment-local APIM model base URL; never an Azure AI account URL.')
  @minLength(1)
  modelEndpoint: string
  @minLength(1)
  modelDeployment: string
  modelApiVersion: string?
  @description('Production only: five workload keys, each with modelEndpoint, modelDeployment and mcpGatewayUrl.')
  workloads: {
    *: {
      @minLength(1)
      modelEndpoint: string
      @minLength(1)
      modelDeployment: string
      @minLength(1)
      mcpGatewayUrl: string
    }
  }?
}

@export()
type RuntimeConfigurations = {
  dev: RuntimeSettings
  stg: RuntimeSettings
  prod: RuntimeSettings
}
