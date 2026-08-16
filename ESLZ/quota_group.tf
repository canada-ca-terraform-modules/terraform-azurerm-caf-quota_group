module "quota_group" {
  source = "github.com/canada-ca-terraform-modules/terraform-azurerm-caf-quota_group?ref=v1.1.0"

  subscription_id = var.subscription_id
  quota_group     = var.quota_group
}
