# ---- the GPU ports under Terraform: Nokia's typed Interface resource, only the `tenant` label managed --------------
# The 16 ports exist already (the topology made them) ⇒ they are IMPORTED, never created. Their key = the Interface
# name, so a recabling changes which tenant a port should carry, never which object Terraform holds.
#   labels  = the live labels without `tenant` + the tenant the file wants (other labels are never touched)
#   ignore_changes: spec, status & co. stay EDA's / the topology's (measured 2026-11-18: without it every plan wants to
#                   rewrite the whole object, because the provider marks every computed field "known after apply")
#   prevent_destroy: a destroy of this resource = DELETE of the switch port's Interface ⇒ never. Releasing GPUs =
#                   `free` in the file. `terraform destroy` is refused on purpose.
import {
  for_each = local.ports
  to       = interfaces-v1_interface.gpu[each.key]
  id       = "${var.namespace}/${each.key}"
}

resource "interfaces-v1_interface" "gpu" {
  for_each = local.ports
  metadata = {
    name        = each.key
    namespace   = var.namespace
    labels      = merge({ for k, v in local.labels[each.key] : k => v if k != "tenant" }, { for k in(local.desired[each.key] == null ? [] : ["tenant"]) : k => local.desired[each.key] })
    annotations = each.value.metadata.annotations
  }
  spec = each.value.spec
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [status, alarms, deviations, spec, name, namespace, api_version, kind]
  }
  depends_on = [terraform_data.inventory_check]
}
