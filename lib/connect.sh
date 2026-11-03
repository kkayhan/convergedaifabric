# aifabric — which EDA and which Kubernetes cluster the scripts talk to. Sourced (BEFORE lib/common.sh) by
# deploy.sh, destroy.sh, hosts.sh, k8s.sh.
#
# Two doors into the same EDA, and both must point at the SAME one:
#   EDA API  (URL + admin login)  creates the orchestrator's EDA user, books GPUs, reads the GPU addresses
#   kubectl  (kubeconfig)         everything else: the lab objects, edactl inside EDA's toolbox pod, the network
#                                 inside the simulated hosts, the dashboard's Prometheus + Grafana
#
# interactive (default)  asks for the EDA URL or IP, the EDA admin user + password, the kubeconfig file and its
#                        context. Any answer can come from env instead: EDA_URL, EDA_ADMIN_USER, EDA_ADMIN_PASSWORD,
#                        AIF_KUBECONFIG, AIF_CONTEXT (+ --yes = no confirmation) => unattended runs.
# pinned                 the caller set KUBECTL, or EDA_CLUSTER (the build host's demo.py dispatcher, which also sets
#                        KUBECONFIG + EDA_URL): no questions; admin login from env, else admin / admin.
# Keycloak (EDA's login server) master admin = admin / admin, the EDA install default (KC_MASTER_USER /
# KC_MASTER_PASSWORD override it): only it can read the secret of EDA's login client, which the API login needs.
#
# The answers, never the password, are saved in .state/connection.env, and .state/kubectl = kubectl pinned to that
# kubeconfig + context, so the demo tools (orchestrator.py, aifabric-traffic.py, aifabric-ssh, hosts.sh, ...) reach
# the same EDA later without asking.

AIF_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
AIF_STATE="$AIF_ROOT/.state"
AIF_CALLER_KUBECTL="${KUBECTL:-}"          # captured before lib/common.sh defaults KUBECTL to plain kubectl
AIF_MODE=interactive
AIF_NEED_CPU=14 AIF_NEED_GIB=30             # pod requests of the lab (13.2 CPU / 28.3 GiB measured 2026-11-03) + dashboard

aif_saved(){  # aif_saved KEY: value saved by an earlier run ("" when none)
  [ -f "$AIF_STATE/connection.env" ] || return 0
  ( . "$AIF_STATE/connection.env" 2>/dev/null; printf '%s' "${!1:-}" )
}

aif_resolve_kubectl(){  # scripts that never ask (hosts.sh, k8s.sh): KUBECTL from env, else the saved wrapper, else kubectl
  if [ -z "${KUBECTL:-}" ]; then
    if [ -x "$AIF_STATE/kubectl" ]; then KUBECTL="$AIF_STATE/kubectl"; else KUBECTL=kubectl; fi
  fi
  export KUBECTL
}

aif_norm_url(){  # "10.1.1.1" | "eda.lab:9443" | "https://eda.lab/core/..." -> https://host[:port]
  local u="$1"
  case "$u" in http://*|https://*) ;; *) u="https://$u" ;; esac
  sed -E 's#^(https?://[^/]+).*#\1#' <<<"$u"
}

aif_ask(){  # aif_ask VAR "question" [default] [secret]: read from the terminal, Enter = default
  local __a
  if [ ! -t 0 ]; then                       # unattended: take the default; a question without one needs the env
    if [ -n "${3:-}" ] || [ "${4:-}" = secret ]; then printf -v "$1" '%s' "${3:-}"; return 0; fi
    err "no terminal to ask '$2': set it through the environment (see README)"; return 1
  fi
  if [ "${4:-}" = secret ]; then
    read -rsp "$2: " __a; echo >&2
  else
    read -rp "$2${3:+ [$3]}: " __a
  fi
  printf -v "$1" '%s' "${__a:-${3:-}}"
}

