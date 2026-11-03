#!/usr/bin/env python3
"""Failure scene: take a spine out of the GPU backend and bring it back, through EDA.

  fail-spine.py l2-1 down     ONE EDA transaction disables all 42 ISL Interfaces of the spine
  fail-spine.py l2-1 up       ONE transaction re-enables them
  fail-spine.py status        ISL admin state per spine

What to watch: the traffic matrix (aifabric-traffic.py). Same-rail GPU flows never touch a spine;
cross-rail flows (leaf -> spine -> leaf) lose the failed spine's half of the ECMP/DLB paths and
continue over the other spine after BGP withdraws the routes. Runs as the EDA admin user
(an operator action, not the orchestrator's: its Role cannot touch ISLs).
"""
import argparse, json, os, subprocess, sys, time
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import eda_auth as eda  # noqa: E402

NS = "aifabric"
EDA_URL = eda.eda_url()
IF_GVK = {"group": "interfaces.eda.nokia.com", "version": "v1", "kind": "Interface"}


def isls(spine=None):
    out = subprocess.run(eda.kubectl_args() + ["-n", NS, "get", "interfaces.interfaces.eda.nokia.com",
                                               "-l", "role=interSwitch", "-o", "json"],
                         capture_output=True, text=True, check=True).stdout
    items = json.loads(out)["items"]
    return [i for i in items if i["spec"]["members"][0]["node"].startswith("l2-")
            and (spine is None or i["spec"]["members"][0]["node"] == spine)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("spine", nargs="?", default="l2-1")
    ap.add_argument("action", nargs="?", choices=["down", "up", "status"], default="status")
    a = ap.parse_args()
    if a.spine == "status" or a.action == "status":
        for sp in ("l2-1", "l2-2"):
            st = [i["spec"].get("enabled", True) for i in isls(sp)]
            print(f"{sp}: {sum(st)}/{len(st)} ISL Interfaces enabled")
        return 0
    want = a.action == "up"
    targets = isls(a.spine)
    if len(targets) != 42:
        sys.exit(f"{a.spine}: expected 42 ISL Interfaces, found {len(targets)}")
    adm = eda.Admin(EDA_URL, eda.eda_client_secret(EDA_URL))
    crs = [{"type": {"patch": {"target": {"gvk": IF_GVK, "name": i["metadata"]["name"], "namespace": NS},
                               "patchOps": [{"op": "replace" if "enabled" in i["spec"] else "add",
                                             "path": "/spec/enabled", "value": want}]}}} for i in targets]
    desc = f"failure scene: spine {a.spine} {'back in service' if want else 'isolated'} (42 ISLs {'enabled' if want else 'disabled'})"
    t0 = time.monotonic()
    tid = adm.call("POST", "/core/transaction/v2", {"description": desc, "dryRun": False, "crs": crs})["id"]
    for _ in range(120):
        time.sleep(1)                       # the summary of a just-posted id is not final yet (read too early = success False)
        s = adm.call("GET", f"/core/transaction/v2/result/summary/{tid}")
        if s.get("id") == tid and s.get("state") in ("complete", "failed"):
            break
    print(f"txn {tid}: {desc} -> success={s.get('success')} ({time.monotonic() - t0:.1f} s)")
    return 0 if s.get("success") else 1


if __name__ == "__main__":
    sys.exit(main())
