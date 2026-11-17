mock_provider "azurerm" {
  mock_data "azurerm_client_config" {
    defaults = {
      tenant_id = "11111111-1111-1111-1111-111111111111"
    }
  }
  mock_resource "azurerm_api_management" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.ApiManagement/service/apim-example"
      identity = {
        type         = "SystemAssigned"
        principal_id = "77777777-7777-7777-7777-777777777777"
        tenant_id    = "11111111-1111-1111-1111-111111111111"
      }
    }
  }
  mock_resource "azurerm_service_plan" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.Web/serverFarms/plan-example"
    }
  }
  mock_resource "azurerm_log_analytics_workspace" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.OperationalInsights/workspaces/logs-example"
    }
  }
  mock_resource "azurerm_application_insights" {
    defaults = {
      id                = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.Insights/components/insights-example"
      connection_string = "InstrumentationKey=88888888-8888-8888-8888-888888888888;IngestionEndpoint=https://eastus2-0.in.applicationinsights.azure.com/"
    }
  }
  mock_resource "azurerm_container_registry" {
    defaults = {
      id           = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.ContainerRegistry/registries/registryexample"
      login_server = "registryexample.azurecr.io"
    }
  }
  mock_resource "azurerm_cosmosdb_account" {
    defaults = {
      id       = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.DocumentDB/databaseAccounts/cosmos-example"
      endpoint = "https://cosmos-example.documents.azure.com:443/"
    }
  }
  mock_resource "azurerm_linux_web_app" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.Web/sites/app-example"
      identity = {
        type         = "SystemAssigned"
        principal_id = "99999999-9999-9999-9999-999999999999"
        tenant_id    = "11111111-1111-1111-1111-111111111111"
      }
    }
  }
  mock_resource "azurerm_api_management_logger" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.ApiManagement/service/apim-example/loggers/appinsights"
    }
  }
}

mock_provider "azapi" {}

variables {
  images = {
    api    = "registryexample.azurecr.io/mosaic-api@sha256:${sha256("api")}"
    web    = "registryexample.azurecr.io/mosaic-web@sha256:${sha256("web")}"
    portal = "registryexample.azurecr.io/mosaic-portal@sha256:${sha256("portal")}"
  }
}

override_resource {
  target = azurerm_key_vault.vault["backend"]
  values = {
    id        = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.KeyVault/vaults/backend-example"
    vault_uri = "https://backend-example.vault.azure.net/"
  }
}

override_resource {
  target = azurerm_key_vault.vault["management"]
  values = {
    id        = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.KeyVault/vaults/management-example"
    vault_uri = "https://management-example.vault.azure.net/"
  }
}

run "public_reference" {
  command = apply

  assert {
    condition = (
      length(azurerm_linux_web_app.app) == 3 &&
      alltrue([for app in azurerm_linux_web_app.app : app.public_network_access_enabled])
    )
    error_message = "All three apps must be deployed with the supported public networking."
  }
  assert {
    condition = (
      length(azurerm_cosmosdb_sql_container.state) == 5 &&
      azurerm_cosmosdb_sql_container.state["desired-state"].default_ttl == -1 &&
      azurerm_cosmosdb_sql_container.state["usage-rollups"].default_ttl == -1 &&
      alltrue([for container in azurerm_cosmosdb_sql_container.state :
      container.partition_key_paths == tolist(["/tenantId"])])
    )
    error_message = "Governance/usage persistence must retain its partition and TTL contract."
  }
  assert {
    condition = (
      length(azurerm_api_management.gateway) == 0 &&
      length(azurerm_role_assignment.gateway_secrets) == 0 &&
      length(azurerm_role_assignment.api_gateway) == 0 &&
      length(azurerm_api_management_logger.insights) == 0 &&
      length(azapi_resource.monitor_logger) == 0 &&
      length(azapi_resource.insights_diagnostic) == 0 &&
      !contains(keys(local.diagnostics), "gateway")
    )
    error_message = "Default hosting must not provision a gateway or gateway-specific permissions/telemetry."
  }
  assert {
    condition = (
      output.deployment.APIM_RESOURCE_ID == "" &&
      output.deployment.APIM_GATEWAY_URL == "" &&
      azurerm_linux_web_app.app["api"].app_settings.MOSAIC_APIM_SERVICE_NAME == "" &&
      azurerm_linux_web_app.app["api"].app_settings.MOSAIC_APIM_GATEWAY_URL == "" &&
      azurerm_linux_web_app.app["api"].app_settings.MOSAIC_APIM_SUBSCRIPTION_ID == var.subscription_id &&
      azurerm_linux_web_app.app["api"].app_settings.MOSAIC_APIM_RESOURCE_GROUP == azurerm_resource_group.mosaic.name
    )
    error_message = "MOSAIC-only hosting must start without a bootstrap gateway and retain its discovery scope."
  }
  assert {
    condition = alltrue([for name, app in azurerm_linux_web_app.app :
      app.site_config[0].application_stack[0].docker_image_name ==
    join("/", slice(split("/", var.images[name]), 1, length(split("/", var.images[name]))))])
    error_message = "Terraform must continue owning the selected images on repeat applies."
  }
}

