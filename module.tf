# ─── Quota allocation (transfer quota between group and subscription) ─────────

# Flatten the quotas list into a map keyed by "group_name/location/resource_name".
# Each SKU gets its own resource instance so adding/removing a SKU does not
# trigger replacement of unrelated allocations.
locals {
  _quota_group_allocations_flat = {
    for entry in flatten([
      for group_name, group in var.quota_group : [
        for q in group.quotas : {
          key             = "${group_name}/${lower(q.location)}/${lower(q.resource_name)}"
          quota_group_key = group_name
          location        = lower(q.location)
          resource_name   = q.resource_name
          limit           = q.limit
        }
      ]
    ]) : entry.key => entry
  }
}

# Fetch compute usages per location to map display names to internal IDs.
# This allows users to specify either the display name or internal ID.
locals {
  _quota_allocation_locations = toset([
    for k, v in local._quota_group_allocations_flat : v.location
  ])
}

data "azapi_resource_action" "compute_usages" {
  for_each = length(local._quota_group_allocations_flat) > 0 ? local._quota_allocation_locations : toset([])

  type        = "Microsoft.Compute/locations@2023-07-01"
  resource_id = "/subscriptions/${var.subscription_id}/providers/Microsoft.Compute/locations/${each.value}"
  action      = "usages"
  method      = "GET"

  response_export_values = ["value"]
}

locals {
  # Build a map per location: { "canadacentral" = { "standard dasv5 family vcpus" = "standarddasv5family", ... } }
  # Keys are lowercased for case-insensitive lookup.
  _compute_usage_lookup = {
    for loc in local._quota_allocation_locations : loc => {
      for usage in try(data.azapi_resource_action.compute_usages[loc].output.value, []) :
      lower(try(usage.name.localizedValue, "")) => try(usage.name.value, "")
      if try(usage.name.value, "") != ""
    }
  }

  # Resolve resource_name: if it matches a display name (case-insensitive), use the internal ID;
  # otherwise use as-is. Always lowercase — the Quota API requires lowercase resource names.
  _resolved_allocations = {
    for k, v in local._quota_group_allocations_flat : k => {
      quota_group_key = v.quota_group_key
      location        = v.location
      resource_name   = lower(try(local._compute_usage_lookup[v.location][lower(v.resource_name)], v.resource_name))
      limit           = v.limit
    }
  }

  # Build the cleanup commands per subscription group key.
  # Each allocation gets its own cleanup entry (reset to limit=10).
  _cleanup_commands_by_subscription = {
    for group_name, group in var.quota_group : group_name => [
      for alloc_key, alloc in local._resolved_allocations : {
        url = "https://management.azure.com/providers/Microsoft.Management/managementGroups/${group.management_group_id}/subscriptions/${coalesce(group.subscription_id, var.subscription_id)}/providers/Microsoft.Quota/groupQuotas/${group_name}/resourceProviders/Microsoft.Compute/quotaAllocations/${alloc.location}?api-version=2025-03-01"
        body = jsonencode({
          properties = {
            value = [
              {
                properties = {
                  limit        = 10
                  resourceName = alloc.resource_name
                }
              }
            ]
          }
        })
      }
      if alloc.quota_group_key == group_name
    ]
  }
}

# ─── Subscription membership ─────────────────────────────────────────────────
# On create: adds subscription to the quota group (idempotent GET-then-PUT).
# On destroy: returns all allocated quota to the group (limit=10), THEN removes
# the subscription from the group. This guarantees quota is never lost.

resource "terraform_data" "quota_group" {
  for_each = var.quota_group

  input = {
    add_url    = "https://management.azure.com/providers/Microsoft.Management/managementGroups/${each.value.management_group_id}/providers/Microsoft.Quota/groupQuotas/${each.key}/subscriptions/${coalesce(each.value.subscription_id, var.subscription_id)}?api-version=2025-03-01"
    remove_url = "https://management.azure.com/providers/Microsoft.Management/managementGroups/${each.value.management_group_id}/providers/Microsoft.Quota/groupQuotas/${each.key}/subscriptions/${coalesce(each.value.subscription_id, var.subscription_id)}?api-version=2025-03-01"
    # Store cleanup commands so they're available at destroy time via self.output
    cleanup_commands = jsonencode(local._cleanup_commands_by_subscription[each.key])
  }

  # Create: add subscription to group
  provisioner "local-exec" {
    command     = "${path.module}/scripts/add-subscription.sh '${self.output.add_url}'"
    interpreter = ["bash", "-c"]
  }

  # Destroy: first return quota, then remove subscription
  provisioner "local-exec" {
    when        = destroy
    command     = "${path.module}/scripts/remove-subscription.sh '${self.output.cleanup_commands}' '${self.output.remove_url}'"
    interpreter = ["bash", "-c"]
  }
}

# ─── Quota allocations ────────────────────────────────────────────────────────
# Each SKU gets its own resource keyed by "group_name/location/resource_name".
# This ensures adding or removing a SKU does not trigger replacement of
# unrelated allocations in the same location.
#
# On destroy (e.g. SKU removed from tfvars), the destroy provisioner returns
# that SKU's quota back to the group (limit=10) before the resource is removed
# from state. This prevents quota leakage.

resource "terraform_data" "quota_group_allocations" {
  for_each = local._quota_group_allocations_flat

  input = {
    url           = "https://management.azure.com/providers/Microsoft.Management/managementGroups/${var.quota_group[each.value.quota_group_key].management_group_id}/subscriptions/${coalesce(var.quota_group[each.value.quota_group_key].subscription_id, var.subscription_id)}/providers/Microsoft.Quota/groupQuotas/${each.value.quota_group_key}/resourceProviders/Microsoft.Compute/quotaAllocations/${local._resolved_allocations[each.key].location}?api-version=2025-03-01"
    resource_name = local._resolved_allocations[each.key].resource_name
    body = jsonencode({
      properties = {
        value = [
          {
            properties = {
              limit        = local._resolved_allocations[each.key].limit
              resourceName = local._resolved_allocations[each.key].resource_name
            }
          }
        ]
      }
    })
  }

  triggers_replace = "${local._resolved_allocations[each.key].resource_name}:${local._resolved_allocations[each.key].limit}"

  lifecycle {
    precondition {
      condition     = !can(regex(" ", local._resolved_allocations[each.key].resource_name))
      error_message = "Resource name '${local._resolved_allocations[each.key].resource_name}' contains spaces — it was not resolved to an internal ID. Check that the display name exactly matches what Azure returns, or use the internal ID directly (e.g. 'standarddasv5family' instead of 'Standard DASv5 Family vCPUs')."
    }
  }

  # Create/update: allocate quota from the group to the subscription
  provisioner "local-exec" {
    command     = "${path.module}/scripts/allocate-quota.sh '${self.output.url}' '${self.output.body}'"
    interpreter = ["bash", "-c"]
  }

  # Destroy: return this SKU's quota to the group before removing from state
  provisioner "local-exec" {
    when        = destroy
    command     = "${path.module}/scripts/deallocate-quota.sh '${self.output.url}' '${self.output.resource_name}'"
    interpreter = ["bash", "-c"]
  }

  depends_on = [terraform_data.quota_group]
}
