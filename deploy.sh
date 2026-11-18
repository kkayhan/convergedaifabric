#!/usr/bin/env bash
# aifabric — GPU AI-backend demo for Nokia EDA 26.8 (built on 26.8.2), EDA namespace aifabric.
#   ./deploy.sh [--yes] [--phase] [P]     asks for the EDA URL, admin login, kubeconfig (lib/connect.sh), checks the
#                                         target (preflight), then runs phase P
#   demo.py --cluster talos aifabric deploy [--phase P]    build host: no questions (pinned mode)
#   phases: ns|topology|backend|qos|frontend|hosts|orchestrator|k8s|grafana|verify|core|network|all
# core = ns -> topology -> backend -> qos -> hosts -> orchestrator -> grafana -> verify (ONE aifabrics Backend
#   over all 8 switches, EVPN overlay, owner 2026-10-27; no VLAN 2 services). The IPv4 example Configlets
#   (manifests/gpu-ipv4.yaml) are NOT deployed: applied by hand (owner Q1 B 2026-11-02, RUNBOOK scene 11).
# network = core + frontend (VLAN 2 VirtualNetworks), no Kubernetes (owner 2026-10-27: network focus for now;
#   GPU bookings = Interface labels by hand or orchestrator.py, which then runs in direct mode).
# all = network + k8s (simulated OpenShift: management cluster, Kamaji hosted tenant control planes, tenant
#   workers, Kueue, in-cluster orchestrator = k8s.sh all) before verify. Default = all where k8s.sh exists (build
#   host), else network (the published bundle carries no Kubernetes layer, owner Q3 A 2026-11-03).
# Every phase is idempotent except `topology`, which deletes + re-creates the lab
# (NetworkTopology is one-shot); `core` skips `topology` when the lab already exists.
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/lib/connect.sh"
source "$HERE/lib/common.sh"
NS=aifabric
YES=""
PHASE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES=yes ;;
    --phase)  ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) PHASE="$1" ;;
  esac
  shift
done
[ -n "$PHASE" ] || { [ -f "$HERE/k8s.sh" ] && PHASE=all || PHASE=network; }
aif_connect deploy "$YES" || exit 1
K="$KUBECTL"
if [ "$AIF_MODE" = interactive ]; then         # demo.py keeps its own log on the build host
  LOGF="$AIF_STATE/deploy-$(date +%Y%m%d-%H%M%S).log"
  exec > >(tee -a "$LOGF") 2>&1
  echo "log: $LOGF"
fi
SWITCHES="l2-1 l2-2 l1-1 l1-2 l1-3 l1-4 l1-5 l1-6"
GPU_VMS=$(for s in 1 2 3 4; do for g in 0 1 2 3; do printf 'gpu-%s-g%s ' $s $g; done; done)   # one sim per GPU (per-GPU tenancy)
SERVERS="$GPU_VMS ces-1 axelera-1 ocp-m1 ocp-m2 ocp-m3 ocp-s1 ocp-s2 ocp-s3 cpu-1"

wait_server_pods(){
  local want; want=$(wc -w <<<"$SERVERS")
  log "wait for $want server sim pods Running"
  for _ in $(seq 1 60); do
    local r; r=$($K -n eda-system get pods -l cx-node-namespace=$NS,cx-kind=linux --no-headers 2>/dev/null | grep -c ' Running ' || true)
    echo "  server pods Running=$r/$want"; [ "$r" -ge "$want" ] && return 0; sleep 10
  done; err "server sim pods not Running"; return 1
}

phase_ns(){
  log "PHASE ns — bootstrap EDA namespace $NS from eda (pools + NodeProfiles)"
  bootstrap_ns "$NS" eda
  $K -n "$NS" get nodeprofile srlinux-ghcr-26.7.1 --no-headers || { err "NodeProfile srlinux-ghcr-26.7.1 missing"; return 1; }
}

