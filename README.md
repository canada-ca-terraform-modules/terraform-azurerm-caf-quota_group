# terraform-azurerm-caf-quota_group

Manages Azure Quota Group subscription memberships and quota allocations via
the Microsoft.Quota API (2025-03-01).

## Features

- Adds subscriptions to quota groups (idempotent)
- Allocates VM SKU family quota from the group to the subscription
- Resolves display names (e.g. "Standard DASv5 Family vCPUs") to internal IDs automatically
- On destroy: safely returns all allocated quota to the group before removing the subscription
- Polling-based confirmation of allocation/deallocation operations

## Prerequisites

- `Microsoft.Quota` resource provider must be registered on the subscription
- `Microsoft.Compute` is auto-registered by the azurerm provider
- The service principal must have **Quota Request Operator** on the subscription
- The service principal must have **GroupQuota Request Operator** on the management group
- `az` CLI must be available in the execution environment (used for REST API calls)
- `jq` must be available in the execution environment

## Usage

```hcl
module "quota_group" {
  source = "github.com/canada-ca-terraform-modules/terraform-azurerm-caf-quota_group?ref=v1.1.0"

  subscription_id = var.subscription_id

  quota_group = {
    CanadaCentral = {
      management_group_id = "my-management-group-id"

      quotas = [
        { location = "canadacentral", resource_name = "standarddasv5family",        limit = 50  },
        { location = "canadacentral", resource_name = "Standard Dv4 Family vCPUs",  limit = 100 },
        { location = "canadaeast",    resource_name = "standarddsv5family",          limit = 64  },
      ]
    }
  }
}
```

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9 |
| <a name="requirement_azapi"></a> [azapi](#requirement\_azapi) | ~> 2.0 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_azapi"></a> [azapi](#provider\_azapi) | ~> 2.0 |
| <a name="provider_terraform"></a> [terraform](#provider\_terraform) | n/a |

## Modules

No modules.

## Resources

| Name | Type |
|------|------|
| [terraform_data.quota_group](https://registry.terraform.io/providers/hashicorp/terraform/latest/docs/resources/data) | resource |
| [terraform_data.quota_group_allocations](https://registry.terraform.io/providers/hashicorp/terraform/latest/docs/resources/data) | resource |
| [azapi_resource_action.compute_usages](https://registry.terraform.io/providers/azure/azapi/latest/docs/data-sources/resource_action) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_quota_group"></a> [quota\_group](#input\_quota\_group) | Map of quota group subscription memberships and their quota allocations.<br/><br/>The top-level key is used as the group\_quota\_name.<br/>`subscription_id` defaults to var.subscription\_id if omitted.<br/><br/>Each entry in `quotas` specifies a location, resource name, and desired limit.<br/>`resource_name` accepts either the display name (e.g. "Standard DASv5 Family vCPUs")<br/>or the internal ID (e.g. "standarddasv5family"). Display names are auto-resolved. | <pre>map(object({<br/>    management_group_id = string<br/>    subscription_id     = optional(string, null)<br/>    quotas = optional(list(object({<br/>      location      = string # Azure region (e.g. "canadacentral")<br/>      resource_name = string # VM SKU family: display name or internal ID<br/>      limit         = number # Desired subscription quota limit (absolute value)<br/>    })), [])<br/>  }))</pre> | `{}` | no |
| <a name="input_subscription_id"></a> [subscription\_id](#input\_subscription\_id) | The subscription ID to manage quota group memberships for. | `string` | n/a | yes |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_quota_group"></a> [quota\_group](#output\_quota\_group) | The quota group subscription membership resources. |
| <a name="output_quota_group_allocations"></a> [quota\_group\_allocations](#output\_quota\_group\_allocations) | The quota group allocation resources. |
<!-- END_TF_DOCS -->
