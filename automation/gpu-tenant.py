#!/usr/bin/env python3
"""gpu-tenant.py: give GPUs to tenants on the aifabric fabric through the EDA REST API.

A GPU belongs to a tenant through ONE label on its switch port (the EDA Interface): tenant=<N>. The aifabrics
Backend then puts the port into the tenant's VRF (VLAN 1, a /64 from the tenant's pool), and the frontend VLAN 2
follows the same label. No label = a free GPU: its port carries nothing.

WHICH PORT a GPU is on comes from the switches, not only from EDA's inventory. LLDP on the leaf port names the host
behind it (the GPU's NIC), and EDA's topology labels on that port (gpu-server, rail) must say the same. If they
disagree, or the port has no LLDP neighbour, the whole change is refused (strict: owner decision R2-Q1 B,
2026-11-18). After a booking the leaf's IPv6 neighbour table proves it: the NIC with the LLDP MAC took an address
from the tenant's /64 (SLAAC).

  gpu-tenant.py list                               every GPU: port, LLDP check, tenant, VRF, address
  gpu-tenant.py assign tenant-1 gpu-3-g2 gpu-4     book GPUs (gpu-<s>-g<G>, or gpu-<s> = all its GPUs)
  gpu-tenant.py unassign gpu-3-g2                  back to the free pool
  gpu-tenant.py apply tenancy.yaml                 make the fabric match the file (only the differences)
      --dry-run   EDA dry run only
      --move      assign: allow taking a GPU from another tenant (apply always may: the file is the intent)
      --no-wait   return after the commit, do not wait for the switches and the GPU NICs

Every change = one EDA dry run, then ONE transaction for all GPUs of the command (all or nothing), as the EDA user
gpu-orchestrator (Role aifabric:gpu-orchestrator: read namespace aifabric, write Interfaces only).
Env: EDA_URL (default: the connection ./deploy.sh saved in .state/), GPU_TENANT_USER / GPU_TENANT_PASSWORD
(default gpu-orchestrator), EDA_CLIENT_SECRET (else read with the Keycloak admin, KC_MASTER_USER / _PASSWORD).
Exit codes: 0 done · 1 EDA refused or failed · 2 refused before EDA (input, inventory or LLDP check).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
import eda_auth as eda  # noqa: E402

NS = "aifabric"
BACKEND = "aifabric"
IF_GVK = {"group": "interfaces.eda.nokia.com", "version": "v1", "kind": "Interface"}
GPU_RE = re.compile(r"^gpu-\d+-g\d+$")
SERVER_RE = re.compile(r"^gpu-\d+$")


class Refused(Exception):
    """Refused before anything reached EDA (exit 2)."""


class Api:
    """EDA REST API as one user: password grant through EDA's Keycloak client `eda`."""

    def __init__(self, url: str, user: str, password: str):
        self.url = url
        secret = os.environ.get("EDA_CLIENT_SECRET") or eda.eda_client_secret(url)
        # EDA's Keycloak refuses CONCURRENT password logins of the same user (HTTP 400 "Invalid user credentials",
        # measured 2026-11-12 and again here with two operators at once) => retry with a random back-off
        for attempt in range(6):
            st, tok = eda.user_token(url, secret, user, password)
            if st == 200 or st != 400 or attempt == 5:
                break
            time.sleep(0.5 + random.random() * 1.5)
        if st != 200:
            sys.exit(f"login as {user} failed: HTTP {st} {tok}")
        self.user = user
        self.h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        st, b = eda.http(method, self.url + path, self.h, None if body is None else json.dumps(body).encode())
        if st not in ok:
            raise RuntimeError(f"{method} {path} -> HTTP {st} {b[:400]}")
        return json.loads(b) if b.strip() else None

    def eql(self, query: str) -> list[dict]:
        # `namespaces` is required for a namespaced Role (else 403 CannotReadDbTable)
        path = "/core/query/v1/eql?" + urllib.parse.urlencode({"query": query, "namespaces": NS})
        return (self.call("GET", path) or {}).get("data") or []

    def transaction(self, description: str, crs: list[dict], dry_run: bool) -> dict:
        r = self.call("POST", "/core/transaction/v2", {"description": description, "dryRun": dry_run, "crs": crs})
        tid = r["id"]
        for _ in range(600):
            time.sleep(0.5)
            s = self.call("GET", f"/core/transaction/v2/result/summary/{tid}")
            # summary/{id} can answer for another id and shows non-final states mid-flight => wait for OUR id
            if s.get("id") == tid and s.get("state") in ("complete", "failed"):
                return s
        raise RuntimeError(f"transaction {tid} did not finish in 300 s")