clear_backend_children(){
  # Everything layered on the Backend blocks its delete (dry-runs 2026-11-02): every Configlet (QoS + gpu-ipv4)
  # = "cannot delete as there are children present" on its derived objects (1125: 48 errors, 1127 without the
  # 12 gpu-ipv4: 36), the frontend VirtualNetworks = "missing dependency of type SystemInterfaceState" (1122).
  # All Configlets + frontend + Backend in ONE dry-run = success (1131) => remove them first, wait for EDA.
  local n
  $K -n "$NS" delete configlets.config.eda.nokia.com --all --ignore-not-found 2>/dev/null
  $K delete -f "$HERE/manifests/frontend.yaml" --ignore-not-found 2>/dev/null
  for _ in $(seq 1 45); do
    n=$( { edactl_cmd -n "$NS" get configlets; edactl_cmd -n "$NS" get virtualnetworks; } 2>/dev/null | grep -cvE '^NAME|^Error|^$')
    [ "$n" -eq 0 ] && return 0; sleep 2
  done
  warn "$n Configlet(s)/VirtualNetwork(s) still present after 90 s (the Backend delete may fail; the namespace delete clears it)"
}

phase_topology(){
  log "PHASE topology — delete + create NetworkTopology aifabric (8x H5-64D srl 26.7.1, 84 ISLs, 16 GPU VMs + 9 island hosts)"
  python3 "$HERE/topology/gen-topology.py" >/dev/null
  clear_backend_children
  $K -n "$NS" delete backends.aifabrics.eda.nokia.com --all --ignore-not-found 2>/dev/null
  $K -n "$NS" delete networktopology aifabric --ignore-not-found
  $K -n "$NS" delete toponode,topolink,simnode --all --ignore-not-found 2>/dev/null
  for _ in $(seq 1 40); do
    local n; n=$($K -n eda-system get pods -l cx-node-namespace=$NS --no-headers 2>/dev/null | wc -l)
    [ "$n" -eq 0 ] && break; echo "  old lab pods left=$n"; sleep 8
  done
  $K apply -f "$HERE/topology/grouping.yaml"   # topology view tiers from the short role label (eda-system)
  $K create -f "$HERE/topology/aifabric-topo.yaml"
  # the first NT create on a fresh namespace can be swallowed silently (eda-lab) => retry once
  for try in 1 2; do
    sleep 60
    [ "$($K -n "$NS" get toponodes --no-headers 2>/dev/null | wc -l)" -gt 0 ] && break
    warn "NetworkTopology not expanded after 60 s (try $try) — delete + re-create"
    $K -n "$NS" delete networktopology aifabric --ignore-not-found
    $K create -f "$HERE/topology/aifabric-topo.yaml"
  done
  wait_toponodes_synced "$NS" 8 1200 || return 1
  wait_server_pods || return 1
}

phase_backend(){
  log "PHASE backend — aifabrics Backend aifabric on all 8 switches (EVPN + RoCEv2 QoS; stripes gpu1 = 4 rail leaves, non-gpu = l1-5/l1-6; spines; tenant-1/tenant-2)"
  $K apply -f "$HERE/manifests/backend.yaml" || return 1
  wait_resource_field "$NS" backends.aifabrics.eda.nokia.com/aifabric '{.status.operationalState}' Up 600 || return 1
}

phase_qos(){
  # 4-class inference QoS (owner 2026-10-26: Q1 B bulk class, Q2 A Configlet 1:1 RDMA:frontend, Q3 B hosts
  # mark): frontend policies + Configlets over the Backend-derived scheduler/classifiers => after backend,
  # before frontend (its VLANs + IRBs reference frontend-ingress). manifests/qos.yaml.
  local n
  log "PHASE qos — classes control CS6 / RDMA AF31 / serving 0 / bulk AF11, scheduler fc6 strict 10 %, WRR 50:50:10"
  $K apply -f "$HERE/manifests/qos.yaml" || return 1
  # kubectl apply returns before EDA has committed the objects (one transaction each) => wait for them
  # (fresh deploy 2026-10-26: checked at once = 0 found, all 3 present seconds later)
  for _ in $(seq 1 30); do
    n=$(edactl_cmd -n "$NS" get configlets 2>/dev/null | grep -cE '^(backend-qos-inference|qos-gpu-leaves|qos-non-gpu-leaves) ?')
    [ "$n" -eq 3 ] && break; sleep 2
  done
  [ "$n" -eq 3 ] || { err "qos: expected 3 Configlets, found $n"; return 1; }
}

