terraform {
  required_version = ">= 1.7, < 2.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.81, < 5.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.14, < 3.0"
    }
  }

}

provider "azapi" {
  subscription_id = var.subscription_id
  environment     = "public"
}

provider "azurerm" {
  subscription_id = var.subscription_id
  environment     = "public"
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
}
