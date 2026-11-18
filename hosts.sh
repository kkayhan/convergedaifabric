#!/usr/bin/env bash
# aifabric — host networking inside the server sims (idempotent; sim netns config is lost
# on every pod restart, so re-run after one). Called by deploy.sh phase `hosts`.
# The per-host commands live in host-net.sh (shared with the in-cluster orchestrator's bookings).
#
# GPU VMs gpu-<s>-g<G> (PER-GPU TENANCY, owner 2026-10-26): one sim per GPU, ONE NIC eth1 = the GPU's
#   ConnectX-8 on rail G+1 (leaf l1-<G+1>). Tenant = the `tenant` label of that NIC's Interface in EDA;
#   no label = free (unbooked).
#   VLAN 1 eth1.1 = the VM's own HOST AGENT (host-agent.sh, owner R2-Q2 2026-11-18), armed here once: SLAAC from the
#           leaf's router advertisement (the port's fixed /64, EDAManagedIPv6; host part = EUI-64 of the
#           NIC's MAC), and it follows bookings, moves and releases BY ITSELF (no script in the booking path). This
#           script only checks the result against EDA's /64; + IPv4 = the leaf's /31 + 1 when the port carries one of
#           the example gpu-ipv4 Configlets (owner 2026-11-02), route 10.201.0.0/16 via the leaf.
#   VLAN 2 eth1.2 = static node address in the tenant's frontend subnet, set here (DHCP over the route-leaked
#           frontend does not work, eda-lab § HOST SLAAC) ⇒ `--follow` re-runs this on a label change.
#   free:   VLAN 1 parked by the agent (the Backend renders nothing on an unlabelled GPU port), VLAN 2 parked here.
# Storage-island hosts: 802.3ad bond0 over eth1 (l1-5) + eth2 (l1-6) = partner of the all-active
#   ESI-LAG lag-<host>-local; VLAN 2 on bond0.2.
# FRONTEND VLAN 2 (manifests/frontend.yaml; only when it is deployed): subnet = the host's CURRENT tenant
#   from EDA => 10.2<T>.0.0/24; services hosts (server-role ocp-master|ocp-services|storage|accelerator|cpu;
#   cpu-1 = shared CPU node, owner Q1 B 2026-10-27) => 10.20.0.0/24. Gateway = the leaves' anycast IRB .1; only 10.20.0.0/14 goes there, the default route
#   stays on eth0 (sim pod network = OOB, image pulls, the orchestrator's provisioning ssh).
#   Host octet: gpu-<s>-g<G> 10*s+G (10..43), cpu-1 51, ocp-m1..3 11-13, ocp-s1..3 21-23, ces-1 31,
#   axelera-1 41 (10.20.0.10 = mgmt cluster API VIP).
# ces-1 also runs iperf3 servers on ports 5301-5316 = "model weights" for the inference job (k8s/inference.sh).
# Every server: hostname = sim name + lldpd, so the leaves' LLDP table names it.
#   bash hosts.sh [server ...]     default: all (a GPU server name = its 4 GPU VMs)
#   bash hosts.sh --follow         keep running: when a GPU's `tenant` label changes (by hand, in the EDA UI, with
#                                  kubectl, gpu-tenant.py or Terraform), set that VM's frontend VLAN 2 (VLAN 1 = agent). Stand-in for the host provisioning the
#                                  in-cluster orchestrator does when the Kubernetes layer is up (owner Q2 A 2026-10-27).
set -uo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$HERE/lib/connect.sh"
aif_resolve_kubectl                     # env KUBECTL, else the cluster deploy.sh saved (.state/kubectl)
K="$KUBECTL"
NS=aifabric

if [ "${1:-}" = --follow ]; then
  if $K -n "$NS" get simnode ocp-m1 -o jsonpath='{.spec.containerImage}' 2>/dev/null | grep -q kindest; then
    echo "the Kubernetes layer is up: the in-cluster orchestrator provisions the GPU VMs, not this follower"; exit 2
  fi
  snap(){  # one line per GPU VM: "<vm> tenant-N|free"
    $K -n "$NS" get interfaces.interfaces.eda.nokia.com -l role=gpu -o jsonpath='{range .items[*]}{.metadata.labels.gpu-server}{" "}{.metadata.labels.rail}{" "}{.metadata.labels.tenant}{"\n"}{end}' 2>/dev/null \
      | awk 'NF >= 2 {printf "%s-g%d %s\n", $1, $2 - 1, ($3 == "" ? "free" : "tenant-" $3)}' | sort
  }
  prev=$(snap); [ -n "$prev" ] || { echo "no GPU Interfaces in namespace $NS"; exit 1; }
  echo "$(date +%T) following the tenant label of $(wc -l <<<"$prev") GPU Interfaces every 2 s (Ctrl-C to stop)"
  while sleep 2; do
    cur=$(snap); [ -n "$cur" ] || continue          # API hiccup: try again
    changed=$(comm -13 <(echo "$prev") <(echo "$cur"))
    prev=$cur
    while read -r vm t; do
      [ -n "$vm" ] || continue
      echo "$(date +%T) $vm -> $t: re-addressing the VM"
      for try in $(seq 1 10); do                    # a new booking's /64 reaches EDA's state a few seconds after the label
        out=$(KUBECTL="$K" bash "$HERE/hosts.sh" "$vm" 2>&1) && break
        sleep 3
      done
      grep -E 'backend|frontend eth|no prefix|not ready|FAILED' <<<"$out" | sed 's/^ */  /'
    done <<<"$changed"
  done
  exit 0
