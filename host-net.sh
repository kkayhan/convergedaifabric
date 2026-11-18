# aifabric — host network functions, sourced ON a server (POSIX sh: kindest/node = dash, multitool = ash).
# ONE source for both callers, so a booking re-provisions a GPU VM exactly like the deploy does:
#   hosts.sh                    deploy / repair:  { cat host-net.sh; echo "<calls>"; } | kubectl exec -i … sh -s
#   k8s/gpu-orchestrator.py     book / release:   same text over ssh on the VM's out-of-band eth0
#
#   hn_lldp <name>                                hostname + lldpd (the leaves' LLDP table names the host)
#   hn_backend <parent> <prefix> [<v4> <v4gw>]    GPU NIC VLAN 1 (Backend gpuVLAN) by SLAAC (owner 2026-11-03): the
#                                                 leaf's router advertisement brings the /64 (from the tenant's
#                                                 pool) + the default route (leaf link-local); host part = EUI-64
#                                                 from the NIC's MAC (owner Q1 B). <prefix> = the /64 EDA allocated,
#                                                 only checked.
#                                                 + IPv4 <v4> (/31), route 10.201.0.0/16 via <v4gw> when the port
#                                                 carries an example gpu-ipv4 Configlet (owner 2026-11-02)
#   hn_backend_park <parent>                      unbooked GPU: no VLAN 1 address (the Backend parked the port);
#                                                 SLAAC stays armed for the next booking
#   hn_frontend <dev> <parent> <cidr> <gw>        VLAN 2 node address + route 10.20.0.0/14 + QoS marking
#   hn_frontend_park <dev>                        no VLAN 2 address, no frontend route, no marking table
# QoS marking (owner 2026-10-26 Q3 B: hosts mark, switches trust; Q4 B DNS; classes = manifests/qos.yaml):
#   nftables table inet aifabric_qos on the VLAN 2 device. postrouting: TCP/UDP to/from the cluster-control
#   ports => CS6; to/from the storage node => AF11; anything else keeps the sender's marking. forward: traffic
#   the node forwards for its pods is reset to DSCP 0 first (owner Q1 A: a pod cannot promote itself).
QOS_CONTROL_PORTS="53, 2379, 2380, 6443, 8132, 10250"          # DNS, etcd, API servers, konnectivity, kubelet
QOS_STORAGE=10.20.0.31                                         # ces-1 => AF11 (bulk)
HN_RA_METRIC=512                                               # GPU NIC's RA default route: below eth0's 1024

hn_lldp() {
  hostname "$1" 2>/dev/null || true
  pgrep -x lldpd >/dev/null || lldpd -I "eth[1-9]*" >/dev/null 2>&1
  sleep 1; lldpcli configure lldp portidsubtype ifname >/dev/null 2>&1
  # the LLDP system name = this host, even if an override was configured in lldpd (e.g. a miscabling test)
  lldpcli configure system hostname "$1" >/dev/null 2>&1; lldpcli update >/dev/null 2>&1; true
}

