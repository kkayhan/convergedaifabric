# ---- which GPU is on which switch port: EDA's inventory AND the switches must agree --------------------------------
# A GPU port = an EDA Interface with label role=gpu. EDA's inventory names its GPU by the labels gpu-server + rail
# (GPU G of server gpu-<s> = rail G+1); the switch names it by LLDP: Interface status.members[].neighbors = the host
# the leaf sees on that port. Owner decision R2-Q1 B (2026-11-18): STRICT. Every GPU in the file must sit on exactly
# one port where both say the same, else the plan fails and nothing reaches EDA.
data "interfaces-v1_interface_list" "gpu" {
  namespace      = var.namespace
  label_selector = "role=gpu"
}

data "aifabrics-v1_backend" "fabric" {
  namespace = var.namespace
  name      = var.backend
}

locals {
  tenancy = yamldecode(file(var.tenancy_file))

  # filter here as well: an EDA list silently ignores a selector it does not understand
  ports  = { for i in data.interfaces-v1_interface_list.gpu.items : i.metadata.name => i if try(i.metadata.labels["role"], "") == "gpu" }
  labels = { for n, i in local.ports : n => coalesce(i.metadata.labels, {}) }
  member = { for n, i in local.ports : n => i.spec.members[0] }
  # SR Linux names: node + ethernet-1/2/1 (EDA member ethernet-1-2-1)
  port_ifname = { for n, m in local.member : n => "ethernet-${replace(trimprefix(m.interface, "ethernet-"), "-", "/")}" }

  gpu_by_label = { for n, l in local.labels : n => can(tonumber(l["rail"])) && can(l["gpu-server"]) ? "${l["gpu-server"]}-g${tonumber(l["rail"]) - 1}" : "" }
  gpu_by_lldp  = { for n, i in local.ports : n => try(i.status.members[0].neighbors[0].node, "") }

  # tenants = the Backend's GPU isolation groups; their selector names the label value ("role=gpu,tenant=1")
  tenant_value = merge([for g in coalesce(data.aifabrics-v1_backend.fabric.spec.gpu_isolation_groups, []) : {
    for s in coalesce(g.interface_selectors, []) : g.name => regex("(?:^|,)tenant=([^,]+)", s)[0] if can(regex("(?:^|,)tenant=([^,]+)", s))
  }]...)
  tenant_name = { for k, v in local.tenant_value : v => k }

  label_ports = { for g in keys(local.tenancy) : g => sort([for n, x in local.gpu_by_label : "${local.member[n].node} ${local.port_ifname[n]}" if x == g]) }
  lldp_ports  = { for g in keys(local.tenancy) : g => sort([for n, x in local.gpu_by_lldp : "${local.member[n].node} ${local.port_ifname[n]}" if x == g]) }
  problems = concat(
    [for g, t in local.tenancy : "${g}: unknown tenant '${t}' (tenants: ${join(", ", sort(keys(local.tenant_value)))}, or free)" if t != "free" && !contains(keys(local.tenant_value), t)],
    [for g in keys(local.tenancy) : length(local.label_ports[g]) + length(local.lldp_ports[g]) == 0 ? "${g}: unknown GPU (no port in EDA's inventory, no LLDP neighbour of that name)" : "${g}: EDA's labels put it on [${join(", ", local.label_ports[g])}], LLDP sees it on [${join(", ", local.lldp_ports[g])}]"
    if !(length(local.label_ports[g]) == 1 && join(",", local.label_ports[g]) == join(",", local.lldp_ports[g]))],
  )

  # desired tenant label value per port: from the file for the GPU on it; a GPU not in the file keeps its label
  now     = { for n, l in local.labels : n => try(l["tenant"], null) }
  desired = { for n, l in local.labels : n => (contains(keys(local.tenancy), local.gpu_by_label[n]) ? (local.tenancy[local.gpu_by_label[n]] == "free" ? null : lookup(local.tenant_value, local.tenancy[local.gpu_by_label[n]], null)) : try(l["tenant"], null)) }
  changes = { for n, t in local.desired : n => t if t != local.now[n] }
  # "gpu-3-g2: free -> tenant-1"
  change_text = { for n, t in local.changes : local.gpu_by_label[n] => "${local.now[n] == null ? "free" : lookup(local.tenant_name, local.now[n], local.now[n])} -> ${t == null ? "free" : local.tenant_name[t]}" }
}

resource "terraform_data" "inventory_check" {
  input = local.problems
  lifecycle {
    precondition {
      condition     = length(local.problems) == 0
      error_message = "REFUSED, nothing sent to EDA: inventory and switches disagree (fix the cabling or the topology first, or the file):\n${join("\n", local.problems)}"
    }
  }
}
