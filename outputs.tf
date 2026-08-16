output "quota_group" {
  description = "The quota group subscription membership resources."
  value       = terraform_data.quota_group
  sensitive   = true
}

output "quota_group_allocations" {
  description = "The quota group allocation resources."
  value       = terraform_data.quota_group_allocations
  sensitive   = true
}
