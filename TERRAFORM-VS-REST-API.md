# Managing GPU tenants by label: EDA REST API vs Terraform

## What is being managed

On this fabric a GPU belongs to a tenant through **one label on its switch port**: the EDA Interface of the GPU's
NIC carries `tenant=1` or `tenant=2`, or no `tenant` label when the GPU is free. Everything else follows that label
by itself:

- the aifabrics Backend puts the port into the tenant's VRF on VLAN 1 (backend RDMA);
- the frontend VirtualNetwork attaches VLAN 2 to the tenant's frontend;
- a free port carries nothing.

The port's IPv6 /64 is fixed per port (`addressAllocation: EDAManagedIPv6`), so a GPU's gateway is the same wherever
and whenever it is booked (table in `README.md`, "GPU gateways").

The tenants themselves (isolation groups, VRFs, frontends) are pre-configured. Giving GPUs to tenants, taking them
away, or growing a tenant is therefore nothing but adding, changing or removing that one label. This document
compares two ways to automate it, both built and measured on this lab:

| | EDA REST API (custom script) | Terraform |
|---|---|---|
| Tool | `automation/gpu-tenant.py` | `automation/terraform/` |
| The operator says | `gpu-tenant.py assign tenant-1 gpu-3-g2` | `gpu-3-g2: tenant-1` in `automation/tenancy.yaml`, then `terraform plan`, `terraform apply` |
| Talks to EDA through | EDA's Transaction API directly | Nokia's EDA Terraform providers (one per EDA app) |
| EDA identity | user `gpu-orchestrator` | user `gpu-terraform` |

## The same rules in both

Both tools enforce the same safety rules, so the comparison is about the tools, not about two designs.

- **The switch decides which port a GPU is on.** LLDP on the leaf port names the host behind it. EDA's inventory
  labels on that port (`gpu-server`, `rail`) must say the same. If they disagree, or the port has no LLDP neighbour,
  the change is refused before anything is sent to EDA, with both views printed. A crossed cable or a wrong topology
  entry is reported, never booked over.
- **Proof after the change.** IPv6 neighbour discovery cannot name a GPU (it carries a MAC and an address), and a
  free port has none (no subinterface, no router advertisement). So it is used as proof afterwards: the leaf has
  learned the NIC with the MAC that LLDP reported, with an address in the port's /64, in the tenant's VRF.
- **EDA dry run first.** EDA computes the exact switch changes before anything is committed.
- **Least privilege.** Both users are in the role `aifabric:gpu-orchestrator`: read the namespace, write Interfaces,
  nothing else.
- **Nothing to do on the GPU host.** Every GPU VM runs a small agent (`host-agent.sh`) that reacts only to what its
  port advertises: SLAAC address on a booking, kept on a move, dropped on a release. It needs no access to EDA. The
  frontend VLAN 2 address is still static and set by `hosts.sh --follow`.

## Approach 1: the EDA REST API (custom script)

**The idea.** The tool is an EDA API client written for this one job. It speaks the operator's language, sends
EDA exactly the change wanted, as one transaction, and finishes when the GPU really works. The desired state is
whatever the last command said; EDA itself is the only record.

**How `gpu-tenant.py` works**

1. Logs in to EDA's Keycloak as `gpu-orchestrator`. Two runs starting at the same instant collide there, so it
   retries with a random back-off.
2. Reads the live fabric through EDA's REST API:
   - the GPU ports (Interfaces with label `role=gpu`);
   - the LLDP neighbour of every port (EQL query);
   - the tenants, from the Backend's isolation groups.
3. Checks every GPU named in the command:
   - the GPU exists, the tenant exists, and the GPU's port agrees between LLDP and EDA's labels;
   - a GPU that belongs to another tenant may only be taken with `--move`.
4. Builds one JSON patch per port: add, change or remove the `tenant` label. Each patch starts with a `test` of the
   labels it just read (compare-and-swap): if someone changed that port in the meantime, EDA refuses the whole
   transaction.
5. Sends all patches as **one dry-run transaction**, then the same as **one real transaction**. All GPUs of the
   command change together, or none do.
6. Waits until the switches show the port in the tenant's VRF and the leaf has learned the GPU's NIC there. Then it
   re-reads the labels, to report a GPU that another operator changed during the run.

