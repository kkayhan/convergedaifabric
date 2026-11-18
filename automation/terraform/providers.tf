# Each Nokia provider logs in to EDA's Keycloak by itself (password grant as var.eda_username through client `eda`).
# With the client secret given, no Keycloak admin login is needed.
# GOTCHA core-v1 1.3.0 names its settings eda_username / eda_password / eda_client_secret; the app providers (1.1.0)
# call them username / password / client_secret (terraform providers schema, 2026-11-12).
provider "interfaces-v1" {
  base_url        = var.eda_url
  tls_skip_verify = true # lab: EDA's self-signed certificate
  username        = var.eda_username
  password        = var.eda_password
  client_secret   = var.eda_client_secret
}

provider "aifabrics-v1" {
  base_url        = var.eda_url
  tls_skip_verify = true
  username        = var.eda_username
  password        = var.eda_password
  client_secret   = var.eda_client_secret
}

provider "core-v1" {
  base_url          = var.eda_url
  tls_skip_verify   = true
  eda_username      = var.eda_username
  eda_password      = var.eda_password
  eda_client_secret = var.eda_client_secret
}
