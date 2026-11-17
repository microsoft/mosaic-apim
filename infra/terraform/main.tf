data "azurerm_client_config" "current" {}

locals {
  suffix = substr(sha256("${var.subscription_id}/${var.environment_name}"), 0, 6)
  label  = trimprefix(var.environment_name, "mosaic-")
  stem   = "mosaic-${local.label}-${local.suffix}"
  tags = merge(var.tags, {
    product            = "MOSAIC"
    managed-by         = "terraform"
    mosaic-environment = var.environment_name
  })
  app_names  = { for app in ["api", "web", "portal"] : app => "${local.stem}-${app}" }
  urls       = { for app, name in local.app_names : app => "https://${name}.azurewebsites.net" }
  cors       = concat(var.localhost_origins, [local.urls.web, local.urls.portal])
  apim_id    = var.deploy_apim ? azurerm_api_management.gateway[0].id : var.external_bootstrap_apim_resource_id
  apim_parts = split("/", local.apim_id)
  apim_name  = local.apim_parts[8]
  apim_url   = "https://${local.apim_name}.azure-api.net"
  vault_names = {
    backend    = "kv${substr(replace(local.label, "-", ""), 0, 10)}${local.suffix}"
    management = "kvm${substr(replace(local.label, "-", ""), 0, 10)}${local.suffix}"
  }
}

resource "azurerm_resource_group" "mosaic" {
  name     = "rg-${local.stem}"
  location = var.location
  tags     = local.tags
  lifecycle {
    precondition {
      condition     = var.deploy_apim || var.external_bootstrap_apim_resource_id != ""
      error_message = "Supply an external bootstrap gateway ID when deploy_apim is false."
    }
    precondition {
      condition     = var.model_runtime_client_id == "" || var.model_runtime_client_id != var.api_client_id
      error_message = "The model-runtime audience must differ from the control-plane API."
    }
    precondition {
      condition = var.model_client_id == "" || (
        var.model_client_id != var.api_client_id && var.model_client_id != var.model_runtime_client_id
      )
      error_message = "The public model client must differ from both API audiences."
    }
    precondition {
      condition     = data.azurerm_client_config.current.tenant_id == var.tenant_id
      error_message = "The authenticated Azure tenant must match tenant_id."
    }
  }
}

resource "azurerm_service_plan" "apps" {
  name                = substr("asp-${local.stem}", 0, 40)
  resource_group_name = azurerm_resource_group.mosaic.name
  location            = coalesce(var.app_service_location, var.location)
  os_type             = "Linux"
  sku_name            = var.app_service_plan_sku
  tags                = local.tags
}

resource "azurerm_container_registry" "images" {
  name                          = "mosaic${substr(replace(local.label, "-", ""), 0, 24)}${local.suffix}acr"
  resource_group_name           = azurerm_resource_group.mosaic.name
  location                      = var.location
  sku                           = "Basic"
  admin_enabled                 = false
  public_network_access_enabled = true
  tags                          = local.tags
}

resource "azurerm_log_analytics_workspace" "logs" {
  name                            = "log-${local.stem}"
  resource_group_name             = azurerm_resource_group.mosaic.name
  location                        = var.location
  sku                             = "PerGB2018"
  retention_in_days               = 30
  internet_ingestion_enabled      = true
  internet_query_enabled          = true
  allow_resource_only_permissions = true
  tags                            = local.tags
}

resource "azurerm_application_insights" "apps" {
  name                = "appi-${local.stem}"
  resource_group_name = azurerm_resource_group.mosaic.name
  location            = var.location
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.logs.id
  tags                = local.tags
}

resource "azurerm_key_vault" "vault" {
  for_each                      = local.vault_names
  name                          = each.value
  resource_group_name           = azurerm_resource_group.mosaic.name
  location                      = var.location
  tenant_id                     = var.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = true
  tags                          = local.tags
}

resource "azurerm_cosmosdb_account" "state" {
  name                          = substr("cosmos-${local.stem}", 0, 44)
  resource_group_name           = azurerm_resource_group.mosaic.name
  location                      = var.location
  offer_type                    = "Standard"
  kind                          = "GlobalDocumentDB"
  local_authentication_enabled  = false
  public_network_access_enabled = true
  minimal_tls_version           = "Tls12"
  capabilities {
    name = "EnableServerless"
  }
  consistency_policy {
    consistency_level = "Session"
  }
  geo_location {
    location          = var.location
    failover_priority = 0
    zone_redundant    = false
  }
  tags = local.tags
}

