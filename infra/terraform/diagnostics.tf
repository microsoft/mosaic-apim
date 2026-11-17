resource "azurerm_api_management_logger" "insights" {
  count               = var.deploy_apim ? 1 : 0
  name                = "appinsights"
  api_management_name = azurerm_api_management.gateway[0].name
  resource_group_name = azurerm_resource_group.mosaic.name
  resource_id         = azurerm_application_insights.apps.id
  buffered            = true
  application_insights {
    connection_string = azurerm_application_insights.apps.connection_string
  }
}

resource "azapi_resource" "monitor_logger" {
  count     = var.deploy_apim ? 1 : 0
  type      = "Microsoft.ApiManagement/service/loggers@2022-08-01"
  parent_id = azurerm_api_management.gateway[0].id
  name      = "azuremonitor"
  body = {
    properties = {
      loggerType = "azureMonitor"
      isBuffered = true
    }
  }
}

// AzureRM exposes neither the azureMonitor logger nor diagnostic metrics.
resource "azapi_resource" "insights_diagnostic" {
  count     = var.deploy_apim ? 1 : 0
  type      = "Microsoft.ApiManagement/service/diagnostics@2022-08-01"
  parent_id = azurerm_api_management.gateway[0].id
  name      = "applicationinsights"
  body = {
    properties = {
      loggerId                = azurerm_api_management_logger.insights[0].id
      alwaysLog               = "allErrors"
      sampling                = { samplingType = "fixed", percentage = 100 }
      verbosity               = "information"
      metrics                 = true
      httpCorrelationProtocol = "W3C"
      frontend = {
        request  = { body = { bytes = 0 }, headers = [] }
        response = { body = { bytes = 0 }, headers = [] }
      }
      backend = {
        request  = { body = { bytes = 0 }, headers = [] }
        response = { body = { bytes = 0 }, headers = [] }
      }
    }
  }
}

locals {
  diagnostics = merge({
    registry   = { id = azurerm_container_registry.images.id, logs = "allLogs" }
    cosmos     = { id = azurerm_cosmosdb_account.state.id, logs = "allLogs" }
    backend    = { id = azurerm_key_vault.vault["backend"].id, logs = "audit" }
    management = { id = azurerm_key_vault.vault["management"].id, logs = "audit" }
    }, { for app in ["api", "web", "portal"] :
    app => { id = azurerm_linux_web_app.app[app].id, logs = "allLogs" }
    }, var.deploy_apim ? {
    gateway = { id = azurerm_api_management.gateway[0].id, logs = "allLogs" }
  } : {})
}

resource "azurerm_monitor_diagnostic_setting" "resource" {
  for_each                       = local.diagnostics
  name                           = "to-log-analytics"
  target_resource_id             = each.value.id
  log_analytics_workspace_id     = azurerm_log_analytics_workspace.logs.id
  log_analytics_destination_type = "Dedicated"
  enabled_log {
    category_group = each.value.logs
  }
  enabled_metric {
    category = "AllMetrics"
  }
}