hn_slaac() {  # <parent>: VLAN 1 subinterface that takes the leaf's router advertisements, host part from the MAC
  echo 0 > /proc/sys/net/ipv6/conf/all/accept_ra; echo 0 > /proc/sys/net/ipv6/conf/default/accept_ra   # eth0, eth1: never
  ip link set "$1" up
  ip link show "$1.1" >/dev/null 2>&1 || ip link add link "$1" name "$1.1" type vlan id 1
  echo 2 > "/proc/sys/net/ipv6/conf/$1.1/accept_ra"        # 2 = also when forwarding (kindest/node workers route pods)
  echo 1 > "/proc/sys/net/ipv6/conf/$1.1/autoconf"
  # Plain SLAAC (owner Q1 B 2026-11-03): host part = EUI-64 of the MAC (addr_gen_mode 0), no temporary privacy
  # addresses, no token. Each written only when it differs: a new addr_gen_mode regenerates the link-local, and
  # `ip token set` (even to the same value) expires every tokenized address = 1.7 s outage (measured 2026-11-03).
  [ "$(cat "/proc/sys/net/ipv6/conf/$1.1/addr_gen_mode")" = 0 ] || echo 0 > "/proc/sys/net/ipv6/conf/$1.1/addr_gen_mode"
  [ "$(cat "/proc/sys/net/ipv6/conf/$1.1/use_tempaddr")" = 0 ] || echo 0 > "/proc/sys/net/ipv6/conf/$1.1/use_tempaddr"
  ip token list dev "$1.1" 2>/dev/null | grep -q '^token :: ' || ip token set :: dev "$1.1"   # the ::2 token of Q1 A
  # The leaf's default route must beat an IPv6 default on eth0: where the Kubernetes pod network is dual-stack (Kind),
  # eth0 carries "default via <pod gw> metric 1024", the RA's default also had 1024 and eth0 won => every GPU flow
  # died "No route" at the pod gateway (measured 2026-11-03, fresh Kind 26.8.2). Talos pods: IPv4 only, no clash.
  f="/proc/sys/net/ipv6/conf/$1.1/ra_defrtr_metric"
  [ ! -f "$f" ] || [ "$(cat "$f")" = "$HN_RA_METRIC" ] || echo "$HN_RA_METRIC" > "$f"
  ip -6 route flush dev "$1.1" proto boot 2>/dev/null      # static routes of the EDAManagedIPv6 era (fd00:100::/32)
  ip link set "$1.1" up
}

hn_v6_forget() {  # <dev>: global addresses + every route but link-local. WHY: the RA's on-link prefix route
  # (proto kernel, expires 30 days) survives an address flush AND a link bounce (measured 2026-11-03): after a
  # tenant move the old /64 would stay "on-link" and a GPU that later gets it would be unreachable.
  ip -6 addr flush dev "$1" scope global
  ip -6 route show dev "$1" | awk '$1 !~ /^fe80/ {print $1}' | while read -r r; do ip -6 route del "$r" dev "$1"; done
}

hn_backend() {
  hn_slaac "$1"
  if ip -6 -o addr show dev "$1.1" scope global | grep -v dynamic | grep -q .   ||           # static (pre-SLAAC)
     [ "$(ip -6 -o addr show dev "$1.1" scope global | wc -l)" -ne 1 ] ||                     # none, or old + new
     [ "$(ip -6 -o addr show dev "$1.1" scope global to "$2::/64" | wc -l)" -ne 1 ] ||        # not in EDA's /64
     [ "$(ip -6 route show dev "$1.1" | awk '$1 !~ /^fe80/ && $1 != "default" {print $1}')" != "$2::/64" ] ||
     ! ip -6 route show default dev "$1.1" | grep -q " metric $HN_RA_METRIC "; then             # RA default, old metric
    # A new booking or a tenant move (/64 from the other tenant's pool): forget the old addresses + routes, then
    # link up = router solicitation => the leaf answers at once (unsolicited RAs: every 200-600 s, plus one when the
    # port's prefix changes). 2.6-3.3 s incl. DAD (measured 2026-11-03 on gpu-4-g1). Unchanged booking = nothing
    # touched (hitless re-run).
    hn_v6_forget "$1.1"
    ip link set "$1.1" down; ip link set "$1.1" up
  fi
  a=""; i=0
  while [ $i -lt 100 ]; do                                  # up to 20 s
    a=$(ip -6 -o addr show dev "$1.1" scope global -tentative | awk '{print $4}' | head -1)
    [ -n "$a" ] && break
    i=$((i + 1)); sleep 0.2
  done
  if [ -z "$a" ]; then
    echo "    backend  $1.1  FAILED: no router advertisement from the leaf in 20 s (EDA allocated $2::/64)"; return 1
  fi
  gw=$(ip -6 route show default dev "$1.1" | awk '{print $3; exit}')
  ip -6 -o addr show dev "$1.1" scope global to "$2::/64" | grep -q . && chk="in the /64 EDA allocated" \
    || chk="!! EDA allocated $2::/64"
  echo "    backend  $1.1  $a  gw $gw  (VLAN 1, SLAAC, host part from the MAC; $chk)"
  hn_v4_example "$1" "${3:-}" "${4:-}"
}

