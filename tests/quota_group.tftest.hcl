# Tests for terraform-azurerm-caf-quota_group logic.
# Run: terraform test
# No real Azure credentials required — pure plan-time logic with mock_provider.

mock_provider "azapi" {}

# ── Empty inputs produce no resources ─────────────────────────────────────────
run "empty_inputs_no_resources" {
  command = plan

  variables {
    subscription_id = "00000000-0000-0000-0000-000000000000"
    quota_group     = {}
  }

  assert {
    condition     = length(terraform_data.quota_group) == 0
    error_message = "Expected 0 subscription memberships for empty input."
  }

  assert {
    condition     = length(terraform_data.quota_group_allocations) == 0
    error_message = "Expected 0 allocations for empty input."
  }
}

# ── Single group with no quotas ───────────────────────────────────────────────
run "single_group_no_quotas" {
  command = plan

  variables {
    subscription_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    quota_group = {
      CanadaCentral = {
        management_group_id = "mg-test"
      }
    }
  }

  assert {
    condition     = length(terraform_data.quota_group) == 1
    error_message = "Expected 1 subscription membership."
  }

  assert {
    condition     = length(terraform_data.quota_group_allocations) == 0
    error_message = "Expected 0 allocations when no quotas specified."
  }
}

# ── Single group with one quota ───────────────────────────────────────────────
run "single_group_with_one_quota" {
  command = plan

  variables {
    subscription_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    quota_group = {
      CanadaCentral = {
        management_group_id = "mg-test"
        quotas = [
          { location = "canadacentral", resource_name = "standarddasv5family", limit = 50 }
        ]
      }
    }
  }

  assert {
    condition     = length(terraform_data.quota_group) == 1
    error_message = "Expected 1 subscription membership."
  }

  assert {
    condition     = length(terraform_data.quota_group_allocations) == 1
    error_message = "Expected 1 allocation."
  }
}

# ── Multiple SKUs in same location get independent allocations ────────────────
run "multiple_skus_same_location_independent" {
  command = plan

  variables {
    subscription_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    quota_group = {
      CanadaCentral = {
        management_group_id = "mg-test"
        quotas = [
          { location = "canadacentral", resource_name = "standarddasv5family", limit = 50 },
          { location = "canadacentral", resource_name = "standardddv4family",  limit = 100 },
        ]
      }
    }
  }

  assert {
    condition     = length(terraform_data.quota_group_allocations) == 2
    error_message = "Expected 2 allocations (one per SKU), got ${length(terraform_data.quota_group_allocations)}."
  }
}

# ── Multiple groups ───────────────────────────────────────────────────────────
run "multiple_groups" {
  command = plan

  variables {
    subscription_id = "main-sub"
    quota_group = {
      ComputeGroup = {
        management_group_id = "mg-prod"
        quotas = [
          { location = "canadacentral", resource_name = "standardddv4family", limit = 50 }
        ]
      }
      StorageGroup = {
        management_group_id = "mg-prod"
        subscription_id     = "other-sub"
        quotas = [
          { location = "canadaeast", resource_name = "standarddsv5family", limit = 64 }
        ]
      }
    }
  }

  assert {
    condition     = length(terraform_data.quota_group) == 2
    error_message = "Expected 2 subscription memberships."
  }

  assert {
    condition     = length(terraform_data.quota_group_allocations) == 2
    error_message = "Expected 2 allocations."
  }
}

# ── Default values for quota_group ─────────────────────────────────────────────
run "default_empty_map" {
  command = plan

  variables {
    subscription_id = "00000000-0000-0000-0000-000000000000"
  }

  assert {
    condition     = length(terraform_data.quota_group) == 0
    error_message = "Default should produce 0 resources."
  }
}
