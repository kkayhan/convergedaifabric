#!/usr/bin/env python3
"""aifabric traffic matrices: GPU backend (VLAN 1) + frontend (VLAN 2), live, scored against EDA's intent.

PER-GPU TENANCY (owner 2026-10-26): tenants book GPUs, not servers; every GPU is its own VM gpu-<s>-g<G>
with ONE NIC (rail G+1); NVLink is off, so two GPUs of the same server talk over their NICs and the fabric
(different rails = leaf -> spine -> leaf). Intent = the `tenant` label of the NIC's EDA Interface; no label
= free (unbooked: the Backend parks the port, the VM has no address).

BACKEND: every booked GPU pings every other booked GPU from its own NIC address (same server included).
Enforcement = the aifabrics Backend (one VRF per tenant).
FRONTEND (only when manifests/frontend.yaml is deployed): the booked GPU VMs (node IP on their own NIC's
VLAN 2) and the shared services hosts (management cluster, CES, Axelera, the shared CPU node cpu-1). Expected:
same tenant reachable, services <-> anyone reachable, tenant <-> other tenant blocked.

  ● green  should reach, reachable         correct
  · grey   should be blocked, blocked      correct isolation
  ✗ red    should reach, NOT reachable     broken
  ▲ red    should be blocked, reachable    isolation breach
  ? yellow warming up
  - dim    no address on one side (free GPU = parked, or being provisioned)

Labels are re-read every 3 s and addresses every 10 s, so a booking (orchestrator.py book/release)
regroups both matrices: cells of the GPUs involved pass through ✗/▲/- briefly and settle.

  python3 aifabric-traffic.py                   live (both matrices when the frontend exists)
  python3 aifabric-traffic.py --view backend    backend only (also: frontend)
  python3 aifabric-traffic.py --once            settle, print once, exit 0 = no violation
Env: KUBECTL (default: the cluster deploy.sh saved, .state/kubectl).
"""
import argparse, json, os, re, signal, subprocess, sys, threading, time
from io import StringIO

_SAVED = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".state", "kubectl")
KUBECTL = os.environ.get("KUBECTL") or (_SAVED if os.access(_SAVED, os.X_OK) else "kubectl")
NS = "aifabric"
GPUS = [f"gpu-{s}-g{g}" for s in range(1, 5) for g in range(4)]
ISLAND = ["ocp-m1", "ocp-m2", "ocp-m3", "ocp-s1", "ocp-s2", "ocp-s3", "ces-1", "axelera-1", "cpu-1"]
HOSTS = GPUS + ISLAND
SERVICES_ROLES = {"ocp-master", "ocp-services", "storage", "accelerator", "cpu"}   # cpu-1 shared (owner Q1 B 2026-10-27)

C_RESET = "\x1b[0m"; C_BOLD = "\x1b[1m"; C_DIM = "\x1b[2m"
C_GREEN = "\x1b[32m"; C_RED = "\x1b[31m"; C_GREY = "\x1b[90m"; C_YEL = "\x1b[33m"; C_CYAN = "\x1b[36m"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SYM = {"ok": C_GREEN + "●", "iso": C_GREY + "·", "down": C_RED + "✗", "leak": C_RED + "▲", "warm": C_YEL + "?",
       "na": C_DIM + "-"}


def sh(args, timeout=20):
    try:
        # own process group: kubectl's SIGURG (Go preemption) bursts must not reach this process
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, start_new_session=True)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def sim_exec(host, script, timeout=20):
    return sh([KUBECTL, "-n", "eda-system", "exec", f"deploy/cx-{NS}--{host}-sim", "-c", host, "--",
               "sh", "-c", script], timeout)


def discover():
    """host -> {'v6': backend NIC address (GPU VMs), 'v4': frontend VLAN 2 address}."""
    addrs = {}
    script = ("for d in eth1.1; do ip -6 -o addr show dev $d scope global 2>/dev/null; done | awk '{print \"v6 \" $4}'; "
              "for d in eth1.2 bond0.2; do ip -4 -o addr show dev $d 2>/dev/null; done | awk '{print \"v4 \" $4}'")

    def one(h):
        rc, out, _ = sim_exec(h, script)
        if rc != 0:
            return
        a = {}
        for line in out.splitlines():
            k, v = line.split()[:2]
            a.setdefault(k, v.split("/")[0])
        addrs[h] = a
    ts = [threading.Thread(target=one, args=(h,)) for h in HOSTS]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return addrs