def sr_if(member: str) -> str:
    """EDA member interface name -> SR Linux name: ethernet-1-2-1 -> ethernet-1/2/1."""
    return "ethernet-" + member.removeprefix("ethernet-").replace("-", "/")


class Port:
    """One GPU port = one EDA Interface with label role=gpu."""

    def __init__(self, intf: dict):
        self.intf = intf
        self.name = intf["metadata"]["name"]
        self.labels = intf["metadata"].get("labels") or {}
        m = intf["spec"]["members"][0]
        self.node, self.ifname = m["node"], sr_if(m["interface"])
        gs, rail = self.labels.get("gpu-server"), self.labels.get("rail", "")
        self.gpu_by_label = f"{gs}-g{int(rail) - 1}" if gs and rail.isdigit() else None
        self.lldp_name = self.lldp_mac = None
        self.tenant = self.labels.get("tenant")          # label value, e.g. "1"; None = free


class Fabric:
    """What EDA and the switches say right now."""

    def __init__(self, api: Api):
        self.api = api
        items = api.call("GET", f"/apps/interfaces.eda.nokia.com/v1/namespaces/{NS}/interfaces?"
                         + urllib.parse.urlencode({"labelSelector": "role=gpu"})).get("items", [])
        # filter again here: the EDA list ignores a misspelt selector silently (labelselector != labelSelector)
        self.ports = [Port(i) for i in items if (i["metadata"].get("labels") or {}).get("role") == "gpu"]
        by_if = {(p.node, p.ifname): p for p in self.ports}
        for r in api.eql(".namespace.node.srl.system.lldp.interface.neighbor fields [system-name, chassis-id]"):
            p = by_if.get((r.get(".namespace.node.name"), r.get(".namespace.node.srl.system.lldp.interface.name")))
            if p:
                p.lldp_name, p.lldp_mac = r.get("system-name") or None, (r.get("chassis-id") or "").lower() or None
        # tenants = the Backend's GPU isolation groups; the selector names the label value ("role=gpu,tenant=1")
        be = api.call("GET", f"/apps/aifabrics.eda.nokia.com/v1/namespaces/{NS}/backends/{BACKEND}")
        self.tenants: dict[str, str] = {}
        for g in be["spec"].get("gpuIsolationGroups") or []:
            for sel in g.get("interfaceSelectors") or []:
                m = re.search(r"(?:^|,)tenant=([^,]+)", sel)
                if m:
                    self.tenants[g["name"]] = m.group(1)
        self.tenant_of_value = {v: k for k, v in self.tenants.items()}
        vns = api.call("GET", f"/apps/services.eda.nokia.com/v2/namespaces/{NS}/virtualnetworks").get("items", [])
        self.frontend = any(v["metadata"]["name"].startswith("fe-tenant-") for v in vns)

    def gpus(self) -> list[str]:
        names = {p.gpu_by_label for p in self.ports if p.gpu_by_label}
        names |= {p.lldp_name for p in self.ports if p.lldp_name and GPU_RE.match(p.lldp_name)}
        return sorted(names, key=lambda n: [int(x) for x in re.findall(r"\d+", n)])

    def locate(self, gpu: str) -> Port:
        """The port of one GPU, when EDA's labels AND LLDP agree; else Refused with both views."""
        by_label = [p for p in self.ports if p.gpu_by_label == gpu]
        by_lldp = [p for p in self.ports if p.lldp_name == gpu]
        if not by_label and not by_lldp:
            raise Refused(f"{gpu}: unknown GPU (no port in EDA's inventory, no LLDP neighbour of that name)")
        problems = []
        if len(by_label) > 1:
            problems.append("EDA's labels name it on " + ", ".join(f"{p.node} {p.ifname}" for p in by_label))
        if len(by_lldp) > 1:
            problems.append("LLDP sees it on " + ", ".join(f"{p.node} {p.ifname}" for p in by_lldp))
        if not problems:
            lab, phy = (by_label or [None])[0], (by_lldp or [None])[0]
            if lab is not None and lab is phy:
                return lab
            if lab is None:
                problems.append(f"LLDP sees it on {phy.node} {phy.ifname}, but EDA's labels there say "
                                f"{phy.gpu_by_label or 'nothing'} (gpu-server/rail)")
            elif phy is None:
                seen = f"LLDP sees {lab.lldp_name}" if lab.lldp_name else "no LLDP neighbour there (NIC down or silent)"
                problems.append(f"EDA's labels put it on {lab.node} {lab.ifname}, but {seen}")
            else:
                problems.append(f"EDA's labels put it on {lab.node} {lab.ifname}, LLDP sees it on {phy.node} {phy.ifname}")
        raise Refused(f"{gpu}: inventory and switches disagree: " + "; ".join(problems)
                      + " => fix the cabling or the topology first")

    def expand(self, specs: list[str]) -> list[str]:
        out: list[str] = []
        known = self.gpus()
        for s in specs:
            if GPU_RE.match(s):
                out.append(s)
            elif SERVER_RE.match(s) and any(g.startswith(s + "-g") for g in known):
                out += [g for g in known if g.startswith(s + "-g")]
            else:
                raise Refused(f"'{s}': not a GPU name (gpu-<server>-g<index>, e.g. gpu-2-g0, or gpu-<server>)")
        return list(dict.fromkeys(out))

    def tenant_value(self, name: str) -> str:
        if name not in self.tenants:
            raise Refused(f"unknown tenant '{name}' (tenants: {', '.join(sorted(self.tenants)) or 'none'})")
        return self.tenants[name]

    def tname(self, value: str | None) -> str:
        return "free" if value is None else self.tenant_of_value.get(value, f"tenant={value}?")

    # ---- switch state (EQL) ----
    def leaf_ni(self) -> dict[tuple[str, str], str]:
        """(leaf, subinterface) -> network-instance."""
        return {(r[".namespace.node.name"], r["name"]): r[".namespace.node.srl.network-instance.name"]
                for r in self.api.eql(".namespace.node.srl.network-instance.interface fields [name]")
                if r.get(".namespace.node.name") and r.get("name") and r.get(".namespace.node.srl.network-instance.name")}

    def port_prefixes(self) -> dict[tuple[str, str], ipaddress.IPv6Network]:
        """(leaf, port) -> the GPU /64 on subinterface 1 (the leaf holds ::1)."""
        out = {}
        for r in self.api.eql(".namespace.node.srl.interface.subinterface.ipv6.address fields [ip-prefix] "
                              "where (.namespace.node.srl.interface.subinterface.index = 1)"):
            pf = r.get("ip-prefix", "")
            if pf and not pf.startswith("fe80"):
                out[(r[".namespace.node.name"], r[".namespace.node.srl.interface.name"])] = \
                    ipaddress.IPv6Interface(pf).network
        return out

    def neighbours(self) -> dict[tuple[str, str], list[tuple[str, str]]]:
        """(leaf, port) -> [(IPv6 address, MAC)] the leaf learned on subinterface 1 (IPv6 neighbour discovery)."""
        out: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for r in self.api.eql(".namespace.node.srl.interface.subinterface.ipv6.neighbor-discovery.neighbor "
                              "fields [link-layer-address] where (.namespace.node.srl.interface.subinterface.index = 1)"):
            key = (r.get(".namespace.node.name"), r.get(".namespace.node.srl.interface.name"))
            if r.get("ipv6-address") and r.get("link-layer-address"):
                out.setdefault(key, []).append((r["ipv6-address"], r["link-layer-address"].lower()))
        return out

    def proof(self, p: Port, pfx, nbrs) -> str | None:
        """The GPU NIC's address in the port's /64, learned from the NIC whose MAC LLDP reported."""
        net = pfx.get((p.node, p.ifname))
        if not net:
            return None
        for a, mac in nbrs.get((p.node, p.ifname), []):
            if ipaddress.IPv6Address(a) in net and (p.lldp_mac is None or mac == p.lldp_mac):
                return a
        return None


