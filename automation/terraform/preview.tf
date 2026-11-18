# ---- terraform plan = EDA dry run of exactly the label changes --------------------------------------------------------
# Nokia's typed resources cannot dry-run (docs 26.8: they use each app's REST API, not the Transaction API) ⇒ a
# plan-time REST call: POST /core/transaction/v2 {dryRun: true} with the same patches the apply will make, each guarded
# by a JSON patch `test` on the labels this plan read; Nokia's core-v1 data sources read EDA's verdict.
locals {
  kc         = "${var.eda_url}/core/httpproxy/v1/keycloak"
  login_body = "grant_type=password&client_id=eda&scope=openid&client_secret=${urlencode(var.eda_client_secret)}&username=${urlencode(var.eda_username)}&password=${urlencode(var.eda_password)}"
}

# GOTCHA (EDA 26.8.2 Keycloak): concurrent password logins of the SAME user fail (HTTP 400 "Invalid user credentials").
# The Nokia providers log in in parallel (and retry); this login runs after them and retries in sequence.
data "http" "eda_login" {
  url             = "${local.kc}/realms/eda/protocol/openid-connect/token"
  method          = "POST"
  insecure        = true
  request_body    = local.login_body
  request_headers = { Content-Type = "application/x-www-form-urlencoded" }
  depends_on      = [data.interfaces-v1_interface_list.gpu, data.aifabrics-v1_backend.fabric]
}

data "http" "eda_login_retry" {
  count           = data.http.eda_login.status_code == 200 ? 0 : 1
  url             = "${local.kc}/realms/eda/protocol/openid-connect/token"
  method          = "POST"
  insecure        = true
  request_body    = local.login_body
  request_headers = { Content-Type = "application/x-www-form-urlencoded" }
  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "EDA login as ${var.eda_username} failed twice (HTTP ${self.status_code}): ${self.response_body}"
    }
  }
}

locals {
  eda_token = sensitive(jsondecode(data.http.eda_login.status_code == 200 ? data.http.eda_login.response_body : data.http.eda_login_retry[0].response_body).access_token)
  iface_gvk = { group = "interfaces.eda.nokia.com", version = "v1", kind = "Interface" }
  # one patch per changed port; JSON text because the two op shapes differ (add has a value, remove has none)
  patches = [for n, t in local.changes : jsonencode({ type = { patch = {
    target = { gvk = local.iface_gvk, name = n, namespace = var.namespace }
    patchOps = [
      { op = "test", path = "/metadata/labels", value = local.labels[n] },
      jsondecode(t == null ? jsonencode({ op = "remove", path = "/metadata/labels/tenant" }) : jsonencode({ op = "add", path = "/metadata/labels/tenant", value = t })),
    ]
  } } })]
  preview_desc = "terraform plan (dry run): ${join(", ", [for g, c in local.change_text : "${g} ${c}"])}"
}

data "http" "dry_run" {
  count    = length(local.changes) > 0 && length(local.problems) == 0 ? 1 : 0
  url      = "${var.eda_url}/core/transaction/v2"
  method   = "POST"
  insecure = true
  request_headers = {
    Authorization = "Bearer ${local.eda_token}"
    Content-Type  = "application/json"
  }
  request_body = jsonencode({ description = local.preview_desc, dryRun = true, crs = jsondecode("[${join(",", local.patches)}]") })
  lifecycle {
    postcondition {
      condition     = self.status_code == 200
      error_message = "EDA refused the dry run (HTTP ${self.status_code}): ${self.response_body}"
    }
  }
}

data "core-v1_transaction_summary_result" "dry_run" {
  count             = length(data.http.dry_run)
  transaction_id    = jsondecode(data.http.dry_run[0].response_body).id
  wait_for_complete = true
  lifecycle {
    postcondition {
      condition     = self.success == true
      error_message = "The EDA dry run failed (transaction ${self.id}): nothing will be applied. GUI → Transactions → ${self.id} shows why."
    }
  }
}

data "core-v1_transaction_execution_result" "dry_run" {
  count             = length(data.http.dry_run)
  transaction_id    = jsondecode(data.http.dry_run[0].response_body).id
  wait_for_complete = true
  depends_on        = [data.core-v1_transaction_summary_result.dry_run]
}
