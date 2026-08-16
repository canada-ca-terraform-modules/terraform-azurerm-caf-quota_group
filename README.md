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
<!-- END_TF_DOCS -->