Commands: `list`, `assign <tenant> <gpu…>`, `unassign <gpu…>`, `apply tenancy.yaml` (make the fabric match the
file), plus `--dry-run`. Exit code 0 = done, 1 = EDA refused or someone else changed the GPU, 2 = refused before EDA.

## Approach 2: Terraform with Nokia's EDA providers

**The idea.** The desired state lives in a file, `tenancy.yaml`, one line per GPU. Terraform compares it with what
EDA has, shows the difference (`plan`) and makes EDA match it (`apply`). It also notices changes made by anyone
else, and puts the file's state back.

**How the Terraform project works**

1. `setup.py`, once per lab: creates the EDA user `gpu-terraform` and writes `terraform.tfvars` (EDA address and the
   client secret of EDA's login client). Terraform then never needs the Keycloak admin. It also runs
   `terraform init`, which downloads Nokia's providers.
2. `inventory.tf` reads the 16 GPU ports with Nokia's `interfaces-v1_interface_list` data source. That includes
   EDA's LLDP view of each port. It reads the tenants with the `aifabrics-v1_backend` data source, then runs the
   strict check. Any problem fails the plan before anything is sent.
3. `main.tf` puts each GPU port under Terraform as Nokia's typed resource `interfaces-v1_interface`:
   - **imported**, because the ports already exist (the topology made them);
   - Terraform manages **only the `tenant` label**; spec, status and other labels are left to EDA;
   - `prevent_destroy`, because destroying such a resource would delete the switch port's Interface.
   To release a GPU you set it to `free`; `terraform destroy` is refused on purpose.
4. `preview.tf` makes `terraform plan` show **EDA's own dry run** of exactly the planned label changes. Nokia's typed
   resources cannot dry-run, so this is a plain REST call, and Nokia's `core-v1` data sources read EDA's verdict.
5. `terraform apply`: the provider writes each changed port through the Interface app's REST API. **That is one EDA
   transaction per port.**
6. `checks.tf` reads the switches on every plan: is each booked port in its tenant VRF, and has the leaf learned the
   GPU's NIC? These checks only warn. Terraform does not wait for the fabric to converge.

## Measured on this lab (18 November 2026, EDA 26.8.2, Talos)

| | REST API script | Terraform |
|---|---|---|
| Book 1 free GPU | 6.5-8.8 s until the GPU's NIC is proven on its port | plan 5.3 s (incl. EDA dry run) + apply 5.3 s |
| Move a GPU to the other tenant | 4.3 s (the address stays) | plan 5.2 s + apply 5.3 s |
| Release a GPU | 3.2 s | plan 5.6 s + apply 5.4 s |
| Several GPUs in one go | 4 GPUs = one transaction, 6.5 s | 3 GPUs = 3 transactions (EDA bundled 2), apply 8.6 s ¹ |
| Back to the file's state | `apply tenancy.yaml`, one transaction, 3.2-3.7 s | plan + apply, one transaction per changed port |
| Unknown GPU or tenant, bad name | refused before EDA (exit 2) | the plan fails before EDA |
| Crossed cables: LLDP disagrees with EDA's labels | that GPU refused, both views shown; other GPUs still bookable | the **whole plan** refused while any GPU in the file is affected |
| Label changed outside the tool (GUI, kubectl) | `list` shows it; `apply --dry-run` shows the difference to the file; `apply` fixes it | the next plan shows the drift by itself; `apply` reverts it |
| Two operators at once | compare-and-swap refuses the later change; if EDA bundles both commits, the loser is detected afterwards and exits 1 | second `apply` in the same directory: "state lock" error; separate copies: no protection without a shared state backend |
| What EDA's Transactions page says | "gpu-tenant.py: assign to tenant-1: gpu-3-g2 tenant-1" + its dry run | the dry run is described; each commit only says "REST" |
| Code | 354 lines of Python + a 133-line shared login library | 251 lines of HCL in 8 files + a 57-line setup script |
| Needs | python3 3.9+, standard library only | Terraform (123 MiB) + 4 providers (89 MiB) from the Terraform registry; an air-gapped site needs a provider mirror |

¹ Measured with the earlier per-tenant pool addressing; the number of transactions does not depend on the addressing.

The GPU side is the same under both tools, because the host agent does not know who made the change. A booked GPU
is addressed within seconds and a moved one keeps its address. A released one drops its address within 5 s. The
traffic matrices showed 0 violations after every test.

## Pros and cons

### EDA REST API (custom script)

**Pros**

- Operator-friendly and immediate: one command per action, finished when the GPU works.
- Atomic: one transaction for all GPUs of a command, after one dry run.
- Safe with several operators without any state of its own: compare-and-swap on every port.
- Clear audit trail: every transaction says what it did and to which GPUs.
- Strictness per command: one miscabled GPU blocks only itself.
- Light: Python only, works on an air-gapped jump host as it is, and is easy to call from a GPU scheduler or a
  ticket workflow (meaningful exit codes).

**Cons**

- Imperative: the intent is only in the commands run. Drift goes unnoticed until someone compares against the file.
- Custom code to own: features such as retries, locking, a merge-request (four-eyes) mode, or a new EDA API version
  are ours to build and maintain.
- One gap in the compare-and-swap: two commits by the same EDA user in the same instant are bundled by EDA and both
  pass. The script reports it afterwards, but cannot prevent it.
- No review step enforced by the tool.

### Terraform

**Pros**

- Declarative and reviewable: the file is the intent, a pull request is the change request, and `plan` shows the
  change with EDA's own dry-run verdict.
- Drift detection built in: changes made in the GUI, by kubectl, by another tool or by an EDA revert show up in the
  next plan and are reverted by the next apply.
- Standard tooling with state and locking that fits CI/GitOps, using Nokia's own providers. The same tool can own the
  tenants too: VRFs, frontends, isolation groups.

**Cons**

- Not atomic: Nokia's providers call each app's REST API, not EDA's Transaction API (EDA 26.8 docs, limitation 1).
  Every port is its own transaction, an apply can stop half way, and the commits are labelled only "REST".
- Noisy plans: one label change shows about 45 lines of "(known after apply)" around the one line that matters.
- Needs careful wiring on objects it did not create:
  - import blocks and `ignore_changes`;
  - `prevent_destroy`, because a destroy would delete switch ports;
  - plain REST calls for the dry run and the switch checks;
  - a Keycloak login order;
  - `label_selector`, not the deprecated `labelselector`, which EDA ignores.
- Does not wait for the fabric; its checks only warn, on the next plan.
- Strictness per file: one miscabled GPU anywhere blocks every change.
- A state file to protect and share. Locking only works between people sharing that state, so a team needs a remote
  backend.
- Slower per change (plan + apply ≈ 10-11 s) and 212 MiB of binaries to install or mirror.

## When to use which

**GPU bookings are frequent, operational events**, usually triggered by a GPU scheduler or a self-service portal.
They must be atomic, quick and proven. That is the REST API's ground, as a tool like `gpu-tenant.py` or the same
logic inside the scheduler.

**Terraform fits a reviewed baseline that changes rarely**, such as a quarterly GPU allocation per tenant kept in
git, where drift correction is the point. It is also the natural owner of the tenants themselves.

Whichever you choose, **give the GPU labels one owner.** If Terraform owns them, every booking made by a script or
in the GUI is drift that the next `terraform apply` reverts.

## Found along the way (EDA 26.8.2)

- **Compare-and-swap works.** A JSON patch `test` on `/metadata/labels` with a stale value makes EDA refuse the whole
  transaction: HTTP 400 "test failed".
- **EDA bundles transactions** posted by the same user at the same moment. A bundled transaction cannot be reverted
  on its own.
- **Concurrent logins fail.** EDA's Keycloak refuses concurrent password logins of the same user (HTTP 400), so
  every client needs a retry.
- **DHCP for the frontend does not work here.** EDA's `DHCPRelay` on the tenant gateways, with a DHCP server in the
  route-leaked services network, fails: replies come back only through the leaf the server sits behind. So the
  frontend keeps static addresses.
- **A parked port withdraws the default router.** When the Backend parks a port, the GPU host loses its default
  router but keeps its address, so a host agent must watch the router.

## Not tested

- a shared remote Terraform backend;
- a merge-request (four-eyes) mode for either tool;
- real GPU servers, whose LLDP names the server and the NIC rather than one VM per GPU;
- macOS as the operator machine.