aif_write_state(){  # aif_write_state <kubectl binary> <kubeconfig|""> <context|"">: wrapper + connection.env
  local bin="$1" kc="$2" ctx="$3"
  mkdir -p "$AIF_STATE"
  {
    echo '#!/usr/bin/env bash'
    echo "# written by deploy.sh/destroy.sh: kubectl pinned to the cluster of the EDA at $EDA_URL"
    printf 'exec %q' "$bin"
    [ -n "$kc" ] && printf ' --kubeconfig=%q' "$kc"
    [ -n "$ctx" ] && printf ' --context=%q' "$ctx"
    echo ' "$@"'
  } > "$AIF_STATE/kubectl.new" && chmod 755 "$AIF_STATE/kubectl.new" && mv "$AIF_STATE/kubectl.new" "$AIF_STATE/kubectl"
  {
    echo "# aifabric connection (written $(date -u +%FT%TZ)); no password is stored"
    printf 'EDA_URL=%q\nEDA_ADMIN_USER=%q\nAIF_KUBECONFIG=%q\nAIF_CONTEXT=%q\n' "$EDA_URL" "$EDA_ADMIN_USER" "$kc" "$ctx"
  } > "$AIF_STATE/connection.env"
}

aif_connect_pinned(){
  local kw bin kc="" ctx=""
  AIF_MODE=pinned
  KUBECTL="${AIF_CALLER_KUBECTL:-kubectl}"
  [ -n "${EDA_URL:-}" ] || { err "EDA_URL is not set (pinned mode: KUBECTL or EDA_CLUSTER given)"; return 1; }
  EDA_URL=$(aif_norm_url "$EDA_URL")
  EDA_ADMIN_USER="${EDA_ADMIN_USER:-admin}" EDA_ADMIN_PASSWORD="${EDA_ADMIN_PASSWORD:-admin}"
  # the saved wrapper must keep working outside this run: resolve the binary and pin KUBECONFIG + context
  read -ra kw <<<"$KUBECTL"
  bin=$(command -v "${kw[0]}") || { err "kubectl '${kw[0]}' not found"; return 1; }
  if [ "$(readlink -f "$bin")" = "$(readlink -f "$AIF_STATE/kubectl" 2>/dev/null)" ]; then
    return 0                                          # the caller passed the saved wrapper itself
  fi
  if [ "${#kw[@]}" -gt 1 ]; then                      # KUBECTL with its own flags: keep them verbatim
    mkdir -p "$AIF_STATE"
    { echo '#!/usr/bin/env bash'; printf 'exec'; printf ' %q' "$bin" "${kw[@]:1}"; echo ' "$@"'; } > "$AIF_STATE/kubectl"
    chmod 755 "$AIF_STATE/kubectl"
    printf 'EDA_URL=%q\nEDA_ADMIN_USER=%q\n' "$EDA_URL" "$EDA_ADMIN_USER" > "$AIF_STATE/connection.env"
    return 0
  fi
  [ -n "${KUBECONFIG:-}" ] && kc=$(readlink -f "${KUBECONFIG%%:*}")
  ctx=$("$bin" config current-context 2>/dev/null)
  aif_write_state "$bin" "$kc" "$ctx"
}