def frontend_deployed():
    rc, out, _ = sh([KUBECTL, "-n", NS, "get", "virtualnetworks.services.eda.nokia.com",
                     "fe-services", "fe-tenant-1", "fe-tenant-2", "--ignore-not-found", "--no-headers"])
    return rc == 0 and bool(out.strip())


def read_intent():
    """Live EDA Interface labels => {host: '1' | '2' | 'free' | 'svc'} (GPU VM = its NIC's Interface)."""
    rc, out, _ = sh([KUBECTL, "-n", NS, "get", "interfaces.interfaces.eda.nokia.com", "-l", "role in (gpu,server)",
                     "-o", "json"])
    if rc != 0:
        return None
    m = {}
    for i in json.loads(out).get("items", []):
        lb = i["metadata"].get("labels", {})
        if lb.get("role") == "gpu" and lb.get("gpu-server") and lb.get("rail", "").isdigit():
            m[f"{lb['gpu-server']}-g{int(lb['rail']) - 1}"] = lb.get("tenant") or "free"
        elif lb.get("role") == "server" and lb.get("server"):
            m[lb["server"]] = "svc" if lb.get("server-role") in SERVICES_ROLES else (lb.get("tenant") or "free")
    return m


class Flow:
    __slots__ = ("ok", "samples")

    def __init__(self):
        self.ok = False
        self.samples = 0