phase_frontend(){
  log "PHASE frontend — VLAN 2: fe-services (shared) + fe-tenant-1/2 VirtualNetworks, local route leaking services<->tenants, on the Backend's EVPN"
  $K apply -f "$HERE/manifests/frontend.yaml" || return 1
  local vn rc=0
  for vn in fe-services fe-tenant-1 fe-tenant-2; do
    wait_resource_field "$NS" virtualnetworks.services.eda.nokia.com/$vn '{.status.operationalState}' Up 300 || rc=1
  done
  # VLAN selectors that match nothing fail silently (TOTAL SUBIF 0) => assert the attachment counts
  # baseline booking (topology/gen-topology.py): tenant-1 6 GPU ports, tenant-2 6 GPU ports, services 9 hosts x 2 legs
  # (cpu-1 shared since 2026-10-27); the 4 free GPU ports carry no VLAN 2
  local want="fe-services-vlan2=18 fe-tenant-1-vlan2=6 fe-tenant-2-vlan2=6" got
  got=$(edactl_cmd -n "$NS" get vlans 2>/dev/null | awk 'NR>1 {print $1"="$4}' | sort | tr '\n' ' ')
  echo "  VLAN 2 subinterfaces: $got (want $want with the baseline booking)"
  return $rc
}

phase_hosts(){
  log "PHASE hosts — GPU VMs (host agent: VLAN 1 IPv6 by SLAAC from the port's fixed /64 + IPv4 where an example Configlet sits; VLAN 2 static; free: parked) + storage-island bonds + frontend VLAN 2 (if deployed)"
  KUBECTL="$K" bash "$HERE/hosts.sh"
}

phase_orchestrator(){
  log "PHASE orchestrator — EDA user gpu-orchestrator (Role aifabric:gpu-orchestrator: write Interfaces only)"
  python3 "$HERE/orchestrator.py" setup
}

phase_k8s(){
  log "PHASE k8s — simulated OpenShift inside CX (k8s.sh all)"
  KUBECTL="$K" bash "$HERE/k8s.sh" all
}

phase_grafana(){
  # Live topology of the GPU backend (owner 2026-10-26: "only the dynamic topology", like the
  # upgrade-pipeline Grafana): own Export CRs (eda-system, namespace aifabric only) + own
  # Prometheus + Grafana in k8s ns $NS, published via HttpProxy aifabric-grafana.
  local T="$HERE/telemetry" psum gsum
  log "PHASE grafana — Exports + Prometheus + Grafana in ns $NS, dashboard 'AI fabric GPU backend'"
  python3 "$T/build-dashboard.py" || return 1
  if ! $K get crd exports.prom.eda.nokia.com >/dev/null 2>&1; then
    log "  Prometheus exporter app missing — AppInstaller prom.eda.nokia.com ${AIF_PROM_APP_VERSION:-v7.0.1}"
    local ai; ai=$(sed "s/__PROM_APP_VERSION__/${AIF_PROM_APP_VERSION:-v7.0.1}/" "$T/apps.yaml" | $K create -f - -o name) || return 1
    for _ in $(seq 1 60); do
      [ "$($K -n eda-system get "$ai" -o jsonpath='{.status.result}' 2>/dev/null)" = Completed ] \
        && $K get crd exports.prom.eda.nokia.com >/dev/null 2>&1 && break; sleep 5
    done
    $K get crd exports.prom.eda.nokia.com >/dev/null 2>&1 || { err "Prometheus exporter app not installed ($ai)"; return 1; }
  fi
  $K apply -f "$T/exports.yaml" || return 1
  $K -n "$NS" create configmap aif-prometheus-config --from-file="$T/prometheus.yml" \
    --dry-run=client -o yaml | $K apply -f - >/dev/null
  $K -n "$NS" create configmap aif-grafana-provisioning --from-file="$T/grafana/datasource.yml" \
    --from-file="$T/grafana/dashboards.yml" --dry-run=client -o yaml | $K apply -f - >/dev/null
  # dashboard JSON > 256 KB = too big for client-side apply's last-applied annotation
  # (telemetry-stack §22) => server-side apply
  $K -n "$NS" create configmap aif-grafana-dashboards --from-file="$T/grafana/aifabric-topology.json" \
    --dry-run=client -o yaml | $K apply --server-side --force-conflicts --field-manager=aifabric-deploy -f - >/dev/null || return 1
  # GF_SERVER_ROOT_URL must name the EDA address the browser uses (__EDA_URL__ in stack.yaml)
  sed "s#__EDA_URL__#$EDA_URL#" "$T/stack.yaml" | $K apply -f - || return 1
  $K apply -f "$T/httpproxy.yaml" || return 1
  # Restart a pod ONLY when its config changed (checksum annotation on the pod template;
  # unchanged => no-op patch). WHY: Prometheus history lives in an emptyDir. The dashboard JSON
  # needs no restart: its directory mount updates live (kubelet <= ~90 s + provider 10 s).
  psum=$(sha1sum "$T/prometheus.yml" | cut -c1-12)
  gsum=$( (cat "$T/grafana/datasource.yml" "$T/grafana/dashboards.yml" "$T/stack.yaml"; echo "$EDA_URL") | sha1sum | cut -c1-12)
  $K -n "$NS" patch deploy prometheus -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"aifabric/config\":\"$psum\"}}}}}" | sed 's/^/  /'
  $K -n "$NS" patch deploy grafana -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"aifabric/config\":\"$gsum\"}}}}}" | sed 's/^/  /'
  $K -n "$NS" rollout status deploy/prometheus --timeout=180s || return 1
  $K -n "$NS" rollout status deploy/grafana --timeout=240s || return 1
  echo "  URL: $EDA_URL/core/httpproxy/v1/aifabric-grafana/d/aifabric-topology/"
}