aif_connect_interactive(){
  local kc ctx n i list cur def kb
  echo "aifabric — GPU backend demo for Nokia EDA. Answers in [brackets] are the default (Enter)."
  if [ -z "${EDA_URL:-}" ]; then aif_ask EDA_URL "EDA URL or IP address (the address of the EDA web UI)" "$(aif_saved EDA_URL)" || return 1; fi
  [ -n "$EDA_URL" ] || { err "no EDA address given"; return 1; }
  EDA_URL=$(aif_norm_url "$EDA_URL")
  if [ -z "${EDA_ADMIN_USER:-}" ]; then def=$(aif_saved EDA_ADMIN_USER); aif_ask EDA_ADMIN_USER "EDA admin user" "${def:-admin}" || return 1; fi
  EDA_ADMIN_USER="${EDA_ADMIN_USER:-admin}"
  if [ -z "${EDA_ADMIN_PASSWORD+x}" ]; then
    aif_ask EDA_ADMIN_PASSWORD "EDA password for $EDA_ADMIN_USER (hidden; Enter = admin)" "" secret || return 1
  fi
  EDA_ADMIN_PASSWORD="${EDA_ADMIN_PASSWORD:-admin}"
  kc="${AIF_KUBECONFIG:-}"
  if [ -z "$kc" ]; then
    def=$(aif_saved AIF_KUBECONFIG); def="${def:-${KUBECONFIG:-$HOME/.kube/config}}"; def="${def%%:*}"
    echo "kubectl access to the Kubernetes cluster EDA runs on is needed too: it builds the simulated hosts and the dashboard."
    aif_ask kc "kubeconfig file of that cluster" "$def" || return 1
  fi
  kc="${kc/#\~/$HOME}"
  [ -f "$kc" ] || { err "kubeconfig '$kc' not found"; return 1; }
  kc=$(readlink -f "$kc")
  kb=$(command -v kubectl) || { err "kubectl is not installed (https://kubernetes.io/docs/tasks/tools/)"; return 1; }
  ctx="${AIF_CONTEXT:-}"
  if [ -z "$ctx" ]; then
    mapfile -t list < <("$kb" --kubeconfig="$kc" config get-contexts -o name 2>/dev/null)
    [ "${#list[@]}" -gt 0 ] || { err "no context in $kc"; return 1; }
    cur=$("$kb" --kubeconfig="$kc" config current-context 2>/dev/null)
    if [ "${#list[@]}" -eq 1 ]; then
      ctx="${list[0]}"
    else
      def=$(aif_saved AIF_CONTEXT); [[ " ${list[*]} " == *" $def "* ]] || def="$cur"
      for i in "${!list[@]}"; do printf '  %2d) %s%s\n' $((i + 1)) "${list[$i]}" "$([ "${list[$i]}" = "$def" ] && echo '   (default)')"; done
      aif_ask n "context of the EDA cluster (number or name)" "$def" || return 1
      if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#list[@]}" ]; then ctx="${list[$((n - 1))]}"; else ctx="$n"; fi
      [[ " ${list[*]} " == *" $ctx "* ]] || { err "context '$ctx' is not in $kc"; return 1; }
    fi
  fi
  aif_write_state "$kb" "$kc" "$ctx"
  KUBECTL="$AIF_STATE/kubectl"
  AIF_CTX_SHOWN="$ctx ($kc)"
}

