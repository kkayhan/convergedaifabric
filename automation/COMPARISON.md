# Booking GPUs for tenants: custom script vs Terraform

On this fabric a GPU belongs to a tenant through **one label on its switch port**: the EDA Interface of the GPU's
NIC carries `tenant=1` or `tenant=2`, or no label when the GPU is free. The aifabrics Backend and the frontend
VirtualNetworks follow that label by themselves: the port joins the tenant's VRF on VLAN 1 with a /64 from the
tenant's pool, and its VLAN 2 joins the tenant's frontend. So automating GPU bookings means automating that one
label. Both tools here do exactly that, against the same lab, with the same safety rules, so the comparison is
about the tools and not about two different designs.

| | Script | Terraform |
|---|---|---|
| Tool | `automation/gpu-tenant.py` (Python, EDA REST API) | `automation/terraform/` (Nokia's EDA providers) |
| How you ask | `gpu-tenant.py assign tenant-1 gpu-3-g2` | edit `gpu-3-g2: tenant-1` in `tenancy.yaml`, then `terraform plan` and `terraform apply` |
| Desired state | the command (or `gpu-tenant.py apply tenancy.yaml`) | `tenancy.yaml` (the same file) |
| EDA identity | user `gpu-orchestrator` | user `gpu-terraform` |

## The same rules in both

- **Which port a GPU is on comes from the switches.** LLDP on the leaf port names the host behind it. EDA's
  inventory labels on that port (`gpu-server`, `rail`) must say the same, or the change is refused before anything
  reaches EDA. A cabling mistake or a wrong topology label is reported, with both views, never booked over.
  IPv6 neighbour discovery cannot do this job on its own: before a booking the port has no subinterface, so there is
  no router advertisement, no SLAAC and no neighbour entry, and neighbour discovery carries a MAC and an address but
  never a name. It serves as the proof afterwards: the NIC with the MAC that LLDP reported took an address from the
  tenant's /64.
- **EDA dry run first.** EDA checks the exact change and names the switches it touches before anything is
  committed.
- **Least privilege.** Both EDA users are in the role `aifabric:gpu-orchestrator`: they can read the namespace and
  write Interfaces, nothing else. Neither can change the Backend, a VirtualNetwork or a switch directly.
- **Nothing on the GPU host.** Each GPU runs a small host agent that reacts only to what its switch port advertises.
  It takes the tenant's /64 by SLAAC on a booking, swaps prefixes on a move and drops the address on a release, with
  no script and no access to EDA. The frontend address (VLAN 2) is still set by `hosts.sh --follow`.

## Measured (Talos lab, EDA 26.8.2, 18 November 2026)

| Test | Script | Terraform |
|---|---|---|
| Book 1 free GPU | **5.4 s** end to end. Dry run 0.5 s, commit and switches done 4.2 s, GPU NIC proven 4.4 s. One dry run + one transaction. | plan **5.5 s** (includes the EDA dry run) + apply **5.8 s**. GPU NIC addressed 6.9 s after the apply started. One transaction. |
| Book several GPUs at once | 4 GPUs in **one transaction**, 8.0 s until the last NIC was proven | 3 GPUs = **3 transactions** (EDA bundled 2 of them), apply 8.6 s |
| Move a GPU to the other tenant | 8.9 s. Refused unless `--move` is given | apply 6.0 s, one in-place update. No guard: the file is the intent |
| Back to the deploy-time booking from `tenancy.yaml` | 3.7 s, one transaction for 4 GPUs. A second run says "nothing to change" | plan 5.5 s + apply 8.5 s, 4 transactions (3 bundled) |
| Unknown GPU or tenant, malformed name, GPU in use | refused before EDA, exit code 2. One bad name refuses the whole command | `terraform plan` fails before EDA |
| GPU's LLDP identity does not match EDA's inventory (cables crossed) | that GPU is refused with both views; other GPUs can still be booked | **the whole plan** is refused while any GPU in the file is miscabled |
| GPU NIC silent (no LLDP) | refused | refused |
| Label changed outside the tool (EDA GUI, kubectl) | `list` shows the live state immediately, but only `apply --dry-run` shows that it differs from the file. `apply` fixes it (3.2 s) | the next `plan` shows the drift by itself; `apply` reverts it (5.3 s) |
| EDA revert of the tool's own transaction | the fabric is back as before; the script keeps no state, so the revert simply stands until someone runs it again | the next `plan` shows the revert as drift; `apply` re-asserts the file |
| Two operators at once, different GPUs | both succeed (after a login retry; EDA's Keycloak refuses two logins of the same user at the same instant) | the second `apply` in the same working directory is refused: "Error acquiring the state lock" |
| Two operators at once, same GPU | the later change is refused by EDA (compare-and-swap, below), or, when EDA bundles both commits, both commit and the losing run reports it and exits 1 | same working directory: locked out. Separate checkouts with their own state: no protection, last write wins |
| `terraform destroy` | — | refused on purpose (destroying the resource would delete the switch port's Interface) |
| What EDA's Transactions page shows | user `gpu-orchestrator`, "gpu-tenant.py: assign to tenant-1: gpu-3-g2 tenant-1", plus its dry run | user `gpu-terraform`; the dry run is described, every commit only says "REST" |
| Size | 354 lines of Python, plus a 133-line shared login library | 251 lines of HCL in 8 files, plus a 57-line setup script |
| What it needs to run | python3 3.9+, standard library only | the Terraform binary (123 MiB) + 4 providers (89 MiB) from the Terraform registry; an air-gapped site needs a provider mirror |

The GPU side behaved the same under both tools, because it does not know which tool made the change: a booked NIC
had its tenant /64 in 4-7 s, a moved one in about 8 s, a released one dropped its address within 5 s. The traffic
matrices showed 0 violations after every test.

## Custom script

**Pros**

- One command per action, in the operator's words, and it finishes when the GPU is really usable: it waits for the
  switches and proves that the NIC took its address.
- All-or-nothing: every command is one EDA transaction for all its GPUs, after one dry run.
- Precise audit trail: each transaction says what it did and for which GPUs.
- Protects concurrent operators without any state of its own: every change carries a JSON patch `test` on the labels
  it read, and EDA refuses the whole transaction when someone changed them meanwhile.
- Strictness per command: a miscabled GPU blocks only itself.
- No dependencies beyond Python; runs on an air-gapped jump host as it is. Easy to call from a GPU scheduler or a
  ticket workflow, because its exit codes say what happened.

**Cons**

- Imperative: the desired state lives only in the commands that were run. Drift is invisible until someone runs
  `apply --dry-run` against the file.
- All of it is our code: every feature, such as retries, locking, a merge-request mode or new EDA API versions, has
  to be written and maintained.
- Its compare-and-swap has a gap: two commits by the same EDA user in the same instant are bundled by EDA and both
  pass. The script detects this after the fact and reports it, but it cannot prevent it.
- No built-in review step. `--dry-run` shows the change, but nothing enforces a second person.

## Terraform

**Pros**

- Declarative and reviewable: the file is the intent, a pull request on it is the change request, and `plan` shows
  exactly what will change, together with EDA's own dry-run verdict.
- Drift detection for free: any change made outside Terraform, in the GUI, by kubectl or by an EDA revert, shows up
  in the next plan and the next apply puts it back.
- Standard tooling many teams already run in CI, with state and locking. It uses Nokia's own typed provider, and the
  same tool can own other EDA objects too (the nebnok demo onboards whole tenants this way).

**Cons**

- No all-or-nothing batch: Nokia's providers call each app's REST API, not EDA's Transaction API (EDA 26.8 docs,
  limitation 1), so every port is its own transaction and an apply can stop half way. The commits are described only
  as "REST".
- Noisy plans: one label change shows about 45 lines of "(known after apply)" around the one line that matters.
- Needs careful wiring to be safe on objects it did not create:
  - import blocks, so it adopts the ports instead of creating them;
  - `ignore_changes`, so it does not rewrite the whole Interface;
  - `prevent_destroy`, because a destroy would delete switch ports;
  - plain HTTP calls for the dry run and the switch checks;
  - a login order that avoids Keycloak's concurrent-login refusal;
  - `label_selector` rather than the deprecated `labelselector`, which EDA ignores.
- Does not wait for the fabric. Its checks only warn, and only on the next plan.
- Strictness is per file: one miscabled GPU anywhere blocks every change.
- A state file to protect and share. Locking works only between people using the same state, so a team needs a
  remote backend.
- About twice as slow per change (plan + apply ≈ 11 s against ≈ 5 s), and 212 MiB of binaries to install or mirror.

## Which one, when

Booking GPUs is an operational, frequent action that usually comes from a GPU scheduler or a self-service portal. It
must be atomic, immediate and proven. **That is the script's ground** (or the same logic inside the scheduler):
one transaction, a precise audit trail, done when the GPU works.

**Terraform fits where the GPU allocation is part of a reviewed baseline** that changes rarely, for example a
quarterly tenant contract in git, and where drift correction is the point. It is a natural owner for the tenants
themselves: their VRFs, frontends and isolation groups.

Whichever is chosen, **give the GPU labels one owner**. If Terraform owns them, every booking made by a script or in
the GUI is drift that the next `terraform apply` reverts.

## Found along the way (EDA 26.8.2)

- **Compare-and-swap works on EDA's transaction API.** A JSON patch `test` op on `/metadata/labels` with a stale
  value makes EDA refuse the whole transaction (HTTP 400 "test failed"). Exception: two commits by the same user
  within the bundling instant are checked together.
- **EDA bundles transactions** that the same user posts at the same moment. Terraform's parallel applies show it as
  "bundled N transactions".
- **Keycloak refuses concurrent password logins of one user.** Two script runs started together failed with HTTP 400
  until the script retried with a random back-off.
- **DHCP for the frontend does not work across the route-leaked frontend.** The plan was a DHCP server in the
  services network plus EDA's `DHCPRelay` on the tenant gateways.
  - EDA's side worked: the relay selects VirtualNetwork-embedded IRBs by their own labels, and per-leaf IRB
    addresses give each leaf a unique relay address.
  - Every leaf relays the request, but the replies come back to a leaf only when the server's traffic lands on that
    leaf itself.
  - The switches do not answer on their own tenant gateway addresses when reached from the services VRF.
  - So the frontend keeps static addresses. A DHCP server with a leg inside each tenant network, or a Configlet, are
    the untested ways out.
- **When the Backend parks a port, the GPU host loses its default router but keeps its address.** A host agent must
  watch for the router, not the address.

## Not tested

A remote Terraform backend shared by a team; a merge-request mode (EDA four-eyes) for either tool; real GPU servers,
whose LLDP comes from the NIC firmware or the host and names the server rather than one VM per GPU (the tools would
then match LLDP system name + port description to the GPU); macOS as the operator machine.
