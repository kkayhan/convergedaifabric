# aifabric: GPU host agent for the backend NIC (VLAN 1). POSIX sh, sourced on a GPU VM after host-net.sh.
# A GPU VM runs its backend network the way a real GPU host does: it never asks EDA anything, it only reacts to
# what its switch port advertises. So booking, moving or releasing a GPU = one label in EDA, nothing on the host.
# (owner R2-Q2 A 2026-11-18. The frontend VLAN 2 by DHCP failed live, see eda-lab § HOST SLAAC "DHCP RELAY ACROSS
# LOCAL ROUTE LEAKING" ⇒ decided fallback B: VLAN 2 stays a static address per tenant, set by hosts.sh.)
#
#   hn_arm <parent> <name>      once per VM (deploy phase hosts, or after a pod restart = power-on provisioning):
#                               hostname + LLDP, VLAN 1 SLAAC armed, the agent (re)started. Idempotent, hitless.
#   hn_agent <parent>           the loop (detached; log /var/log/aifabric-agent.log), every second:
#     booked    a SLAAC address appears (the Backend put the port in a tenant VRF, the leaf advertised the /64)
#               => ping the leaf ::1 once (its IPv6 neighbour table learns the NIC: proof for the tools)
#     moved     a second /64 appears (tenant move: the leaf advertises the other tenant's /64 and never withdraws
#               the old one) => forget both, solicit => only the new one comes back (= a re-provisioned host)
#     released  the leaf's gateway stops answering for 3 s (the Backend parked the port) => forget the address
#     parked    no address => router solicitation every 10 s (a booking is picked up even if its RA was missed)
#   hn_state <parent> [<prefix>]  print what VLAN 1 holds; with <prefix> (EDA's /64 for a booked port) wait up to
#                                 20 s for exactly one address inside it; with "-" wait for no address
HN_DIR=/etc/aifabric
HN_LOG=/var/log/aifabric-agent.log

hn_log() { echo "$(date +%T) $*" >> "$HN_LOG"; }

hn_v6_prefixes() { ip -6 route show dev "$1" 2>/dev/null | awk '$1 ~ /\/64$/ && $1 !~ /^fe80/ {print $1}'; }

hn_agent() {  # <parent>
  dev="$1.1" last="" fails=0 idle=0
  hn_log "agent started on $dev"
  while :; do
    n=$(ip -6 -o addr show dev "$dev" scope global -tentative 2>/dev/null | wc -l)
    np=$(hn_v6_prefixes "$dev" | wc -l)
    if [ "$n" -gt 1 ] || [ "$np" -gt 1 ]; then
      hn_log "moved: $np prefixes on $dev (tenant move) => forget them, solicit"
      hn_v6_forget "$dev"; ip link set "$dev" down; ip link set "$dev" up
      last="" fails=0; sleep 2; continue
    fi
    pfx=$(hn_v6_prefixes "$dev")
    if [ "$n" -eq 1 ] && [ -n "$pfx" ]; then
      idle=0
      gw=$(ip -6 route show default dev "$dev" 2>/dev/null | awk '{print $3; exit}')
      # booked = an address AND a leaf that answers as default router. When the Backend parks the port the leaf
      # withdraws as router (the default route goes, the address stays: measured 2026-11-18) or stops answering
      if [ -n "$gw" ] && ping -c 1 -W 1 -I "$dev" "$gw" >/dev/null 2>&1; then
        fails=0
        if [ "$pfx" != "$last" ]; then
          hn_log "booked: $(ip -6 -o addr show dev "$dev" scope global | awk '{print $4; exit}') (gateway $gw)"
          ping -c 2 -W 1 "${pfx%/64}1" >/dev/null 2>&1        # the leaf's ::1 learns the NIC (neighbour table)
          last=$pfx
        fi
      else
        fails=$((fails + 1))
        if [ "$fails" -ge 3 ]; then
          hn_log "released: ${gw:-no default router} on $dev (the leaf parked the port) => forget the address"
          hn_v6_forget "$dev"; last="" fails=0
        fi
      fi
    else
      [ -z "$last" ] || hn_log "address gone on $dev"
      last=""
      idle=$((idle + 1))
      if [ "$idle" -ge 10 ]; then ip link set "$dev" down; ip link set "$dev" up; idle=0; fi   # = router solicitation
    fi
    sleep 1
  done
}

hn_arm() {  # <parent> <name>
  hn_lldp "$2"
  hn_slaac "$1"
  if [ -f /run/aifabric-agent.pid ]; then kill "$(cat /run/aifabric-agent.pid)" 2>/dev/null; fi
  setsid sh -c ". $HN_DIR/host-net.sh; . $HN_DIR/host-agent.sh; hn_agent $1" >> "$HN_LOG" 2>&1 < /dev/null &
  echo $! > /run/aifabric-agent.pid
}

hn_state() {  # <parent> [<prefix>|-]
  dev="$1.1" i=0
  while [ -n "${2:-}" ] && [ $i -lt 100 ]; do
    if [ "$2" = - ]; then
      [ "$(ip -6 -o addr show dev "$dev" scope global 2>/dev/null | wc -l)" -eq 0 ] && break
    else
      [ "$(ip -6 -o addr show dev "$dev" scope global -tentative 2>/dev/null | wc -l)" -eq 1 ] &&
        ip -6 -o addr show dev "$dev" scope global -tentative to "$2::/64" | grep -q . && break
    fi
    i=$((i + 1)); sleep 0.2
  done
  a=$(ip -6 -o addr show dev "$dev" scope global -tentative 2>/dev/null | awk '{print $4}' | tr '\n' ' ')
  gw=$(ip -6 route show default dev "$dev" 2>/dev/null | awk '{print $3; exit}')
  if [ "${2:-}" = - ] && [ -n "$a" ]; then
    echo "    backend  $dev  FAILED: still holds ${a% } after 20 s, the port is parked"; return 1
  elif [ -z "${2:-}" ] || [ "$2" = - ]; then
    echo "    backend  $dev  ${a:-parked (no address)}  (host agent)"
  elif ip -6 -o addr show dev "$dev" scope global -tentative to "$2::/64" | grep -q .; then
    echo "    backend  $dev  ${a% }  gw $gw  (VLAN 1, SLAAC by the host agent; in the /64 EDA allocated)"
  else
    echo "    backend  $dev  FAILED: ${a:-no address} after 20 s, EDA allocated $2::/64"; return 1
  fi
}