fi
GPU_SERVERS="gpu-1:1:1 gpu-2:1:2 gpu-3:2:1 gpu-4:2:2"          # server:port:channel on every rail leaf
ISLAND="ces-1 axelera-1 ocp-m1 ocp-m2 ocp-m3 ocp-s1 ocp-s2 ocp-s3 cpu-1"
ONLY=" $* "
rc=0

want(){ [ "$ONLY" = "  " ] || [[ "$ONLY" == *" $1 "* ]]; }

sim_run(){  # sim_run <server> <calls>: host-net.sh + calls on stdin
  { cat "$HERE/host-net.sh"; echo "$2"; } | $K -n eda-system exec -i "deploy/cx-${NS}--$1-sim" -c "$1" -- sh -s
}

install_agent(){  # install_agent <gpu vm>: host-net.sh + host-agent.sh into /etc/aifabric (the agent re-sources them)
  { echo "mkdir -p /etc/aifabric"
    for f in host-net.sh host-agent.sh; do echo "cat > /etc/aifabric/$f <<'AIFABRIC_EOF'"; cat "$HERE/$f"; echo AIFABRIC_EOF; done
  } | $K -n eda-system exec -i "deploy/cx-${NS}--$1-sim" -c "$1" -- sh -s
}

wait_ports(){  # wait_ports <server> <n>: cxdp plugs ethN ~60 s after the pod is Running
  local s="$1" n="$2" i
  $K -n eda-system rollout status "deploy/cx-${NS}--${s}-sim" --timeout=300s >/dev/null 2>&1 || return 1
  for i in $(seq 1 30); do
    $K -n eda-system exec "deploy/cx-${NS}--$s-sim" -c "$s" -- sh -c \
      "for i in \$(seq 1 $n); do test -e /sys/class/net/eth\$i || exit 1; done" 2>/dev/null && return 0
    sleep 5
  done
  return 1
}

# leaf/interface -> /64 of the GPU subinterface (index 1), from EDA's view of the switches (booked ports only;
# leaf = ::1 of the port's /64 = the GPU's gateway; link-local fe80 rows skipped)
PFX=$($K -n eda-system exec deploy/eda-toolbox -- edactl -n "$NS" query \
  '.namespace.node.srl.interface.subinterface.ipv6.address fields [ip-prefix] where (.namespace.node.srl.interface.subinterface.index = 1)' 2>/dev/null \
  | awk '$5 !~ /^fe80/ && $5 ~ /::1\/64$/ {sub(/::1\/64$/, "", $5); print $2, $3, $5}')
[ -n "$PFX" ] || { echo "no GPU subinterface prefixes in EDA yet (Backend not Up?)"; exit 1; }
# leaf/interface -> leaf-side IPv4 /31 of the GPU subinterface (gpu-ipv4 Configlets; empty when there are none)
PFX4=$($K -n eda-system exec deploy/eda-toolbox -- edactl -n "$NS" query \
  '.namespace.node.srl.interface.subinterface.ipv4.address fields [ip-prefix] where (.namespace.node.srl.interface.subinterface.index = 1)' 2>/dev/null \
  | awk '$5 ~ /^10\.201\./ {print $2, $3, $5}')

