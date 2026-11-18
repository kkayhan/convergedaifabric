variable "tenancy_file" {
  description = "Desired state: one line per GPU, gpu-<server>-g<index>: tenant-N | free (shared with gpu-tenant.py)."
  type        = string
  default     = "../tenancy.yaml"
}

variable "eda_url" {
  description = "EDA address, e.g. https://10.1.2.3 (setup.py writes it into terraform.tfvars)."
  type        = string
}

variable "eda_client_secret" {
  description = "Secret of EDA's Keycloak client `eda` (setup.py reads it once; Terraform then needs no Keycloak admin)."
  type        = string
  sensitive   = true
}

variable "eda_username" {
  description = "EDA user Terraform works as: gpu-terraform = Role aifabric:gpu-orchestrator (read, write Interfaces only)."
  type        = string
  default     = "gpu-terraform"
}

variable "eda_password" {
  type      = string
  default   = "gpu-terraform"
  sensitive = true
}

variable "namespace" {
  type    = string
  default = "aifabric"
}

variable "backend" {
  description = "The aifabrics Backend whose GPU isolation groups are the tenants."
  type        = string
  default     = "aifabric"
}