phase_verify(){
  log "PHASE verify — Backend, deviations, tenant VRFs, traffic matrix"
  local rc=0 st dev
  st=$($K -n "$NS" get backends.aifabrics.eda.nokia.com aifabric -o jsonpath='{.status.operationalState}' 2>/dev/null)
  echo "  Backend aifabric operationalState=$st"; [ "$st" = Up ] || rc=1
  dev=$($K -n "$NS" get deviations.core.eda.nokia.com --no-headers 2>/dev/null | wc -l)
  echo "  deviations=$dev"; [ "$dev" -eq 0 ] || rc=1
  python3 "$HERE/orchestrator.py" show || rc=1
  KUBECTL="$K" python3 "$HERE/aifabric-traffic.py" --once || rc=1
  # k8s layer present (management nodes run kindest/node) => all 3 clusters Ready + orchestrator up
  if $K -n "$NS" get simnode ocp-m1 -o jsonpath='{.spec.containerImage}' 2>/dev/null | grep -q kindest; then
    KUBECTL="$K" bash "$HERE/k8s.sh" status || rc=1
  fi
  if [ $rc -eq 0 ]; then
    log "aifabric ready"
    echo "  EDA UI:    $EDA_URL  (namespace selector: aifabric)"
    echo "  dashboard: $EDA_URL/core/httpproxy/v1/aifabric-grafana/d/aifabric-topology/"
  else
    err "verify found problems (rc=$rc)"
  fi
  return $rc
}

case "$PHASE" in
  ns)           phase_ns ;;
  topology)     phase_topology ;;
  backend)      phase_backend ;;
  qos)          phase_qos ;;
  frontend)     phase_frontend ;;
  k8s)          phase_k8s ;;
  hosts)        phase_hosts ;;
  orchestrator) phase_orchestrator ;;
  grafana)      phase_grafana ;;
  verify)       phase_verify ;;
  core|network|all)
    [ "$PHASE" = all ] && [ ! -f "$HERE/k8s.sh" ] && { err "phase all needs k8s.sh (the Kubernetes layer is not in this bundle): use network"; exit 2; }
    phase_ns || exit 1
    if [ "$($K -n "$NS" get toponodes --no-headers 2>/dev/null | wc -l)" -ge 8 ]; then
      log "topology already present — skipping (run --phase topology to rebuild)"
    else
      phase_topology || exit 1
    fi
    phase_backend || exit 1
    phase_qos || exit 1
    [ "$PHASE" != core ] && { phase_frontend || exit 1; }
    phase_hosts || exit 1
    phase_orchestrator || exit 1
    [ "$PHASE" = all ] && { phase_k8s || exit 1; }
    phase_grafana || exit 1
    phase_verify
    ;;
  *) err "unknown phase '$PHASE'"; exit 2 ;;
esac