# frontend deployed? + Interface labels: <gpu-server or server>|<rail>|<tenant>|<server-role>
FE=$($K -n "$NS" get virtualnetworks.services.eda.nokia.com fe-services fe-tenant-1 fe-tenant-2 --ignore-not-found --no-headers 2>/dev/null | wc -l)
LBL=$($K -n "$NS" get interfaces.interfaces.eda.nokia.com -l 'role in (gpu,server)' -o jsonpath='{range .items[*]}{.metadata.labels.gpu-server}{.metadata.labels.server}{"|"}{.metadata.labels.rail}{"|"}{.metadata.labels.tenant}{"|"}{.metadata.labels.server-role}{"\n"}{end}' 2>/dev/null)
declare -A OCTET=([cpu-1]=51 [ocp-m1]=11 [ocp-m2]=12 [ocp-m3]=13 [ocp-s1]=21 [ocp-s2]=22 [ocp-s3]=23 [ces-1]=31 [axelera-1]=41)
label(){  # label <server> <rail|""> -> tenant | svc | "" (free)
  awk -F'|' -v h="$1" -v r="$2" '$1==h && $2==r {
      if ($4 ~ /^(ocp-master|ocp-services|storage|accelerator|cpu)$/) { print "svc"; exit }
      print $3; exit }' <<<"$LBL"
}
fe_call(){  # fe_call <host> <dev> <parent> <tenant|svc|"">: hn_frontend / hn_frontend_park line
  local net
  [ "$FE" -gt 0 ] || return 0
  case "$4" in
    svc)   net=20 ;;
    [0-9]) net=$((20 + $4)) ;;
    *)     echo "hn_frontend_park $2"; return 0 ;;
  esac
  echo "hn_frontend $2 $3 10.$net.0.${OCTET[$1]}/24 10.$net.0.1"
}

for entry in $GPU_SERVERS; do
  IFS=: read -r s port ch <<<"$entry"
  for g in 0 1 2 3; do
    vm="$s-g$g"; r=$((g + 1))
    want "$vm" || want "$s" || continue
    t=$(label "$s" "$r")
    OCTET[$vm]=$(( ${s#gpu-} * 10 + g ))
    echo "--> $vm (GPU$g of $s, eth1 -> l1-$r ethernet-1/$port/$ch, tenant ${t:-free})"
    wait_ports "$vm" 1 || { echo "    $vm: pod or eth1 not ready"; rc=1; continue; }
    # VLAN 1 = the VM's own host agent (host-agent.sh, owner R2-Q2 2026-11-18): it follows what the leaf advertises,
    # so a booking needs nothing here; this run only (re)arms it and checks it against EDA's /64.
    want_pfx=-
    v4call="hn_v4_example eth1"
    if [ -n "$t" ]; then
      want_pfx=$(awk -v l="l1-$r" -v i="ethernet-1/$port/$ch" '$1==l && $2==i {print $3}' <<<"$PFX")
      [ -n "$want_pfx" ] || { echo "    $vm: tenant-$t but no prefix on l1-$r ethernet-1/$port/$ch yet"; rc=1; continue; }
      v4=$(awk -v l="l1-$r" -v i="ethernet-1/$port/$ch" '$1==l && $2==i {print $3}' <<<"$PFX4")
      if [ -n "$v4" ]; then            # leaf 10.201.r.2k/31 => GPU 10.201.r.2k+1/31, gw = the leaf
        gw=${v4%/*}; v4call="$v4call ${gw%.*}.$(( ${gw##*.} + 1 ))/31 $gw"
      fi
    fi
    install_agent "$vm" || { echo "    $vm: could not install the host agent"; rc=1; continue; }
    sim_run "$vm" ". /etc/aifabric/host-agent.sh
hn_arm eth1 $vm
hn_state eth1 $want_pfx
$v4call
$(fe_call "$vm" eth1.2 eth1 "$t")" || rc=1
  done
done

for s in $ISLAND; do
  want "$s" || continue
  echo "--> $s (bond0 = eth1 l1-5 + eth2 l1-6, LACP)"
  wait_ports "$s" 2 || { echo "    $s: pod or eth1/eth2 not ready"; rc=1; continue; }
  extra=""
  [ "$s" = ces-1 ] && extra='pkill -x iperf3 2>/dev/null; for p in $(seq 5301 5316); do iperf3 -s -D -p $p >/dev/null 2>&1; done
    echo "    model-weights servers: iperf3 on ports 5301-5316 (inference job, k8s/inference.sh)"'
  sim_run "$s" "hn_lldp $s
    ip link show bond0 >/dev/null 2>&1 || ip link add bond0 type bond mode 802.3ad miimon 100 lacp_rate fast xmit_hash_policy layer3+4
    for m in eth1 eth2; do
      [ \"\$(basename \"\$(readlink /sys/class/net/\$m/master 2>/dev/null)\" 2>/dev/null)\" = bond0 ] || { ip link set \$m down; ip link set \$m master bond0; }
      ip link set \$m up
    done
    ip link set bond0 up
    echo \"    bond0 slaves: \$(cat /sys/class/net/bond0/bonding/slaves)\"
$(fe_call "$s" bond0.2 bond0 "$(label "$s" "")")
$extra" || rc=1
done
exit $rc