hn_v4_example() {  # <parent> [<v4/31> <gw>]: IPv4 on VLAN 1 only where an example gpu-ipv4 Configlet sits; else none
  for a in $(ip -4 -o addr show dev "$1.1" | awk '{print $4}'); do
    [ "$a" = "${2:-}" ] || ip -4 addr del "$a" dev "$1.1"
  done
  if [ -n "${2:-}" ]; then
    ip -4 addr replace "$2" dev "$1.1"
    ip route replace 10.201.0.0/16 via "$3" dev "$1.1"
    echo "    backend  $1.1  $2  gw $3  (VLAN 1 IPv4, gpu-ipv4 Configlet)"
  else
    ip route del 10.201.0.0/16 dev "$1.1" 2>/dev/null
  fi
}

hn_backend_park() {
  hn_slaac "$1"                                             # armed: a later booking's RA addresses the NIC by itself
  hn_v6_forget "$1.1"                                       # SLAAC addresses + routes would otherwise live on 30 days
  ip route del 10.201.0.0/16 dev "$1.1" 2>/dev/null
  ip -4 addr flush dev "$1.1"
  echo "    backend  $1.1  parked (unbooked GPU: no tenant, the Backend renders nothing on its port, no RA)"
}

hn_frontend() {  # <dev> <parent> <cidr> <gw>
  dev=$1 parent=$2 want=$3 gw=$4
  sysctl -qw net.ipv4.conf.all.arp_ignore=1 net.ipv4.conf.all.arp_announce=2
  ip link show "$dev" >/dev/null 2>&1 || ip link add link "$parent" name "$dev" type vlan id 2
  ip link set "$dev" up
  cur=$(ip -4 -o addr show dev "$dev" | awk '{print $4}')
  [ "$cur" = "$want" ] || { ip -4 addr flush dev "$dev"; ip -4 addr add "$want" dev "$dev"; }
  ip route replace 10.20.0.0/14 via "$gw" dev "$dev"
  arping -q -U -c 1 -I "$dev" "${want%/*}" >/dev/null 2>&1 || true
  echo "    frontend $dev  $want  gw $gw  (VLAN 2)"
  hn_qos "$dev"
}

hn_qos() {  # <dev>: the host's QoS marking on its VLAN 2 device (address-independent: matches the device only)
  dev=$1
  command -v nft >/dev/null 2>&1 || apk add --no-cache nftables >/dev/null 2>&1
  nft -f - <<NFT || { echo "    frontend QoS marking FAILED on $dev"; return 1; }
table inet aifabric_qos
delete table inet aifabric_qos
table inet aifabric_qos {
  set control { type inet_service; elements = { $QOS_CONTROL_PORTS } }
  chain pod_reset {
    type filter hook forward priority mangle; policy accept;
    oifname "$dev" ip dscp set cs0
  }
  chain classes {
    type filter hook postrouting priority mangle; policy accept;
    oifname != "$dev" accept
    meta l4proto { tcp, udp } th dport @control ip dscp set cs6 accept
    meta l4proto { tcp, udp } th sport @control ip dscp set cs6 accept
    ip daddr $QOS_STORAGE ip dscp set af11 accept
    ip saddr $QOS_STORAGE ip dscp set af11 accept
  }
}
NFT
  echo "    frontend QoS marking $dev: CS6 tcp/udp {$QOS_CONTROL_PORTS}, AF11 to/from $QOS_STORAGE, pod-forwarded traffic reset to 0"
}

hn_frontend_park() {
  if ip link show "$1" >/dev/null 2>&1; then
    ip route del 10.20.0.0/14 dev "$1" 2>/dev/null
    ip -4 addr flush dev "$1"
  fi
  nft delete table inet aifabric_qos 2>/dev/null
  echo "    frontend $1  parked (no VLAN 2 address)"
}
