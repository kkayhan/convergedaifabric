#!/usr/bin/env python3
"""Which egress queue a marked packet uses on every hop, both directions (switch queue counters).

  qos-path.py frontend <dscp> [count]   pings gpu-1-g0 10.21.0.10 -> cpu-1 10.20.0.51 (VLAN 2, EVPN-VXLAN, tenant-1 -> services)
  qos-path.py rdma <dscp> [count]       pings gpu-1-g0 (rail 1) -> gpu-2-g1 (rail 2) (VLAN 1, across the spines)
  qos-path.py rdma4 <dscp> [count]      same path over IPv4 10.201.1.1 -> 10.201.2.3 (gpu-ipv4 Configlets)
  qos-path.py idle [seconds]            no test traffic: what the cluster itself sends (default 60 s)

Hops: fwd1-3 = gpu-1-g0 -> far end, rev1-3 = way back (the echo reply carries the request's DSCP). Each
row = transmitted-packet delta per output queue, summed over a hop's ports (14 uplinks, both spines,
both non-GPU leaves); empty queues hidden. Queue => class (manifests/qos.yaml): unicast-6 control
(CS6 48), unicast-3 RDMA lossless (AF31 26, VLAN 1), unicast-0 serving/default, unicast-1 bulk
(AF11 10); any other queue is flagged. The other traffic on those ports (BGP on the uplinks,
Kubernetes control traffic) adds a few hundred packets per run. The simulator counts packets per queue
exactly but does not model congestion, so scheduler shares are not visible here.
"""
import json, subprocess, sys, time
import concurrent.futures as cf
from pathlib import Path

SSH = str(Path(__file__).resolve().parent / "aifabric-ssh")
Q = "info from state qos interfaces interface * output queues queue * queue-statistics aggregate-statistics transmitted-packets | as json"
CLASS = {"unicast-0": "serving", "unicast-1": "bulk", "unicast-3": "RDMA", "unicast-6": "control"}


def port(i):                                  # "ethernet-1/52" -> 52, "ethernet-1/1/1" -> 1
    p = i.split("/")
    return int(p[1]) if len(p) > 1 and p[1].isdigit() else 0


def uplink(i):
    return 51 <= port(i) <= 64 and i.count("/") == 1


FRONTEND = {  # gpu-1-g0 (l1-1 e1-1/1/1) <-> cpu-1 (ESI-LAG l1-5/l1-6 e1-12/3)
    "l1-1": {"fwd1 l1-1 uplinks": uplink, "rev3 l1-1 -> gpu-1-g0": lambda i: i == "ethernet-1/1/1"},
    "l2-1": {"fwd2 spines -> l1-5/6": lambda i: 29 <= port(i) <= 42, "rev2 spines -> l1-1": lambda i: 1 <= port(i) <= 7},
    "l2-2": {"fwd2 spines -> l1-5/6": lambda i: 29 <= port(i) <= 42, "rev2 spines -> l1-1": lambda i: 1 <= port(i) <= 7},
    "l1-5": {"fwd3 l1-5/6 -> cpu-1": lambda i: i == "ethernet-1/12/3", "rev1 l1-5/6 uplinks": uplink},
    "l1-6": {"fwd3 l1-5/6 -> cpu-1": lambda i: i == "ethernet-1/12/3", "rev1 l1-5/6 uplinks": uplink},
}
RDMA = {  # gpu-1-g0 rail 1 (l1-1 e1-1/1/1) <-> gpu-2-g1 rail 2 (l1-2 e1-1/1/2), routed IPv6 over the spines
    "l1-1": {"fwd1 l1-1 uplinks": uplink, "rev3 l1-1 -> gpu-1-g0": lambda i: i == "ethernet-1/1/1"},
    "l2-1": {"fwd2 spines -> l1-2": lambda i: 8 <= port(i) <= 14, "rev2 spines -> l1-1": lambda i: 1 <= port(i) <= 7},
    "l2-2": {"fwd2 spines -> l1-2": lambda i: 8 <= port(i) <= 14, "rev2 spines -> l1-1": lambda i: 1 <= port(i) <= 7},
    "l1-2": {"fwd3 l1-2 -> gpu-2-g1": lambda i: i == "ethernet-1/1/2", "rev1 l1-2 uplinks": uplink},
}


def backend_v6(vm):                          # the GPU NIC's SLAAC address (the port's /64 + MAC part), read on the host
    out = subprocess.run([SSH, "-c", "ip -6 -o addr show dev eth1.1 scope global", vm], capture_output=True, text=True).stdout
    for w in out.split():
        if "/" in w and ":" in w:
            return w.split("/")[0]
    sys.exit(f"{vm}: no IPv6 address on eth1.1 (not booked?)")


def snap(groups, node):
    out = subprocess.run([SSH, "-c", Q, node], capture_output=True, text=True).stdout
    d = json.loads(out[out.index("{"):])
    res = {}
    for itf in d["interface"]:
        for g, pred in groups[node].items():
            if pred(itf["interface-id"]):
                for q in itf.get("output", {}).get("queues", {}).get("queue", []):
                    if q["queue-name"].startswith("unicast-"):
                        k = (g, q["queue-name"])
                        res[k] = res.get(k, 0) + int(q["queue-statistics"]["aggregate-statistics"]["transmitted-packets"])
    return node, res


def snapall(groups):
    with cf.ThreadPoolExecutor(len(groups)) as ex:
        return dict(ex.map(lambda n: snap(groups, n), groups))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("frontend", "rdma", "rdma4", "idle"):
        sys.exit(__doc__)
    mode = sys.argv[1]
    if mode == "idle":
        groups, secs = FRONTEND, int(sys.argv[2]) if len(sys.argv) > 2 else 60
        cmd, title = f"sleep {secs}", f"no test traffic, {secs} s"
    else:
        if len(sys.argv) < 3:
            sys.exit(__doc__)
        dscp, count = int(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 3000
        groups = FRONTEND if mode == "frontend" else RDMA
        src, dst = {"frontend": lambda: ("10.21.0.10", "10.20.0.51"),
                    "rdma": lambda: (backend_v6("gpu-1-g0"), backend_v6("gpu-2-g1")),
                    "rdma4": lambda: ("10.201.1.1", "10.201.2.3")}[mode]()
        cmd = f"ping -q -c {count} -i 0.002 -Q {dscp << 2:#04x} -I {src} {dst} | tail -2"
        title = f"{mode}: DSCP {dscp}, {count} echo requests {src} -> {dst}"
    a = snapall(groups)
    r = subprocess.run([SSH, "-c", cmd, "gpu-1-g0"], capture_output=True, text=True)
    if r.stdout.strip():
        print(r.stdout.strip())
    time.sleep(12)                            # queue counters update with a delay
    b = snapall(groups)
    tot = {}
    for n in groups:
        for k in set(a[n]) | set(b[n]):
            tot[k] = tot.get(k, 0) + b[n].get(k, 0) - a[n].get(k, 0)
    print(f"{title} — transmitted packets per hop and queue:")
    for g in sorted({g for g, _ in tot}):
        qs = sorted((q for gg, q in tot if gg == g and tot[(gg, q)]), key=lambda q: int(q.split("-")[1]))
        print(f"  {g:22s} " + "  ".join(f"{q} {CLASS.get(q, '!! unclassified')} {tot[(g, q)]}" for q in qs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
