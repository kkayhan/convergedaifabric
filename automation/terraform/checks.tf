# ---- after the apply: did the switches and the GPU NICs follow? -------------------------------------------------------
# Terraform does not wait for the fabric to converge: these checks read the switch state and only WARN. They look at
# ports whose label already is what the file wants (a port this plan changes is checked by the NEXT plan): per booked
# GPU port, subinterface 1 sits in the tenant's VRF, and the leaf's IPv6 neighbour table holds the NIC (MAC = the one
# LLDP reports) with a SLAAC address = the GPU took its /64 (same proof as gpu-tenant.py).
# (A check block may hold ONE data source, hence two checks; the LLDP read is a plain data source.)
locals {
  eql_url          = "${var.eda_url}/core/query/v1/eql?namespaces=${var.namespace}&query="
  booked           = { for n, t in local.desired : n => t if t != null && t == local.now[n] }
  tenant_ni_filter = join(" or ", [for k in sort(keys(local.tenant_value)) : ".namespace.node.srl.network-instance.name = \"${k}\""])
}

data "http" "lldp_mac" {
  url             = "${local.eql_url}${urlencode(".namespace.node.srl.system.lldp.interface.neighbor fields [chassis-id]")}"
  insecure        = true
  request_headers = { Authorization = "Bearer ${local.eda_token}" }
}

locals {
  lldp_mac = { for n, m in local.member : n => lower(try([for l in jsondecode(data.http.lldp_mac.response_body).data : l["chassis-id"]
  if l[".namespace.node.name"] == m.node && l[".namespace.node.srl.system.lldp.interface.name"] == local.port_ifname[n]][0], "")) }
}

check "gpu_ports_in_tenant_vrf" {
  data "http" "vrf" {
    url             = "${local.eql_url}${urlencode(".namespace.node.srl.network-instance.interface fields [name] where (${local.tenant_ni_filter})")}"
    insecure        = true
    request_headers = { Authorization = "Bearer ${local.eda_token}" }
  }
  assert {
    condition = alltrue([for n, t in local.booked : contains([for r in try(jsondecode(data.http.vrf.response_body).data, []) :
    r[".namespace.node.srl.network-instance.name"] if r[".namespace.node.name"] == local.member[n].node && r["name"] == "${local.port_ifname[n]}.1"], local.tenant_name[t])])
    error_message = "Not every booked GPU port is in its tenant VRF on the leaf yet (EDA still pushing; run terraform plan again in a few seconds)."
  }
}

check "gpu_nics_addressed" {
  data "http" "nd" {
    url             = "${local.eql_url}${urlencode(".namespace.node.srl.interface.subinterface.ipv6.neighbor-discovery.neighbor fields [link-layer-address] where (.namespace.node.srl.interface.subinterface.index = 1)")}"
    insecure        = true
    request_headers = { Authorization = "Bearer ${local.eda_token}" }
  }
  assert {
    condition = alltrue([for n, t in local.booked : length([for r in try(jsondecode(data.http.nd.response_body).data, []) : r
      if r[".namespace.node.name"] == local.member[n].node && r[".namespace.node.srl.interface.name"] == local.port_ifname[n]
    && !startswith(r["ipv6-address"], "fe80") && lower(r["link-layer-address"]) == local.lldp_mac[n]]) > 0])
    error_message = "Not every booked GPU's NIC has taken an address from its port yet (the leaf learns it from the GPU's first traffic; run terraform plan again in a few seconds)."
  }
}