resource "azurerm_cosmosdb_sql_database" "mosaic" {
  name                = "mosaic"
  resource_group_name = azurerm_resource_group.mosaic.name
  account_name        = azurerm_cosmosdb_account.state.name
}

resource "azurerm_cosmosdb_sql_container" "state" {
  for_each = {
    desired-state   = -1
    sync-operations = null
    audit-events    = null
    observed-state  = null
    usage-rollups   = -1
  }
  name                = each.key
  resource_group_name = azurerm_resource_group.mosaic.name
  account_name        = azurerm_cosmosdb_account.state.name
  database_name       = azurerm_cosmosdb_sql_database.mosaic.name
  partition_key_paths = ["/tenantId"]
  partition_key_kind  = "Hash"
  default_ttl         = each.value
}

resource "azurerm_api_management" "gateway" {
  count                         = var.deploy_apim ? 1 : 0
  name                          = "apim-${local.stem}"
  resource_group_name           = azurerm_resource_group.mosaic.name
  location                      = var.location
  publisher_name                = var.publisher_name
  publisher_email               = var.publisher_email
  sku_name                      = var.apim_sku_name
  public_network_access_enabled = true
  identity {
    type = "SystemAssigned"
  }
  protocols {
    http2_enabled = true
  }
  security {
    backend_tls10_enabled  = false
    backend_tls11_enabled  = false
    frontend_ssl30_enabled = false
    frontend_tls10_enabled = false
    frontend_tls11_enabled = false
  }
  tags = local.tags
}

locals {
  api_settings = {
    MOSAIC_ENVIRONMENT                           = "azure"
    MOSAIC_AUTH_MODE                             = "entra"
    MOSAIC_REPOSITORY_BACKEND                    = "cosmos"
    MOSAIC_AUTHORITY_URL                         = "https://login.microsoftonline.com/${var.tenant_id}"
    MOSAIC_TENANT_ID                             = var.tenant_id
    MOSAIC_API_CLIENT_ID                         = var.api_client_id
    MOSAIC_MODEL_RUNTIME_CLIENT_ID               = var.model_runtime_client_id
    MOSAIC_MODEL_CLIENT_ID                       = var.model_client_id
    MOSAIC_ENTRA_GROUP_CLAIMS                    = tostring(var.entra_group_claims)
    MOSAIC_SPA_CLIENT_ID                         = var.spa_client_id
    MOSAIC_API_APPLICATION_ID_URI                = var.api_application_id_uri
    MOSAIC_API_SCOPE                             = var.api_scope
    MOSAIC_FRONTEND_URL                          = local.urls.web
    MOSAIC_PORTAL_URL                            = local.urls.portal
    MOSAIC_SCHEDULED_WORKERS_ALWAYS_ON           = "true"
    MOSAIC_APIM_GATEWAY_URL                      = local.apim_url
    MOSAIC_APIM_SUBSCRIPTION_ID                  = local.apim_parts[2]
    MOSAIC_APIM_RESOURCE_GROUP                   = local.apim_parts[4]
    MOSAIC_APIM_SERVICE_NAME                     = local.apim_name
    MOSAIC_COSMOS_ENDPOINT                       = azurerm_cosmosdb_account.state.endpoint
    MOSAIC_COSMOS_OBSERVED_STATE_CONTAINER       = "observed-state"
    MOSAIC_COSMOS_USAGE_ROLLUPS_CONTAINER        = "usage-rollups"
    MOSAIC_KEY_VAULT_URI                         = azurerm_key_vault.vault["backend"].vault_uri
    MOSAIC_KEY_VAULT_RESOURCE_ID                 = azurerm_key_vault.vault["backend"].id
    MOSAIC_MANAGEMENT_KEY_VAULT_URI              = azurerm_key_vault.vault["management"].vault_uri
    MOSAIC_CORS_ORIGINS                          = jsonencode(local.cors)
    MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.apps.connection_string
    MOSAIC_USAGE_SOURCE                          = "rollups"
    MOSAIC_LOG_ANALYTICS_ENDPOINT                = "https://api.loganalytics.azure.com"
    MOSAIC_MCP_DISCOVERY_TIMEOUT_SECONDS         = tostring(var.mcp_discovery_timeout_seconds)
    MOSAIC_TEAMS_WORKFLOW_ALLOWED_HOSTS          = jsonencode(var.teams_workflow_allowed_hosts)
    MOSAIC_TEAMS_BOT_AUTH_MODE                   = "disabled"
  }
  frontend_settings = {
    MOSAIC_API_BASE_URL                          = local.urls.api
    MOSAIC_ENTRA_TENANT_ID                       = var.tenant_id
    MOSAIC_ENTRA_API_SCOPE                       = var.api_scope
    MOSAIC_APPLICATIONINSIGHTS_CONNECTION_STRING = azurerm_application_insights.apps.connection_string
  }
  settings = {
    api = local.api_settings
    web = merge(local.frontend_settings, {
      MOSAIC_ENTRA_CLIENT_ID = var.spa_client_id
      MOSAIC_PORTAL_URL      = local.urls.portal
    })
    portal = merge(local.frontend_settings, { MOSAIC_ENTRA_CLIENT_ID = var.portal_client_id })
  }
}

