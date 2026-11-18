# aifabric: which tenant owns which GPU, as Terraform (owner Q2 A 2026-11-18: Nokia's own EDA providers).
# Desired state = ../tenancy.yaml (the same file automation/gpu-tenant.py applies). Terraform manages the 16 GPU
# switch ports (EDA Interfaces) and only their `tenant` label; the ports themselves belong to the topology.
#   python3 setup.py          once: EDA user gpu-terraform, terraform.tfvars, terraform init
#   terraform plan            EDA dry run of exactly the label changes + the switches they touch
#   terraform apply           one EDA transaction per changed GPU port (Nokia's providers use each app's REST API)
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    interfaces-v1 = { source = "nokia-eda/interfaces-v1", version = "1.1.0" } # the GPU ports: read + labels
    aifabrics-v1  = { source = "nokia-eda/aifabrics-v1", version = "1.1.0" }  # the Backend: which tenants exist
    core-v1       = { source = "nokia-eda/core-v1", version = "1.3.0" }       # dry-run verdict
    http          = { source = "hashicorp/http", version = "3.6.3" }          # dry run + switch state (EDA REST)
  }
}
