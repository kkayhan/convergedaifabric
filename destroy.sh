#!/usr/bin/env bash
# aifabric — remove the whole lab: Backend(s), NetworkTopology(ies), the EDA
# namespace (switches, servers, pools, the dashboard's Grafana + Prometheus), the demo's
# EDA API user, and the dashboard's HttpProxy + Export CRs in eda-system.
# Kept: the Prometheus exporter app (others may use it) and the files on disk.
#   ./destroy.sh [--yes]                       asks for the EDA URL, admin login, kubeconfig (saved answers = defaults)
#   demo.py --cluster talos aifabric destroy   build host: no questions
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/lib/connect.sh"
source "$HERE/lib/common.sh"
NS=aifabric
YES=""
case "${1:-}" in --yes|-y) YES=yes ;; -h|--help) sed -n '2,7p' "$0"; exit 0 ;; esac
aif_connect destroy "$YES" || exit 1
K="$KUBECTL"

log "delete what sits on the Backend first: all Configlets (QoS + gpu-ipv4) + the frontend VirtualNetworks"
log "  (each blocks the Backend's delete; all together + Backend = clean, dry-run 1131, 2026-11-02)"
$K -n "$NS" delete configlets.config.eda.nokia.com --all --ignore-not-found 2>/dev/null
$K delete -f "$HERE/manifests/frontend.yaml" --ignore-not-found 2>/dev/null
for _ in $(seq 1 45); do
  n=$( { edactl_cmd -n "$NS" get configlets; edactl_cmd -n "$NS" get virtualnetworks; } 2>/dev/null | grep -cvE '^NAME|^Error|^$')
  [ "$n" -eq 0 ] && break; sleep 2
done

log "delete Backends in $NS (frees the per-tenant VRFs + underlay before the switches go)"
$K -n "$NS" delete backends.aifabrics.eda.nokia.com --all --ignore-not-found 2>/dev/null

log "delete NetworkTopologies in $NS via kubectl (also removes their workflow records;"
log "  a stale record makes the next create of the same name a silent no-op)"
$K -n "$NS" delete networktopologies.topologies.eda.nokia.com --all --ignore-not-found 2>/dev/null

log "delete the gpu-terraform EDA user + the local Terraform state of automation/terraform (the ports go with the lab)"
python3 "$HERE/automation/terraform/setup.py" remove || warn "Terraform user cleanup failed (continuing)"
log "delete the gpu-orchestrator EDA user, group + Role (before the namespace holding the Role)"
python3 "$HERE/orchestrator.py" remove || warn "orchestrator user cleanup failed (continuing)"

log "delete the eda-system objects: dashboard HttpProxy + Export CRs, topology grouping (Grafana/Prometheus pods go with ns $NS)"
$K -n eda-system delete httpproxy aifabric-grafana --ignore-not-found
$K delete -f "$HERE/telemetry/exports.yaml" --ignore-not-found 2>/dev/null
$K delete -f "$HERE/topology/grouping.yaml" --ignore-not-found 2>/dev/null   # TopologyGrouping spine-leaf (eda-system)

log "delete EDA namespace $NS (edactl removes the EDB record + k8s ns + pools)"
if $K -n eda-system get namespaces.core.eda.nokia.com "$NS" >/dev/null 2>&1; then
  edactl_cmd delete namespace "$NS" || $K -n eda-system delete namespaces.core.eda.nokia.com "$NS" --ignore-not-found
fi
$K wait --for=delete ns/"$NS" --timeout=300s 2>/dev/null || true
for _ in $(seq 1 36); do
  n=$($K -n eda-system get pods -l cx-node-namespace=$NS --no-headers 2>/dev/null | wc -l)
  [ "$n" -eq 0 ] && break; echo "  lab pods left=$n"; sleep 10
done
left=$(edactl_cmd workflow get -A 2>/dev/null | awk -v ns="$NS" '$2==ns' | wc -l)
[ "$left" -eq 0 ] || warn "$left workflow record(s) still listed for ns $NS: edactl workflow get -A | grep $NS"
log "aifabric torn down (files kept on disk)"