def cmd_list(f: Fabric, _args) -> int:
    ni, pfx, nbrs = f.leaf_ni(), f.port_prefixes(), f.neighbours()
    print(f"{'GPU':10} {'leaf port':20} {'LLDP neighbour':15} {'check':9} {'tenant':9} {'VLAN 1 VRF':11} "
          f"{'GPU address (leaf ND)':32} VLAN 2")
    count: dict[str, int] = {}
    bad = 0
    for p in sorted(f.ports, key=lambda p: (p.gpu_by_label or "~", p.node, p.ifname)):
        try:
            ok = f.locate(p.gpu_by_label) is p if p.gpu_by_label else False
        except Refused:
            ok = False
        bad += not ok
        t = f.tname(p.tenant)
        count[t] = count.get(t, 0) + 1
        addr = f.proof(p, pfx, nbrs) or (f"{pfx[(p.node, p.ifname)]} (no ND yet)" if (p.node, p.ifname) in pfx else "-")
        print(f"{p.gpu_by_label or '?':10} {p.node + ' ' + p.ifname:20} {p.lldp_name or '-':15} "
              f"{'ok' if ok else 'MISMATCH':9} {t:9} {ni.get((p.node, p.ifname + '.1'), 'parked'):11} {addr:32} "
              f"{ni.get((p.node, p.ifname + '.2'), 'parked') if f.frontend else '-'}")
    print("GPUs: " + " · ".join(f"{k} {v}" for k, v in sorted(count.items()))
          + (f" · {bad} port(s) where EDA's labels and LLDP disagree" if bad else " · inventory = LLDP on every port"))
    return 0


