variable "subscription_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "Supply the Azure subscription UUID."
  }
}

variable "environment_name" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}$", var.environment_name))
    error_message = "Use 3-24 lowercase letters, digits or hyphens, beginning with a letter."
  }
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "app_service_location" {
  type    = string
  default = null
}

variable "app_service_plan_sku" {
  type    = string
  default = "B1"
}

variable "apim_sku_name" {
  type    = string
  default = "Developer_1"
}

variable "deploy_apim" {
  type    = bool
  default = true
}

variable "external_bootstrap_apim_resource_id" {
  type    = string
  default = ""
  validation {
    condition = var.external_bootstrap_apim_resource_id == "" || can(regex(
      "(?i)^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft.ApiManagement/service/[^/]+$",
      var.external_bootstrap_apim_resource_id
    ))
    error_message = "Supply an existing API Management resource ID or an empty string."
  }
}

variable "publisher_name" {
  type = string
}

variable "publisher_email" {
  type = string
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.publisher_email))
    error_message = "Supply a publisher email address."
  }
}

variable "tenant_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.tenant_id))
    error_message = "Supply the Entra tenant UUID."
  }
}

variable "api_client_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.api_client_id))
    error_message = "Supply the MOSAIC API application client UUID."
  }
}

variable "spa_client_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.spa_client_id))
    error_message = "Supply the administrator console application client UUID."
  }
}

variable "portal_client_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$", var.portal_client_id))
    error_message = "Supply the portal application client UUID."
  }
}

variable "api_application_id_uri" {
  type = string
}

variable "api_scope" {
  type = string
}

variable "model_runtime_client_id" {
  type    = string
  default = ""
}

variable "model_client_id" {
  type    = string
  default = ""
}

variable "entra_group_claims" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "teams_workflow_allowed_hosts" {
  type    = list(string)
  default = []
}

variable "mcp_discovery_timeout_seconds" {
  type    = number
  default = 20
  validation {
    condition = (
      var.mcp_discovery_timeout_seconds >= 1 && var.mcp_discovery_timeout_seconds <= 120 &&
      floor(var.mcp_discovery_timeout_seconds) == var.mcp_discovery_timeout_seconds
    )
    error_message = "MCP discovery timeout must be a whole number from 1 to 120 seconds."
  }
}

variable "localhost_origins" {
  type    = list(string)
  default = ["http://localhost:3000", "http://localhost:5173"]
}

variable "images" {
  description = "Application images, persisted here by the deployment helper so apply cannot restore placeholders."
  type = object({
    api    = string
    web    = string
    portal = string
  })
  default = {
    api    = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
    web    = "mcr.microsoft.com/appsvc/staticsite:latest"
    portal = "mcr.microsoft.com/appsvc/staticsite:latest"
  }
  validation {
    condition = alltrue([for image in values(var.images) :
    can(regex("^[a-zA-Z0-9.-]+/[^[:space:]]+$", image))])
    error_message = "Each image must include a registry hostname and repository/tag or digest."
  }
}