resource "azurerm_linux_web_app" "app" {
  for_each                                       = local.app_names
  name                                           = each.value
  resource_group_name                            = azurerm_resource_group.mosaic.name
  location                                       = azurerm_service_plan.apps.location
  service_plan_id                                = azurerm_service_plan.apps.id
  https_only                                     = true
  client_affinity_enabled                        = false
  public_network_access_enabled                  = true
  ftp_publish_basic_authentication_enabled       = false
  webdeploy_publish_basic_authentication_enabled = false
  identity {
    type = "SystemAssigned"
  }
  site_config {
    always_on                               = true
    container_registry_use_managed_identity = true
    ftps_state                              = "Disabled"
    health_check_path                       = each.key == "api" ? "/readyz" : "/healthz"
    health_check_eviction_time_in_min       = 2
    http2_enabled                           = true
    minimum_tls_version                     = "1.2"
    scm_minimum_tls_version                 = "1.2"
    use_32_bit_worker                       = false
    websockets_enabled                      = false
    application_stack {
      docker_image_name   = join("/", slice(split("/", var.images[each.key]), 1, length(split("/", var.images[each.key]))))
      docker_registry_url = "https://${split("/", var.images[each.key])[0]}"
    }
    dynamic "cors" {
      for_each = each.key == "api" ? [1] : []
      content {
        allowed_origins     = local.cors
        support_credentials = false
      }
    }
  }
  app_settings = merge(local.settings[each.key], {
    WEBSITES_PORT = each.key == "api" ? "8000" : "8080"
  })
  tags = local.tags
}

resource "azurerm_role_assignment" "image_pull" {
  for_each                         = local.app_names
  scope                            = azurerm_container_registry.images.id
  role_definition_name             = "AcrPull"
  principal_id                     = azurerm_linux_web_app.app[each.key].identity[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

locals {
  api_roles = {
    backend-read    = { scope = azurerm_key_vault.vault["backend"].id, role = "Key Vault Secrets User" }
    backend-write   = { scope = azurerm_key_vault.vault["backend"].id, role = "Key Vault Secrets Officer" }
    backend-inspect = { scope = azurerm_key_vault.vault["backend"].id, role = "Reader" }
    management      = { scope = azurerm_key_vault.vault["management"].id, role = "Key Vault Secrets Officer" }
    logs            = { scope = azurerm_log_analytics_workspace.logs.id, role = "Log Analytics Reader" }
    insights        = { scope = azurerm_application_insights.apps.id, role = "Monitoring Reader" }
  }
}

resource "azurerm_role_assignment" "api" {
  for_each                         = local.api_roles
  scope                            = each.value.scope
  role_definition_name             = each.value.role
  principal_id                     = azurerm_linux_web_app.app["api"].identity[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "gateway_secrets" {
  count                            = var.deploy_apim ? 1 : 0
  scope                            = azurerm_key_vault.vault["backend"].id
  role_definition_name             = "Key Vault Secrets User"
  principal_id                     = azurerm_api_management.gateway[0].identity[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_role_assignment" "api_gateway" {
  for_each                         = var.deploy_apim ? toset(["API Management Service Contributor", "Monitoring Reader"]) : toset([])
  scope                            = azurerm_api_management.gateway[0].id
  role_definition_name             = each.value
  principal_id                     = azurerm_linux_web_app.app["api"].identity[0].principal_id
  principal_type                   = "ServicePrincipal"
  skip_service_principal_aad_check = true
}

resource "azurerm_cosmosdb_sql_role_assignment" "api" {
  resource_group_name = azurerm_resource_group.mosaic.name
  account_name        = azurerm_cosmosdb_account.state.name
  role_definition_id  = "${azurerm_cosmosdb_account.state.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_linux_web_app.app["api"].identity[0].principal_id
  scope               = azurerm_cosmosdb_account.state.id
}