def plan_change(f: Fabric, want: dict[str, str | None], move_ok: bool) -> list[tuple[str, Port, str | None]]:
    """want = {GPU: label value | None}; strict checks for every GPU named; returns [(gpu, port, new)] that change."""
    ports, errors = {}, []
    for g in want:
        try:
            ports[g] = f.locate(g)
        except Refused as e:
            errors.append(str(e))
    if errors:
        raise Refused("\n".join(errors))
    taken = [g for g, v in want.items() if v is not None and ports[g].tenant not in (None, v)]
    if taken and not move_ok:
        raise Refused("GPU(s) in use by another tenant: "
                      + ", ".join(f"{g} ({f.tname(ports[g].tenant)})" for g in taken) + " => add --move to take them")
    return [(g, ports[g], v) for g, v in want.items() if ports[g].tenant != v]


def execute(f: Fabric, todo: list[tuple[str, Port, str | None]], verb: str, args) -> int:
    if not todo:
        print("nothing to change: the fabric already matches")
        return 0
    print(f"{verb}: " + ", ".join(f"{g} {f.tname(p.tenant)} -> {f.tname(v)}" for g, p, v in todo)
          + f"   [EDA user {f.api.user}]")
    # compare-and-swap: JSON patch `test` = the port's labels must still be what this run read, else EDA refuses the
    # WHOLE transaction (HTTP 400 FailedToApplyPatch "test failed", measured 2026-11-18) => no silent overwrite of
    # another operator's change made since our read
    crs = [{"type": {"patch": {
        "target": {"gvk": IF_GVK, "name": p.name, "namespace": NS},
        "patchOps": [{"op": "test", "path": "/metadata/labels", "value": p.labels},
                     {"op": "add", "path": "/metadata/labels/tenant", "value": v} if v is not None
                     else {"op": "remove", "path": "/metadata/labels/tenant"}]}}} for _, p, v in todo]
    desc = f"gpu-tenant.py: {verb}: " + ", ".join(f"{g} {f.tname(v)}" for g, _, v in todo)
    t0 = time.monotonic()
    try:
        dry = f.api.transaction(desc + " (dry run)", crs, dry_run=True)
        t_dry = time.monotonic() - t0
        if dry.get("success") and not args.dry_run:
            res = f.api.transaction(desc, crs, dry_run=False)
            t_commit = time.monotonic() - t0
    except RuntimeError as e:
        if "test failed" not in str(e):
            raise
        print("REFUSED by EDA, nothing committed: a GPU of this change was changed by someone else after this run "
              "read it (compare-and-swap). Run the command again to decide on the new state.", file=sys.stderr)
        return 1
    ex = f.api.call("GET", f"/core/transaction/v2/result/execution/{dry['id']}") or {}
    nodes = sorted(n.get("name", n) if isinstance(n, dict) else n for n in ex.get("nodesWithConfigChanges") or [])
    print(f"  dry run  transaction {dry['id']}: {'OK' if dry.get('success') else 'FAILED'}, switches changed: "
          f"{', '.join(nodes) or '-'}  ({t_dry:.1f} s)")
    if not dry.get("success"):
        print(json.dumps(ex.get("generalErrors") or ex.get("intentsRun"), indent=1)[:2000])
        return 1
    if args.dry_run:
        return 0
    print(f"  commit   transaction {res['id']}: {'OK' if res.get('success') else 'FAILED'}  ({t_commit:.1f} s)")
    if not res.get("success"):
        return 1
    if args.no_wait:
        return 0
    # the switches: VLAN 1 subinterface in the tenant VRF (or gone), VLAN 2 in the tenant's frontend bridge domain
    want = {g: (f.tname(v) if v is not None else None) for g, _, v in todo}
    for _ in range(60):
        ni = f.leaf_ni()
        got = {g: ni.get((p.node, p.ifname + ".1")) for g, p, _ in todo}
        fe = {g: ni.get((p.node, p.ifname + ".2")) for g, p, _ in todo}
        if all(got[g] == want[g] and (not f.frontend or fe[g] == (f"fe-{want[g]}-bd" if want[g] else None))
               for g in want):
            break
        time.sleep(0.5)
    print(f"  switches done  ({time.monotonic() - t0:.1f} s): "
          + ", ".join(f"{g} {got[g] or 'parked'}" for g in want))
    # the GPU NICs: SLAAC address from the tenant's /64, learned by the leaf from the NIC LLDP reported
    def labels_now() -> dict[str, str | None]:
        return {i["metadata"]["name"]: (i["metadata"].get("labels") or {}).get("tenant")
                for i in f.api.call("GET", f"/apps/interfaces.eda.nokia.com/v1/namespaces/{NS}/interfaces?"
                                    + urllib.parse.urlencode({"labelSelector": "role=gpu"})).get("items", [])}

    # nothing locks a GPU between our read and our commit: another operator or tool may change it meanwhile
    # (measured: two runs on one GPU at once = both commit, the later one wins) => say so instead of claiming success
    booked = [(g, p) for g, p, v in todo if v is not None]
    proof: dict[str, str] = {}
    lost: list[tuple[str, str | None]] = []
    for _ in range(90):
        now = labels_now()
        lost = [(g, now.get(p.name)) for g, p, v in todo if now.get(p.name) != v]
        if len(proof) + len([g for g, _ in lost if g in dict(booked)]) >= len(booked):
            break
        ni, pfx, nbrs = f.leaf_ni(), f.port_prefixes(), f.neighbours()
        for g, p in booked:
            # the /64 must be the one of the tenant we asked for (the port's VRF now), not a later change's
            if g not in proof and ni.get((p.node, p.ifname + ".1")) == want[g] and (a := f.proof(p, pfx, nbrs)):
                proof[g] = a
                print(f"  {g:9} address {a}  (the NIC LLDP reported, {time.monotonic() - t0:.1f} s)")
        time.sleep(1)
    missing = [g for g, _ in booked if g not in proof and g not in dict(lost)]
    for g, cur in lost:
        print(f"  !! {g} is now {f.tname(cur)}: changed by another operator or tool while this ran (last change wins)")
    if missing and not lost:
        print(f"  no address learned yet for {', '.join(missing)} (the GPU host has not sent traffic yet?)")
    print(f"done in {time.monotonic() - t0:.1f} s")
    return 1 if lost else 0