run "external_gateway" {
  command = plan
  variables {
    deploy_apim                         = false
    external_bootstrap_apim_resource_id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/external/providers/Microsoft.ApiManagement/service/owned-elsewhere"
  }
  assert {
    condition = (
      length(azurerm_api_management.gateway) == 0 &&
      length(azurerm_role_assignment.gateway_secrets) == 0 &&
      length(azurerm_role_assignment.api_gateway) == 0 &&
      length(azurerm_api_management_logger.insights) == 0 &&
      length(azapi_resource.monitor_logger) == 0 &&
      length(azapi_resource.insights_diagnostic) == 0 &&
      !contains(keys(local.diagnostics), "gateway")
    )
    error_message = "An external gateway must not be provisioned or mutated by this deployment."
  }
  assert {
    condition = (
      output.deployment.APIM_RESOURCE_ID == var.external_bootstrap_apim_resource_id &&
      azurerm_linux_web_app.app["api"].app_settings.MOSAIC_APIM_SERVICE_NAME == "owned-elsewhere" &&
      azurerm_linux_web_app.app["api"].app_settings.MOSAIC_APIM_RESOURCE_GROUP == "external" &&
      output.deployment.APIM_GATEWAY_URL == "https://owned-elsewhere.azure-api.net"
    )
    error_message = "An external gateway reference must reach the API's bootstrap configuration."
  }
}

run "created_gateway_requires_publisher" {
  command = plan
  variables {
    deploy_apim = true
  }
  expect_failures = [azurerm_api_management.gateway]
}

run "created_gateway" {
  command = apply
  variables {
    deploy_apim     = true
    publisher_name  = "MOSAIC administrator"
    publisher_email = "admin@example.com"
  }
  assert {
    condition = (
      length(azurerm_api_management.gateway) == 1 &&
      output.deployment.APIM_RESOURCE_ID == azurerm_api_management.gateway[0].id &&
      azurerm_linux_web_app.app["api"].app_settings.MOSAIC_APIM_SERVICE_NAME == "apim-example" &&
      output.deployment.APIM_GATEWAY_URL == "https://apim-example.azure-api.net" &&
      alltrue([for role in ["API Management Service Contributor", "Monitoring Reader"] :
      azurerm_role_assignment.api_gateway[role].scope == azurerm_api_management.gateway[0].id]) &&
      length(azurerm_api_management_logger.insights) == 1 &&
      length(azapi_resource.monitor_logger) == 1 &&
      length(azapi_resource.insights_diagnostic) == 1 &&
      contains(keys(local.diagnostics), "gateway")
    )
    error_message = "Explicit opt-in must create and configure the gateway with its scoped permissions and telemetry."
  }
  assert {
    condition = (
      azurerm_role_assignment.gateway_secrets[0].scope == azurerm_key_vault.vault["backend"].id &&
      local.api_roles.management.scope == azurerm_key_vault.vault["management"].id
    )
    error_message = "Gateway secret access must remain separate from API-only management secrets."
  }
}
