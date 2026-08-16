variable "subscription_id" {
  description = "The subscription ID to manage quota group memberships for."
  type        = string
}

variable "quota_group" {
  description = <<-EOT
    Map of quota group subscription memberships and their quota allocations.

    The top-level key is used as the group_quota_name.
    `subscription_id` defaults to var.subscription_id if omitted.

    Each entry in `quotas` specifies a location, resource name, and desired limit.
    `resource_name` accepts either the display name (e.g. "Standard DASv5 Family vCPUs")
    or the internal ID (e.g. "standarddasv5family"). Display names are auto-resolved.
  EOT
  type = map(object({
    management_group_id = string
    subscription_id     = optional(string, null)
    quotas = optional(list(object({
      location      = string # Azure region (e.g. "canadacentral")
      resource_name = string # VM SKU family: display name or internal ID
      limit         = number # Desired subscription quota limit (absolute value)
    })), [])
  }))
  default = {}
}
