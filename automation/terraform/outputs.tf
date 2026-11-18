output "changes" {
  description = "What this plan/apply changes: GPU => old tenant -> new tenant (empty = the fabric matches the file)."
  value       = local.change_text
}

output "eda_dry_run" {
  description = "terraform plan: EDA's verdict on exactly these changes (GUI → Transactions → the id)."
  value = length(data.http.dry_run) == 0 ? null : {
    transaction = data.core-v1_transaction_summary_result.dry_run[0].id
    success     = data.core-v1_transaction_summary_result.dry_run[0].success
    switches    = sort([for n in coalesce(data.core-v1_transaction_execution_result.dry_run[0].nodes_with_config_changes, []) : n.name])
  }
}

output "gpus" {
  description = "Every GPU in the file: tenant now (EDA label) and where the switches see it."
  value       = { for n, g in local.gpu_by_label : g => "${local.now[n] == null ? "free" : lookup(local.tenant_name, local.now[n], local.now[n])} on ${local.member[n].node} ${local.port_ifname[n]} (LLDP: ${local.gpu_by_lldp[n] == "" ? "no neighbour" : local.gpu_by_lldp[n]})" if g != "" }
}
