output "deployment" {
  description = "Non-secret deployment contract consumed by scripts/deploy_terraform.py."
  value = {
    AZURE_ENV_NAME                    = var.environment_name
    AZURE_SUBSCRIPTION_ID             = var.subscription_id
    AZURE_LOCATION                    = var.location
    AZURE_RESOURCE_GROUP              = azurerm_resource_group.mosaic.name
    AZURE_CONTAINER_REGISTRY_NAME     = azurerm_container_registry.images.name
    AZURE_CONTAINER_REGISTRY_ENDPOINT = azurerm_container_registry.images.login_server
    API_APP_NAME                      = azurerm_linux_web_app.app["api"].name
    WEB_APP_NAME                      = azurerm_linux_web_app.app["web"].name
    PORTAL_APP_NAME                   = azurerm_linux_web_app.app["portal"].name
    API_APP_URL                       = local.urls.api
    WEB_APP_URL                       = local.urls.web
    PORTAL_APP_URL                    = local.urls.portal
    API_APP_PRINCIPAL_ID              = azurerm_linux_web_app.app["api"].identity[0].principal_id
    APIM_RESOURCE_ID                  = local.apim_id
    APIM_GATEWAY_URL                  = local.apim_url
    KEY_VAULT_URI                     = azurerm_key_vault.vault["backend"].vault_uri
    MANAGEMENT_KEY_VAULT_URI          = azurerm_key_vault.vault["management"].vault_uri
    COSMOSDB_ENDPOINT                 = azurerm_cosmosdb_account.state.endpoint
    LOG_ANALYTICS_WORKSPACE_ID        = azurerm_log_analytics_workspace.logs.id
  }
}
