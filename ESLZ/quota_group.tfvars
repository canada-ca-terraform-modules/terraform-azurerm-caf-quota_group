# ESLZ/quota_group.tfvars
#
# Azure Quota Groups — manage subscription membership and VM SKU family quota
# allocations via the Microsoft.Quota API (2025-03-01).
#
# Prerequisites:
#   - Microsoft.Quota must be registered on the subscription
#   - Microsoft.Compute is auto-registered by the azurerm provider
#   - Subscription must have Quota Request Operator role assigned
#   - Management Group must have GroupQuota Request Operator role assigned
#
# Behaviour:
#   - Adding a subscription to a group does not change its existing quota.
#   - Allocations set the absolute subscription limit; the difference is
#     transferred to/from the group automatically.
#   - On destroy, allocations are reduced to 10 (minimum) before the
#     subscription is removed, returning quota to the group.
#   - A subscription can only belong to one quota group at a time.

# ─── Subscription membership & quota allocations ─────────────────────────────
# The top-level key is used as the group_quota_name.
# subscription_id defaults to var.subscription_id if omitted.
#
# Example: if current subscription quota is 10 and you set limit = 50, then
# 40 cores are transferred from the group to the subscription.
# To return quota: lower the limit (minimum 10).
#
# The PATCH only fires when the limit value changes — no action on subsequent
# applies with the same values.

quota_group = {
  # "CanadaCentral" = { # <-- Name of the existing quota group (used in the Microsoft.Quota API)
  #   management_group_id = "my-management-group-id"
  #   # subscription_id   = "00000000-0000-0000-0000-000000000000"  # optional override
  #
  #   quotas = [
  #     { location = "canadacentral", resource_name = "standardddv4family",          limit = 50  },
  #     { location = "canadacentral", resource_name = "Standard Dv4 Family vCPUs",   limit = 100 },
  #     { location = "canadaeast",    resource_name = "standarddsv5family",           limit = 64  },
  #   ]
  # }
}