def main():
    try:
        signal.signal(signal.SIGURG, signal.SIG_IGN)
    except (ValueError, AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--view", choices=["both", "backend", "frontend"], default="both",
                    help="matrices to show (both = frontend only if deployed)")
    ap.add_argument("--count", type=int, default=2, help="pings per flow per cycle")
    ap.add_argument("--interval", type=float, default=0.2, help="seconds between pings of one flow (-i)")
    ap.add_argument("--wait", type=float, default=1.0, help="seconds before a ping counts as lost (-W)")
    ap.add_argument("--gap", type=float, default=0.5, help="idle seconds between cycles")
    ap.add_argument("--refresh", type=float, default=1.0, help="screen redraw interval")
    ap.add_argument("--once", action="store_true", help="wait until every flow has 2 samples, print, exit")
    a = ap.parse_args()
    show_be = a.view in ("both", "backend")
    show_fe = a.view == "frontend" or (a.view == "both" and frontend_deployed())

    state = {"intent": read_intent() or {}, "addr": discover()}
    lock = threading.Lock()
    flows = {}                                     # (family, src, dst) -> Flow
    stop = threading.Event()

    def specs(h):
        """this cycle's flows from h: [(key, family, src, dst)] over the current addresses."""
        ad, out = state["addr"], []
        if show_be and h in GPUS and ad.get(h, {}).get("v6"):
            out += [(f"6:{d}", "-6", ad[h]["v6"], ad[d]["v6"]) for d in GPUS if d != h and ad.get(d, {}).get("v6")]
        if show_fe and ad.get(h, {}).get("v4"):
            out += [(f"4:{d}", "-4", ad[h]["v4"], ad[d]["v4"]) for d in HOSTS if d != h and ad.get(d, {}).get("v4")]
        return out

    def worker(h, jitter):
        """One exec per cycle per host: all its flows ping in parallel."""
        time.sleep(jitter)
        while not stop.is_set():
            sp = specs(h)
            if not sp:
                if stop.wait(2):
                    break
                continue
            script = ("for x in %s; do k=${x%%%%=*}; y=${x#*=}; f=${y%%%%=*}; y=${y#*=}; s=${y%%%%=*}; d=${y#*=}; "
                      "( if ping $f -n -q -c%d -i%s -W%s -I $s $d >/dev/null 2>&1; then echo \"$k 1\"; else echo \"$k 0\"; fi ) & "
                      "done; wait" % (" ".join(f"{k}={f}={s}={d}" for k, f, s, d in sp), a.count, a.interval, a.wait))
            rc, out, _ = sim_exec(h, script, timeout=int(a.count * a.interval + a.wait) + 15)
            for line in out.splitlines():   # an exec failure (API hiccup) prints nothing => no flow painted red
                parts = line.split()
                if len(parts) == 2 and ":" in parts[0]:
                    fam, d = parts[0].split(":", 1)
                    with lock:
                        f = flows.setdefault((fam, h, d), Flow())
                    f.ok = parts[1] == "1"
                    f.samples += 1
            if stop.wait(a.gap):
                break

    for n, h in enumerate(HOSTS):
        threading.Thread(target=worker, args=(h, n * 0.1), daemon=True).start()

    def intent_loop():
        k = 0
        while not stop.wait(3):
            m = read_intent()
            if m:
                state["intent"] = m
            k += 1
            if k % 3 == 0:                             # addresses change on book / release
                state["addr"] = discover()
    threading.Thread(target=intent_loop, daemon=True).start()

    def group(h):
        return state["intent"].get(h, "?")

    def has(h, fam):
        return bool(state["addr"].get(h, {}).get("v6" if fam == "6" else "v4"))

    def should(fam, x, y):
        gx, gy = group(x), group(y)
        if "free" in (gx, gy):
            return False
        return gx == gy or (fam == "4" and "svc" in (gx, gy))

    def cls(fam, x, y):
        if not (has(x, fam) and has(y, fam)):
            return "na"
        f = flows.get((fam, x, y))
        if f is None or f.samples < 2:
            return "warm"
        return {(True, True): "ok", (True, False): "down", (False, False): "iso", (False, True): "leak"}[(should(fam, x, y), f.ok)]

    def rank(g):
        return {"svc": (0, ""), "free": (9, "")}.get(g, (1, g))

    def gname(g):
        return {"svc": "services", "free": "free"}.get(g, f"tenant-{g}")

    def gshort(g):
        return {"svc": "svc", "free": "free"}.get(g, f"t{g}")

    def spans(order, key, width, short=None):
        out, i = "", 0
        while i < len(order):
            k = key(order[i]); j = i
            while j < len(order) and key(order[j]) == k:
                j += 1
            w = width * (j - i)
            label = str(k) if short is None or len(str(k)) < w else short(order[i])
            out += label.center(w)[:w]
            i = j
        return out

    def summary(buf, classes, ok_label):
        cnt = {k: 0 for k in SYM}
        for c in classes:
            cnt[c] += 1
        viol = cnt["down"] + cnt["leak"]
        vc = C_GREEN if viol == 0 else C_RED
        buf.write(f"  {C_GREEN}{ok_label} {cnt['ok']:>3}{C_RESET}   {C_GREY}isolated {cnt['iso']:>3}{C_RESET}   "
                  f"{vc}violations {viol:>3}{C_RESET}")
        if cnt["down"]:
            buf.write(f"   {C_RED}✗ {cnt['down']} should reach, down{C_RESET}")
        if cnt["leak"]:
            buf.write(f"   {C_RED}▲ {cnt['leak']} isolation breach{C_RESET}")
        if cnt["warm"]:
            buf.write(f"   {C_YEL}{cnt['warm']} warming{C_RESET}")
        buf.write("\n")
        return viol, cnt["warm"]

    def pairs(fam, hosts):
        return [(x, y) for x in hosts for y in hosts if x != y and has(x, fam) and has(y, fam)]

    def render_backend(buf):
        order = sorted(GPUS, key=lambda h: (rank(group(h)), GPUS.index(h)))
        buf.write(f"{C_BOLD}GPU backend — per-GPU tenancy{C_RESET}   16 GPUs = 4 servers × 4 (one NIC each, rail = GPU+1), "
                  f"VLAN 1, IPv6, NVLink off: same-server GPUs talk over the fabric · intent = EDA Interface label 'tenant'\n")
        lw = 16
        buf.write(C_CYAN + " " * lw + spans(order, lambda h: gname(group(h)), 2, lambda h: gshort(group(h))) + C_RESET + "\n")
        buf.write(C_BOLD + " " * lw + "".join(f"{h[4]:>2}" for h in order) + C_RESET + C_DIM + "  server" + C_RESET + "\n")
        buf.write(C_DIM + " " * lw + "".join(f"{h[-1]:>2}" for h in order) + "  GPU (rail = GPU+1)" + C_RESET + "\n")
        for x in order:
            row = f"{gshort(group(x)):>4} {x}".ljust(lw - 1) + " "
            for y in order:
                row += "  " if x == y else " " + SYM[cls("6", x, y)] + C_RESET
            buf.write(row + "\n")
        res = summary(buf, [cls("6", x, y) for x, y in pairs("6", GPUS)], "same-tenant ok")
        members = {}
        for h in GPUS:
            members.setdefault(gname(group(h)), {}).setdefault(h[:5], []).append(h[-1])
        buf.write("  " + "   ".join(f"{g}: " + ", ".join(f"{s} g{''.join(gs)}" for s, gs in sv.items())
                                 for g, sv in sorted(members.items(), key=lambda kv: (kv[0] == "free", kv[0]))) + "\n")
        return res

    def render_frontend(buf):
        hosts = [h for h in HOSTS if has(h, "4") or h in ISLAND]
        order = sorted(hosts, key=lambda h: (rank(group(h)), HOSTS.index(h)))
        buf.write(f"{C_BOLD}Frontend — VLAN 2{C_RESET}   {len(order)} hosts (free GPUs have none), IPv4, services VRF shared, "
                  f"tenant VRFs isolated from each other\n")
        lw = 19
        buf.write(C_CYAN + " " * lw + spans(order, lambda h: gname(group(h)), 3, lambda h: gshort(group(h))) + C_RESET + "\n")
        buf.write(C_DIM + " " * lw + "".join(f"{i + 1:>3}" for i in range(len(order))) + C_RESET + "\n")
        for i, h in enumerate(order):
            row = f"{i + 1:>2} {gshort(group(h)):>4} {h}".ljust(lw - 1) + " "
            for d in order:
                row += "   " if d == h else "  " + SYM[cls("4", h, d)] + C_RESET
            buf.write(row + "\n")
        return summary(buf, [cls("4", x, y) for x, y in pairs("4", HOSTS)], "should-reach ok")

    def render(clear=True):
        buf = StringIO()
        if clear:
            buf.write("\x1b[H\x1b[2J\x1b[3J")
        buf.write(f"{C_BOLD}aifabric{C_RESET}  {C_DIM}{C_GREEN}●{C_DIM} reachable as intended  {C_GREY}·{C_DIM} "
                  f"isolated as intended  {C_RED}✗{C_DIM} should reach, DOWN  {C_RED}▲{C_DIM} isolation BREACH  "
                  f"{C_YEL}?{C_DIM} warming  - no address (free GPU){C_RESET}\n\n")
        viol = warm = 0
        if show_be:
            v, w = render_backend(buf); viol += v; warm += w
        if show_fe:
            if show_be:
                buf.write("\n")
            v, w = render_frontend(buf); viol += v; warm += w
        if clear:
            buf.write(f"{C_DIM}Ctrl-C to stop.{C_RESET}\n")
        out = buf.getvalue()
        sys.stdout.write(out if sys.stdout.isatty() or clear else ANSI.sub("", out))
        sys.stdout.flush()
        return viol, warm

    if a.once:
        t0 = time.monotonic()
        while time.monotonic() - t0 < 120:
            todo = ([cls("6", x, y) for x, y in pairs("6", GPUS)] if show_be else []) + \
                   ([cls("4", x, y) for x, y in pairs("4", HOSTS)] if show_fe else [])
            if "warm" not in todo:
                break
            time.sleep(1)
        viol, warm = render(clear=False)
        stop.set()
        return 0 if viol == 0 and warm == 0 else 1
    try:
        while True:
            render()
            time.sleep(a.refresh)
    except KeyboardInterrupt:
        stop.set()
        print("\nstopping...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