aif_preflight(){  # aif_preflight deploy|destroy: is everything there? (fails hard only on what would break the run)
  local verb="$1" rc=0 img ver dom ip host n cap c m
  log "preflight — EDA $EDA_URL, kubectl ${AIF_CTX_SHOWN:-$KUBECTL}"
  [ "${BASH_VERSINFO[0]}" -ge 4 ] || { err "bash 4 or newer needed (this is bash $BASH_VERSION; macOS: brew install bash)"; rc=1; }
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null || { err "python3 3.9 or newer needed"; rc=1; }
  img=$($KUBECTL -n eda-system get deploy eda-api -o jsonpath='{.spec.template.spec.containers[0].image}' 2>&1) \
    || { err "kubectl cannot read EDA's deployment eda-system/eda-api with this kubeconfig/context: $img"; return 1; }
  ver="${img##*:}"
  echo "  Kubernetes side: EDA $ver"
  # the two doors must open the same EDA: compare the URL with the address EDA itself is configured for
  dom=$($KUBECTL -n eda-system get engineconfig -o jsonpath='{.items[0].spec.cluster.external.domainName}' 2>/dev/null)
  ip=$($KUBECTL -n eda-system get engineconfig -o jsonpath='{.items[0].spec.cluster.external.ipv4Address}' 2>/dev/null)
  host="${EDA_URL#*://}"; host="${host%%[:/]*}"
  if [ -n "$dom$ip" ] && [ "$host" != "$dom" ] && [ "$host" != "$ip" ]; then
    warn "the EDA behind this kubeconfig calls itself '$dom'${ip:+ ($ip)}, the URL says '$host': make sure both are the same EDA"
  fi
  python3 "$AIF_ROOT/lib/eda_auth.py" check || rc=1
  if [ "$verb" = deploy ]; then
    [[ "$ver" == 26.8.* ]] || warn "built and tested on EDA 26.8.2 (aifabrics Backend with per-tenant IPv6 pools); this EDA runs $ver"
    $KUBECTL get crd backends.aifabrics.eda.nokia.com >/dev/null 2>&1 \
      && echo "  aifabrics app: installed" \
      || { err "the aifabrics app (Backend CRD) is not installed: install 'AI Fabrics' from the EDA Store first"; rc=1; }
    $KUBECTL -n eda get nodeprofile srlinux-ghcr-26.7.1 >/dev/null 2>&1 \
      && echo "  NodeProfile srlinux-ghcr-26.7.1 (SR Linux 26.7.1 for the simulated switches): present" \
      || { err "NodeProfile srlinux-ghcr-26.7.1 missing in EDA namespace 'eda' (it ships with EDA 26.8)"; rc=1; }
    if $KUBECTL get crd exports.prom.eda.nokia.com >/dev/null 2>&1; then
      echo "  Prometheus exporter app: installed"
    elif $KUBECTL -n eda-system get catalogs.appstore.eda.nokia.com eda-catalog-builtin-apps >/dev/null 2>&1; then
      echo "  Prometheus exporter app: not installed => installed by the deploy (catalog eda-catalog-builtin-apps)"
    else
      err "Prometheus exporter app missing and catalog eda-catalog-builtin-apps not found: install 'Prometheus Exporter' from the EDA Store"; rc=1
    fi
    cap=$($KUBECTL get nodes -o jsonpath='{range .items[*]}{.status.allocatable.cpu}{" "}{.status.allocatable.memory}{"\n"}{end}' 2>/dev/null \
      | python3 -c '
import sys
cpu = mem = 0.0
for line in sys.stdin:
    c, m = line.split()
    cpu += float(c[:-1]) / 1000 if c.endswith("m") else float(c)
    units = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "K": 1e3, "M": 1e6, "G": 1e9}
    u = next((k for k in units if m.endswith(k)), "")
    mem += float(m[:len(m) - len(u)]) * units.get(u, 1) / 2**30
print(f"{cpu:.0f} {mem:.0f}")' 2>/dev/null)
    if [ -n "$cap" ]; then
      read -r c m <<<"$cap"
      echo "  cluster capacity: $c CPU / $m GiB allocatable in total (the lab requests about $AIF_NEED_CPU CPU / $AIF_NEED_GIB GiB, on top of EDA itself)"
      [ "$c" -ge $((AIF_NEED_CPU * 2)) ] && [ "$m" -ge $((AIF_NEED_GIB * 2)) ] \
        || warn "the cluster looks small for EDA + this lab: simulator pods may stay Pending"
    fi
  fi
  n=$($KUBECTL -n aifabric get toponodes --no-headers 2>/dev/null | wc -l)
  if [ "$n" -gt 0 ]; then
    echo "  namespace aifabric: exists with $n switches$([ "$verb" = deploy ] && echo ' => the deploy updates it in place')"
  else
    echo "  namespace aifabric: $([ "$verb" = deploy ] && echo 'not there yet => built from scratch' || echo 'no lab found')"
  fi
  [ $rc -eq 0 ] || err "preflight failed: nothing was changed"
  return $rc
}

aif_confirm(){  # aif_confirm <what> [yes]
  if [ "$AIF_MODE" = pinned ] || [ -n "${2:-}" ]; then return 0; fi
  local a
  aif_ask a "$1 Continue? (yes/no)" "no" || return 1
  [[ "$a" =~ ^[yY] ]] || { echo "stopped: nothing was changed"; return 1; }
}

aif_connect(){  # aif_connect deploy|destroy [yes]: settle the connection, preflight, confirm
  if [ -n "$AIF_CALLER_KUBECTL" ] || [ -n "${EDA_CLUSTER:-}" ]; then
    aif_connect_pinned || return 1
  else
    aif_connect_interactive || return 1
  fi
  export KUBECTL EDA_URL EDA_ADMIN_USER EDA_ADMIN_PASSWORD
  aif_preflight "$1" || return 1
  if [ "$1" = deploy ]; then
    aif_confirm "This builds the aifabric demo in EDA namespace 'aifabric' on $EDA_URL (8 simulated switches, 25 simulated hosts, about 10 minutes)." "${2:-}"
  else
    aif_confirm "This DELETES EDA namespace 'aifabric' on $EDA_URL with everything in it, plus the demo's user, dashboard proxy and Exports." "${2:-}"
  fi
}