def read_tenancy(path: Path) -> dict[str, str]:
    """tenancy.yaml = flat `gpu-<s>-g<G>: tenant-N | free` lines (comments allowed): the same file Terraform reads."""
    out: dict[str, str] = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9-]+)\s*:\s*\"?([A-Za-z0-9-]+)\"?", line)
        if not m:
            raise Refused(f"{path}:{n}: expected 'gpu-<s>-g<G>: <tenant>|free', got: {line}")
        if m.group(1) in out:
            raise Refused(f"{path}:{n}: {m.group(1)} listed twice")
        out[m.group(1)] = m.group(2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("list")
    for name in ("assign", "unassign", "apply"):
        p = sp.add_parser(name)
        if name == "assign":
            p.add_argument("tenant")
        if name == "apply":
            p.add_argument("file", type=Path)
        else:
            p.add_argument("gpus", nargs="+")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--no-wait", action="store_true")
        if name == "assign":
            p.add_argument("--move", action="store_true")
    args = ap.parse_args()
    api = Api(eda.eda_url(), os.environ.get("GPU_TENANT_USER", "gpu-orchestrator"),
              os.environ.get("GPU_TENANT_PASSWORD", "gpu-orchestrator"))
    f = Fabric(api)
    try:
        if args.cmd == "list":
            return cmd_list(f, args)
        if args.cmd == "assign":
            v = f.tenant_value(args.tenant)
            todo = plan_change(f, {g: v for g in f.expand(args.gpus)}, args.move)
            return execute(f, todo, f"assign to {args.tenant}", args)
        if args.cmd == "unassign":
            return execute(f, plan_change(f, {g: None for g in f.expand(args.gpus)}, True), "unassign", args)
        want: dict[str, str | None] = {}
        for g, t in read_tenancy(args.file).items():
            if not GPU_RE.match(g):
                raise Refused(f"{args.file}: '{g}' is not a GPU name")
            want[g] = None if t == "free" else f.tenant_value(t)
        return execute(f, plan_change(f, want, True), f"apply {args.file.name}", args)
    except Refused as e:
        print(f"REFUSED, nothing sent to EDA:\n{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
