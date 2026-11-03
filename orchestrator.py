#!/usr/bin/env python3
"""GPU tenant orchestrator for the aifabric demo: books GPUs for tenants through the EDA API.

Stands in for an external GPU / cloud orchestrator (owner decision Q7 2026-10-23: "API orchestrator
first"). It holds its own EDA identity, user `gpu-orchestrator`, whose only write right is on
Interface objects in namespace aifabric, and it changes the network only through EDA transactions.

PER-GPU TENANCY (owner 2026-10-26: "tenants will own/book gpu cores, not the gpu servers … the tenant
isolation will happen at the NIC level … nvlinks disabled"; decisions Q1 A / Q2 A free pool). Each GPU has
its own NIC = one rail port = one EDA Interface, and runs as its own VM `gpu-<s>-g<G>` (GPU G of server
gpu-<s>, rail G+1). The aifabrics Backend gives each tenant one gpuIsolationGroup (= one ip-vrf on every
rail leaf) selected by the Interface labels `role=gpu,tenant=N`; an Interface without `tenant` is in no
group, so the Backend parks the port (no subinterface, no address, no QoS: dry-run txn 880). VLAN 2
(manifests/frontend.yaml) keys on the same label. So:
  book    tenant-N GPU(s) = ONE transaction that sets `tenant=N` on their Interfaces (a GPU of the other
          tenant moves, a free one comes out of the pool)
  release GPU(s)          = ONE transaction that removes the label (back to the free pool, port parked)
Host side (default, skip with --fabric-only): hosts.sh for those VMs (address or park them).
When the Kubernetes layer is deployed (k8s.sh), the orchestrator runs INSIDE the management cluster
(k8s/gpu-orchestrator.py) and owns tenancy: its desired state is ConfigMap gpu-orchestrator/gpu-tenancy.
book/release then delegate: they edit that ConfigMap and follow the in-cluster orchestrator's log (a
direct relabel would be reverted by it). `--direct` forces the direct path, `--dry-run` = EDA dry-run only.

  orchestrator.py setup | remove              Role aifabric:gpu-orchestrator + group + user (idempotent) | delete them
  orchestrator.py show                        every GPU: server, rail, leaf port, tenant, backend VRF + address, frontend BD
  orchestrator.py book tenant-2 gpu-3-g2 …    book GPUs (gpu-<s>-g<G> or a whole server gpu-<s>)
  orchestrator.py release gpu-3-g2 …          back to the free pool
  orchestrator.py baseline                    back to the deploy-time booking (topology/gen-topology.py), one batch
          [--dry-run] [--fabric-only] [--direct]

Env: EDA_URL, KUBECTL; default = the connection deploy.sh saved in .state/ (lib/eda_auth.py).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))   # in-cluster (k8s/gpu-orchestrator.py) eda_auth.py sits next to this file
import eda_auth as eda  # noqa: E402  (http, eda_client_secret, user_token, Admin, edactl, kubectl_args, eda_url)

NS = "aifabric"
USER = PASSWORD = "gpu-orchestrator"
GROUP = "gpu-orchestrator"
ROLE = "gpu-orchestrator"
ROLE_FILE = HERE / "manifests" / "orchestrator-role.yaml"
IF_GVK = {"group": "interfaces.eda.nokia.com", "version": "v1", "kind": "Interface"}
EDA_URL = eda.eda_url(required=False)


class Api:
    """EDA REST API as one user (password grant through the confidential client `eda`)."""

    def __init__(self, user: str, password: str):
        # in-cluster (k8s/gpu-orchestrator.py) the client secret comes from a Secret; outside via the Keycloak admin
        secret = os.environ.get("EDA_CLIENT_SECRET") or eda.eda_client_secret(EDA_URL)
        st, tok = eda.user_token(EDA_URL, secret, user, password)
        if st != 200:
            sys.exit(f"login as {user} failed: HTTP {st} {tok}")
        self.user = user
        self.h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        st, b = eda.http(method, EDA_URL + path, self.h, None if body is None else json.dumps(body).encode())
        if st not in ok:
            raise RuntimeError(f"{method} {path} -> HTTP {st} {b[:400]}")
        return json.loads(b) if b.strip() else None

    def tenant_interfaces(self) -> dict[str, dict]:
        """GPU VM -> its Interface CR: gpu-<s>-g<G> = the rail port of GPU G of server gpu-<s> (labels role=gpu,
        gpu-server, rail=G+1). Sorted by name. (cpu-1 = shared services node since owner Q1 B 2026-10-27: not bookable.)"""
        out = {}
        for sel in ("role=gpu",):
            path = (f"/apps/interfaces.eda.nokia.com/v1/namespaces/{NS}/interfaces?"
                    + urllib.parse.urlencode({"labelSelector": sel}))
            # client-side filter too (the EDA REST list ignores a misspelt selector silently: label-selector != labelSelector)
            for i in (self.call("GET", path) or {}).get("items", []):
                lb = i["metadata"].get("labels", {})
                if lb.get("role") == "gpu" and lb.get("gpu-server") and lb.get("rail", "").isdigit():
                    out[f"{lb['gpu-server']}-g{int(lb['rail']) - 1}"] = i
        return dict(sorted(out.items()))

    def leaf_vrfs(self) -> dict[tuple[str, str], str]:
        """(leaf, subinterface) -> network-instance, for the GPU subinterfaces (index 1 = gpuVLAN 1)."""
        # `namespaces` is mandatory for a namespaced Role: without it EDA answers 403 CannotReadDbTable
        q = ".namespace.node.srl.network-instance.interface fields [name]"
        path = "/core/query/v1/eql?" + urllib.parse.urlencode({"query": q, "namespaces": NS})
        try:
            data = self.call("GET", path) or {}
        except RuntimeError:
            return {}
        # rows are flat: {".namespace.node.name": leaf, ".namespace.node.srl.network-instance.name": vrf, "name": subif}
        return {(r[".namespace.node.name"], r["name"]): r[".namespace.node.srl.network-instance.name"]
                for r in data.get("data") or []
                if r.get(".namespace.node.name") and r.get("name") and r.get(".namespace.node.srl.network-instance.name")}

    def gpu_prefixes(self) -> dict[tuple[str, str], str]:
        """(leaf, port) -> the /64 the Backend took from the tenant's pool for that GPU port (leaf ::1 stripped)."""
        q = (".namespace.node.srl.interface.subinterface.ipv6.address fields [ip-prefix] "
             "where (.namespace.node.srl.interface.subinterface.index = 1)")
        path = "/core/query/v1/eql?" + urllib.parse.urlencode({"query": q, "namespaces": NS})
        try:
            data = self.call("GET", path) or {}
        except RuntimeError:
            return {}
        return {(r[".namespace.node.name"], r[".namespace.node.srl.interface.name"]): r["ip-prefix"][:-len("::1/64")]
                for r in data.get("data") or []
                if r.get(".namespace.node.name") and r.get(".namespace.node.srl.interface.name")
                and r.get("ip-prefix", "").endswith("::1/64") and not r["ip-prefix"].startswith("fe80")}

    def gpu_addresses(self, prefixes: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
        """(leaf, port) -> the address the GPU NIC built by SLAAC, as the leaf's IPv6 neighbor table knows it
        (learned from traffic). Preference: <the port's /64> + EUI-64 of the neighbor's MAC (owner Q1 B 2026-11-03),
        else any address inside the port's /64 (an older ::2 entry can linger until it ages out)."""
        q = (".namespace.node.srl.interface.subinterface.ipv6.neighbor-discovery.neighbor fields [link-layer-address] "
             "where (.namespace.node.srl.interface.subinterface.index = 1)")
        path = "/core/query/v1/eql?" + urllib.parse.urlencode({"query": q, "namespaces": NS})
        try:
            data = self.call("GET", path) or {}
        except RuntimeError:
            return {}
        out: dict[tuple[str, str], str] = {}
        for r in data.get("data") or []:
            key = (r.get(".namespace.node.name"), r.get(".namespace.node.srl.interface.name"))
            if key not in prefixes or not r.get("ipv6-address") or not r.get("link-layer-address"):
                continue
            net = ipaddress.IPv6Network(prefixes[key] + "::/64")
            a = ipaddress.IPv6Address(r["ipv6-address"])
            if a not in net:
                continue
            m = bytes.fromhex(r["link-layer-address"].replace(":", ""))
            eui = int(net.network_address) | int.from_bytes(bytes([m[0] ^ 2]) + m[1:3] + b"\xff\xfe" + m[3:], "big")
            if int(a) == eui or key not in out:
                out[key] = str(a)
        return out

    def transaction(self, description: str, crs: list[dict], dry_run: bool) -> dict:
        r = self.call("POST", "/core/transaction/v2",
                      {"description": description, "dryRun": dry_run, "crs": crs})
        tid = r["id"]
        for _ in range(180):
            time.sleep(1)
            s = self.call("GET", f"/core/transaction/v2/result/summary/{tid}")
            # summary/{id} can answer for a later id (purged ids round up) and shows non-final states
            # mid-flight (read right after the POST it said success False) => wait for OUR id complete|failed
            if s.get("id") == tid and s.get("state") in ("complete", "failed"):
                return s
        raise RuntimeError(f"transaction {tid} did not finish in 180 s")

    def execution(self, tid: int) -> dict:
        return self.call("GET", f"/core/transaction/v2/result/execution/{tid}") or {}


def subif(intf: dict, index: int = 1) -> tuple[str, str]:
    """Interface CR -> (leaf, SR Linux subinterface), e.g. ('l1-1', 'ethernet-1/1/2.1').
    index 1 = backend gpuVLAN 1 (routed), index 2 = frontend VLAN 2 (bridged)."""
    m = intf["spec"]["members"][0]
    return m["node"], m["interface"].replace("-", "/").replace("ethernet/", "ethernet-") + f".{index}"


def norm_tenant(t: str) -> str:
    t = t.removeprefix("tenant-").removeprefix("tenant")
    if t not in ("1", "2"):
        sys.exit(f"unknown tenant '{t}' (tenant-1 | tenant-2)")
    return t


def tenant_of(intf: dict) -> str | None:
    return intf["metadata"].get("labels", {}).get("tenant")


def tname(t: str | None) -> str:
    return f"tenant-{t}" if t else "free"


def cmd_show(_args) -> int:
    api = Api(USER, PASSWORD)
    vrfs, pfx = api.leaf_vrfs(), api.gpu_prefixes()
    addr = api.gpu_addresses(pfx)
    print(f"{'worker':9} {'server':6} {'GPU':4} {'rail leaf port':22} {'tenant':9} {'backend VLAN 1':15} "
          f"{'GPU address (SLAAC, leaf ND)':31} frontend VLAN 2")
    count: dict[str, int] = {}
    for vm, i in api.tenant_interfaces().items():
        t = tenant_of(i)
        count[tname(t)] = count.get(tname(t), 0) + 1
        leaf, si = subif(i)
        park = "-" if t else "parked"
        p = pfx.get((leaf, si[:-2]))
        a = addr.get((leaf, si[:-2])) or (f"{p}::/64 (no traffic yet)" if p else "-")
        print(f"{vm:9} {vm.rsplit('-g', 1)[0]:6} {'GPU' + vm.rsplit('-g', 1)[1]:4} {leaf + ' ' + si[:-2]:22} {tname(t):9} "
              f"{vrfs.get((leaf, si), park):15} {a:31} {vrfs.get(subif(i, 2), park)}")
    print("GPUs: " + " · ".join(f"{k} {v}" for k, v in sorted(count.items())))
    return 0


def in_cluster(*kubectl_args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """kubectl against the management cluster (runs inside ocp-m1; the operator's machine cannot reach 10.20/14)."""
    return subprocess.run(eda.kubectl_args() + ["-n", "eda-system", "exec", "deploy/cx-aifabric--ocp-m1-sim", "-c", "ocp-m1",
                           "--", "kubectl", "--kubeconfig", "/etc/kubernetes/admin.conf", *kubectl_args],
                          capture_output=True, text=True, timeout=timeout)


def change_in_cluster(todo: dict[str, str | None]) -> int:
    """Delegate to k8s/gpu-orchestrator.py: set the desired state for all GPUs at once, follow its log."""
    patch = {vm: tname(t) for vm, t in todo.items()}
    logs = lambda: in_cluster("-n", "gpu-orchestrator", "logs", "deploy/gpu-orchestrator", "-c", "orchestrator",
                              "--since=10m").stdout.splitlines()
    seen = set(logs())          # earlier batches may name the same GPUs: only lines written after our patch count
    print("the GPU orchestrator runs inside the management cluster (ocp-mgmt) => setting the desired state")
    print(f"  kubectl -n gpu-orchestrator patch configmap gpu-tenancy  {json.dumps(patch)}")
    r = in_cluster("-n", "gpu-orchestrator", "patch", "configmap", "gpu-tenancy", "--type", "merge",
                   "-p", json.dumps({"data": patch}))
    if r.returncode != 0:
        sys.exit(r.stderr.strip())
    t0 = time.monotonic()
    while time.monotonic() - t0 < 420:
        for line in logs():
            if line in seen:
                continue
            ours = any(f"{vm}:" in line or f"{vm} " in line for vm in todo)
            if ours and ("batch" in line or "->" in line) or ("ERROR" in line and time.monotonic() - t0 > 2):
                seen.add(line)
                print("  " + line)
                if "batch done" in line and all(vm in line for vm in todo):
                    return 0
        time.sleep(2)
    print("  no 'batch done' from the orchestrator after 420 s")
    return 1


def targets(specs: list[str], intfs: dict[str, dict]) -> list[str]:
    out: list[str] = []
    for sp in specs:
        if sp in intfs:
            out.append(sp)
        elif re.fullmatch(r"gpu-\d+", sp) and any(k.startswith(sp + "-g") for k in intfs):
            out += [k for k in intfs if k.startswith(sp + "-g")]
        else:
            sys.exit(f"unknown GPU '{sp}' (gpu-<s>-g<0-3> or a whole server gpu-<s>)")
    return list(dict.fromkeys(out))


def change(args, new: str | None) -> int:
    api = Api(USER, PASSWORD)
    intfs = api.tenant_interfaces()
    vms = targets(args.gpus, intfs)
    verb = f"book for {tname(new)}" if new else "release"
    return apply(args, api, intfs, {vm: new for vm in vms}, verb)


def baseline_booking() -> dict[str, str | None]:
    """The deploy-time booking, from topology/gen-topology.py GPU_SERVERS."""
    src = (HERE / "topology" / "gen-topology.py").read_text()
    ns: dict = {}
    exec(src[src.index("GPU_SERVERS = "):src.index("# storage-island hosts")], ns)
    out: dict[str, str | None] = {}
    for srv, booking in ns["GPU_SERVERS"].items():
        for g, t in enumerate(booking):
            out[f"{srv}-g{g}"] = str(t) if t else None
    return out


def cmd_baseline(args) -> int:
    api = Api(USER, PASSWORD)
    return apply(args, api, api.tenant_interfaces(), baseline_booking(), "back to the deploy-time booking")


def apply(args, api: Api, intfs: dict[str, dict], want: dict[str, str | None], verb: str) -> int:
    """want = {worker: tenant | None (free)}; only the differences are changed, in ONE batch / transaction."""
    todo = {vm: t for vm, t in want.items() if tenant_of(intfs[vm]) != t}
    print(f"{verb}: " + ", ".join(f"{vm} ({tname(tenant_of(intfs[vm]))} -> {tname(t)})" for vm, t in todo.items())
          + (f"; already there: {len(want) - len(todo)}" if len(want) > len(todo) else "")
          + f"   [EDA user {api.user}]")
    if not todo:
        print("nothing to do")
        return 0
    if not args.direct and not args.dry_run \
            and in_cluster("-n", "gpu-orchestrator", "get", "configmap", "gpu-tenancy").returncode == 0:
        return change_in_cluster(todo)
    crs = [{"type": {"patch": {
        "target": {"gvk": IF_GVK, "name": intfs[vm]["metadata"]["name"], "namespace": NS},
        "patchOps": [{"op": "add", "path": "/metadata/labels/tenant", "value": t} if t
                     else {"op": "remove", "path": "/metadata/labels/tenant"}]}}} for vm, t in todo.items()]
    desc = f"gpu-orchestrator: {verb}: " + ", ".join(f"{vm} {tname(t)}" for vm, t in todo.items())
    t0 = time.monotonic()
    dry = api.transaction(desc + " (dry-run)", crs, dry_run=True)
    ex = api.execution(dry["id"])
    nodes = [n.get("name", n) if isinstance(n, dict) else n for n in ex.get("nodesWithConfigChanges", [])]
    print(f"dry-run  txn {dry['id']}: success={dry.get('success')}  nodes with config changes: "
          f"{', '.join(sorted(nodes)) or '-'}")
    if not dry.get("success"):
        print(json.dumps(ex.get("generalErrors") or ex.get("intentsRun"), indent=1)[:2000])
        return 1
    if args.dry_run:
        return 0
    res = api.transaction(desc, crs, dry_run=False)
    print(f"commit   txn {res['id']}: success={res.get('success')}  ({time.monotonic() - t0:.1f} s incl. dry-run)")
    if not res.get("success"):
        return 1
    print("waiting for the leaves to report each GPU port in its new tenant (or parked) ...")
    vrfs = api.leaf_vrfs()
    fe = any(subif(i, 2) in vrfs for i in intfs.values() if tenant_of(i))       # VLAN 2 only if the frontend exists
    want = {vm: ({1: f"tenant-{t}", 2: f"fe-tenant-{t}-bd"} if t else {1: None, 2: None}) for vm, t in todo.items()}
    idx = [1, 2] if fe else [1]
    for _ in range(60):
        got = {(vm, x): vrfs.get(subif(intfs[vm], x)) for vm in todo for x in idx}
        if all(v == want[vm][x] for (vm, x), v in got.items()):
            break
        time.sleep(2)
        vrfs = api.leaf_vrfs()
    for (vm, x), v in sorted(got.items()):
        leaf, si = subif(intfs[vm], x)
        print(f"  {vm:9} {leaf:5} {si:20} {v or 'parked (no subinterface)'}")
    print(f"fabric done in {time.monotonic() - t0:.1f} s")
    if not args.fabric_only:
        print(f"host side: hosts.sh {' '.join(todo)} (address or park the VM's NIC; Kubernetes untouched in direct mode)")
        r = subprocess.run(["bash", str(HERE / "hosts.sh"), *todo], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if line.lstrip().startswith(("backend", "frontend ", "-->")):
                print("  " + line.strip())
        if r.returncode != 0:
            print(r.stdout[-1500:] + r.stderr[-500:])
            return 1
        print(f"done in {time.monotonic() - t0:.1f} s")
    return 0


def cmd_book(args) -> int:
    return change(args, norm_tenant(args.tenant))


def cmd_release(args) -> int:
    return change(args, None)


def cmd_setup(_args) -> int:
    K = eda.kubectl_args()
    eda.edactl(K, "apply", ROLE_FILE, print)
    a = eda.Admin(EDA_URL, eda.eda_client_secret(EDA_URL))
    g = a.find("groups", "name", GROUP)
    if not g:
        a.call("POST", "/core/admin/groups", {"name": GROUP, "description": "GPU tenant orchestrator (aifabric demo)"})
        g = a.find("groups", "name", GROUP)
    a.call("PUT", f"/core/admin/groups/{g['uuid']}/roles", [f"{NS}:{ROLE}"])
    u = a.find("users", "username", USER)
    if not u:
        a.call("POST", "/core/admin/users", {"username": USER, "email": f"{USER}@example.com", "enabled": True,
                                             "firstName": "GPU", "lastName": "Orchestrator"})
        u = a.find("users", "username", USER)
    a.call("PUT", f"/core/admin/groups/{g['uuid']}/users/{u['uuid']}")
    a.call("PUT", f"/core/admin/users/{u['uuid']}/resetpassword", {"value": PASSWORD, "temporary": False})
    Api(USER, PASSWORD)
    print(f"user {USER} in group {GROUP}, role {NS}:{ROLE}, login OK")
    return 0


def cmd_remove(_args) -> int:
    a = eda.Admin(EDA_URL, eda.eda_client_secret(EDA_URL))
    for kind, key, name in (("users", "username", USER), ("groups", "name", GROUP)):
        x = a.find(kind, key, name)
        if x:
            a.call("DELETE", f"/core/admin/{kind}/{x['uuid']}")
            print(f"deleted {kind[:-1]} {name}")
    K = eda.kubectl_args()
    if eda.role_exists(K, "roles", NS, ROLE):
        eda.edactl(K, "delete", ROLE_FILE, print)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("setup").set_defaults(fn=cmd_setup)
    sp.add_parser("remove").set_defaults(fn=cmd_remove)
    sp.add_parser("show").set_defaults(fn=cmd_show)
    b = sp.add_parser("baseline", help="restore the deploy-time booking (topology/gen-topology.py) in one batch")
    b.add_argument("--dry-run", action="store_true")
    b.add_argument("--fabric-only", action="store_true")
    b.add_argument("--direct", action="store_true")
    b.set_defaults(fn=cmd_baseline)
    for name, fn in (("book", cmd_book), ("release", cmd_release)):
        m = sp.add_parser(name)
        if name == "book":
            m.add_argument("tenant", help="tenant-1 | tenant-2")
        m.add_argument("gpus", nargs="+", help="gpu-<s>-g<G> | gpu-<s> (all 4)")
        m.add_argument("--dry-run", action="store_true", help="EDA dry-run only")
        m.add_argument("--fabric-only", action="store_true", help="skip the host side (hosts.sh)")
        m.add_argument("--direct", action="store_true", help="relabel directly, bypassing the in-cluster orchestrator")
        m.set_defaults(fn=fn)
    args = p.parse_args()
    if not EDA_URL:
        eda.eda_url()            # exits with a hint: run ./deploy.sh first or set EDA_URL
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
