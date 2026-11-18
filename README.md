# Converged AI fabric on Nokia EDA

A complete GPU backend demo for [Nokia EDA](https://docs.eda.dev): one script builds a simulated AI-factory
fabric of eight Nokia 7220 IXR-H5-64D switches, sixteen GPUs, a storage island and a live Grafana topology
in an EDA namespace called `aifabric`. Everything runs inside EDA's own network simulator, so you need an
EDA, not hardware.

## What you get

- **Eight switches, one intent.** Two spines (`l2-1`, `l2-2`), four rail leaves (`l1-1`..`l1-4`, GPU0..GPU3 of every
  server) and two leaves for storage, CPU and management nodes (`l1-5`, `l1-6`), all SR Linux 26.7.1. One
  EDA AI Fabrics `Backend` (`manifests/backend.yaml`) builds the whole fabric: system addresses and ASNs,
  84 800G ISLs with eBGP, an EVPN overlay, one VRF per tenant and lossless RoCEv2 QoS (ECN + PFC).
- **Tenants book GPUs, not servers.** Four GPU servers with four GPUs each; every GPU has its own NIC on its own
  rail and runs as its own VM `gpu-<s>-g<G>`. A GPU belongs to a tenant through one label on its switch
  port (`tenant=1|2`); a GPU without the label is free and its port carries nothing.
- **Addresses without DHCP, gateways you can predict.** EDA gives every booked GPU port its own /64, computed from
  the stripe, the leaf and the port (no pool to manage): `fd00:100:<leaf>01:<port>0<channel>::/64`. The leaf is `::1`
  of it and advertises it, and the GPU NIC builds its own address from it (SLAAC, host part from the NIC's MAC). So
  every GPU's gateway is fixed: the same after a release and a new booking, and when it moves to the other tenant
  (the VRF changes, the address does not). Table below, under "GPU gateways".
- **A converged NIC.** VLAN 1 carries RDMA, VLAN 2 the frontend: per-tenant frontend VRFs plus a shared services
  network (storage, management cluster, a shared CPU node), with four QoS classes on every port.
- **An orchestrator with its own EDA identity.** `orchestrator.py` logs in as the EDA user `gpu-orchestrator`,
  which may change only Interface labels, and books, moves or releases GPUs in one EDA transaction each.
- **GPU hosts that follow their port.** Every GPU VM runs a small host agent: when EDA books its port it takes the
  port's /64 by SLAAC, on a move to the other tenant it keeps it, on a release it drops it. Nothing runs on the host
  for a booking.
- **Two ways to automate bookings, compared.** `automation/` holds a script on EDA's REST API and a Terraform project
  on Nokia's EDA providers, both checking each GPU's switch port by LLDP against EDA's inventory before changing
  anything. `TERRAFORM-VS-REST-API.md` explains both, how each works, the measurements and the pros and cons.
- **Live views.** A traffic matrix that pings every GPU from every GPU and flags any leak between tenants, and a
  Grafana topology with live traffic per cable direction and every GPU coloured by its tenant.

## What you need

**The EDA**

- Nokia EDA **26.8** (built and tested on 26.8.2) in simulation mode, with:
  - the **AI Fabrics** app (`aifabrics.eda.nokia.com`; shipped with 26.8.2);
  - the NodeProfile `srlinux-ghcr-26.7.1` in namespace `eda` (shipped with 26.8).
- The **Prometheus Exporter** app. If it is missing, the deploy installs it from EDA's built-in catalog.
- Room for the lab. Its 33 simulator pods request about 14 CPU and 30 GiB of memory, on top of EDA itself.
- Internet access from the cluster. It pulls `ghcr.io/nokia/srlinux:26.7.1-554`,
  `ghcr.io/srl-labs/network-multitool`, Prometheus, Grafana, and a Grafana plugin from grafana.com.

**Logins**

- The EDA URL or IP address, and an EDA admin user and password.
- Keycloak (EDA's login server) must still have its install default, `admin` / `admin`. The script needs it to
  read the secret of EDA's login client.
- A **kubeconfig** for the Kubernetes cluster EDA runs on, with cluster-admin rights. The script uses
  `kubectl` for everything EDA's API cannot do: configuring the network inside the simulated hosts, and running
  the dashboard's Prometheus and Grafana.

**Your machine** (Linux, or macOS with a newer bash)

- bash 4 or newer, `kubectl`, and python3 3.9 or newer (standard library only).
- `jq` and `sshpass`, for `aifabric-ssh`.
- Optional: Terraform 1.9 or newer, for `automation/terraform/` (it downloads Nokia's EDA providers from the
  Terraform registry).

## Deploy

```bash
git clone https://github.com/kkayhan/convergedaifabric.git
cd convergedaifabric
./deploy.sh
```

The script asks for the following. Enter takes the default shown in brackets.

1. The EDA URL or IP address, the one you open in a browser, e.g. `10.1.2.3` or `https://eda.example.com:9443`.
2. The EDA admin user and password. The password is hidden; Enter takes `admin`.
3. The kubeconfig file of EDA's Kubernetes cluster, and the context to use if the file holds several.

Then it runs a preflight and shows what it found:

- the EDA version on the Kubernetes side, and an API login as the admin;
- whether the kubeconfig and the URL point at the same EDA;
- the apps, the NodeProfile and the cluster capacity.

It changes nothing until you answer `yes`. A build from scratch takes about 9 minutes. It ends with two traffic
matrices, both with 0 violations, and prints the dashboard URL:

```
<EDA URL>/core/httpproxy/v1/aifabric-grafana/d/aifabric-topology/
```

The answers, but never the password, are saved in `.state/`. Later runs offer them as defaults, and the demo
tools below use them to reach the same EDA. Each run also writes a log to `.state/deploy-<time>.log`.

Running `./deploy.sh` again on a deployed lab updates it in place. Single phases, for example after a change or
a simulator pod restart:

```bash
./deploy.sh --phase hosts      # network config inside the simulated hosts (lost when their pods restart)
./deploy.sh --phase grafana    # dashboard, Prometheus, Grafana
./deploy.sh --phase verify     # Backend state, deviations, both traffic matrices
```

Phases: `ns topology backend qos frontend hosts orchestrator grafana verify`. `network` (the default) runs them
all. `topology` deletes and rebuilds the lab.

**Unattended runs.** Answers can also come from the environment:

- `EDA_URL`, `EDA_ADMIN_USER`, `EDA_ADMIN_PASSWORD`, `AIF_KUBECONFIG`, `AIF_CONTEXT`;
- `--yes` skips the confirmation;
- `KC_MASTER_USER` and `KC_MASTER_PASSWORD` override the Keycloak admin;
- `AIF_PROM_APP_VERSION` sets the exporter app version (default `v7.0.1`).

## What it creates on the EDA

- **Inside EDA namespace `aifabric`** (Kubernetes namespace `aifabric` too):
  - the topology (8 switches, 25 hosts) and the pools;
  - the Backend and its two tenant IPv6 pools;
  - the QoS policies and three Configlets;
  - the frontend VirtualNetworks;
  - the dashboard's Prometheus and Grafana.
- **Outside the namespace:**
  - EDA user and group `gpu-orchestrator`, with Role `aifabric:gpu-orchestrator` (plus user `gpu-terraform` in the
    same group once you run `automation/terraform/setup.py`);
  - in `eda-system`: HttpProxy `aifabric-grafana`, four `Export` objects `aifabric-*`, and TopologyGrouping
    `aifabric-spine-leaf`. The grouping makes EDA's topology page open on spine/leaf tiers, for every namespace.
  - the Prometheus Exporter app, only if it was missing.

## Remove

```bash
./destroy.sh
```

This deletes everything listed above except the Prometheus Exporter app, which others may use. It takes about a
minute. It may show one or two FAILED transactions in the EDA UI; that is harmless, because EDA retries the
deletes in the right order by itself.

## GPU gateways

Every GPU has its own NIC on its own rail leaf, and EDA (Backend `addressAllocation: EDAManagedIPv6`) gives that port a
fixed /64: `fd00:<stripe ID>00:<leaf index>01:<port>0<channel>::/64`, here stripe gpu1 = 1 and leaf index = rail.
The leaf holds `::1`, the GPU's gateway; the GPU's own address is that /64 plus a host part from its MAC (SLAAC).
The /64 exists on the switch only while the GPU is booked, but it is always the same one. Measured on all 16 ports.

| GPU | What | Leaf port | /64 | Gateway |
|---|---|---|---|---|
| `gpu-1-g0` | GPU0 of server gpu-1 | `l1-1` `ethernet-1/1/1` | `fd00:100:101:101::/64` | `fd00:100:101:101::1` |
| `gpu-1-g1` | GPU1 of server gpu-1 | `l1-2` `ethernet-1/1/1` | `fd00:100:201:101::/64` | `fd00:100:201:101::1` |
| `gpu-1-g2` | GPU2 of server gpu-1 | `l1-3` `ethernet-1/1/1` | `fd00:100:301:101::/64` | `fd00:100:301:101::1` |
| `gpu-1-g3` | GPU3 of server gpu-1 | `l1-4` `ethernet-1/1/1` | `fd00:100:401:101::/64` | `fd00:100:401:101::1` |
| `gpu-2-g0` | GPU0 of server gpu-2 | `l1-1` `ethernet-1/1/2` | `fd00:100:101:102::/64` | `fd00:100:101:102::1` |
| `gpu-2-g1` | GPU1 of server gpu-2 | `l1-2` `ethernet-1/1/2` | `fd00:100:201:102::/64` | `fd00:100:201:102::1` |
| `gpu-2-g2` | GPU2 of server gpu-2 | `l1-3` `ethernet-1/1/2` | `fd00:100:301:102::/64` | `fd00:100:301:102::1` |
| `gpu-2-g3` | GPU3 of server gpu-2 | `l1-4` `ethernet-1/1/2` | `fd00:100:401:102::/64` | `fd00:100:401:102::1` |
| `gpu-3-g0` | GPU0 of server gpu-3 | `l1-1` `ethernet-1/2/1` | `fd00:100:101:201::/64` | `fd00:100:101:201::1` |
| `gpu-3-g1` | GPU1 of server gpu-3 | `l1-2` `ethernet-1/2/1` | `fd00:100:201:201::/64` | `fd00:100:201:201::1` |
| `gpu-3-g2` | GPU2 of server gpu-3 | `l1-3` `ethernet-1/2/1` | `fd00:100:301:201::/64` | `fd00:100:301:201::1` |
| `gpu-3-g3` | GPU3 of server gpu-3 | `l1-4` `ethernet-1/2/1` | `fd00:100:401:201::/64` | `fd00:100:401:201::1` |
| `gpu-4-g0` | GPU0 of server gpu-4 | `l1-1` `ethernet-1/2/2` | `fd00:100:101:202::/64` | `fd00:100:101:202::1` |
| `gpu-4-g1` | GPU1 of server gpu-4 | `l1-2` `ethernet-1/2/2` | `fd00:100:201:202::/64` | `fd00:100:201:202::1` |
| `gpu-4-g2` | GPU2 of server gpu-4 | `l1-3` `ethernet-1/2/2` | `fd00:100:301:202::/64` | `fd00:100:301:202::1` |
| `gpu-4-g3` | GPU3 of server gpu-4 | `l1-4` `ethernet-1/2/2` | `fd00:100:401:202::/64` | `fd00:100:401:202::1` |

## Demo tools

All tools use the connection saved by `./deploy.sh`.

| Command | What it does |
|---|---|
| `python3 aifabric-traffic.py` | Live traffic matrices (leave it running on a second screen). **Backend:** 16 × 16 GPUs over VLAN 1; green = same tenant reachable, grey = other tenant isolated, red = a fault or a leak. Baseline: 60 reachable, 72 isolated, 0 violations. **Frontend:** VLAN 2, IPv4; baseline 348 / 72 / 0. |
| `python3 orchestrator.py show` | Every GPU: server, rail leaf port, tenant or free, backend VRF, SLAAC address (as the leaf learned it), frontend bridge domain. |
| `python3 orchestrator.py book tenant-1 gpu-3-g2 gpu-3-g3` | Books GPUs for a tenant: one dry-run, then one EDA transaction as user `gpu-orchestrator`. Then it re-addresses the GPU VMs. A GPU of the other tenant moves, a free one leaves the pool. |
| `python3 orchestrator.py release gpu-3-g2 gpu-3-g3` | Sends GPUs back to the free pool. |
| `python3 orchestrator.py baseline` | Back to the deploy-time booking: tenant-1 6 GPUs, tenant-2 6, free 4. |
| `python3 automation/gpu-tenant.py assign tenant-1 gpu-3-g2` | The automation script: books, moves (`--move`), releases (`unassign`) or applies `automation/tenancy.yaml`, after checking the GPU's port by LLDP; one dry run + one transaction, done when the GPU's NIC has its address. `list` shows every GPU. See `automation/README.md`. |
| `cd automation/terraform && python3 setup.py && terraform apply` | The same bookings as Terraform: edit `automation/tenancy.yaml`, plan (EDA dry run), apply. See `automation/README.md`. |
| `bash hosts.sh --follow` | Sets a GPU VM's frontend VLAN 2 address whenever its `tenant` label changes (VLAN 1 follows by itself: host agent). Keep it running when you book GPUs by any means. |
| `./aifabric-ssh` | Node access. `-l` lists every node with its role and live tenant; `l1-1` opens the SR Linux CLI (admin / NokiaSrl1!); `gpu-1-g0` opens a shell in a GPU VM. `-c 'CMD' NODE`, `--stripe gpu1`, `--tenant 2`, `--all-switch` run commands on one node or a group. |
| `python3 fail-spine.py l2-1 down` / `up` | Takes a spine out (one transaction disables its 42 ISLs) and brings it back. Cross-rail traffic moves to the other spine. |
| `python3 qos-path.py frontend 48` | Sends traffic of one QoS class and shows which queue it used on every hop (also `frontend 10`, `frontend 0`, `rdma 26`). |
| `manifests/gpu-ipv4.yaml` | Example of IPv4 on the GPU VLAN without SLAAC: one hand-written Configlet per port. Apply with `.state/kubectl apply -f manifests/gpu-ipv4.yaml && bash hosts.sh gpu-1-g0 gpu-2-g1 gpu-3-g0`. Delete it before releasing those GPUs. |

### Suggested walk-through

1. **One intent builds the fabric.** In the EDA UI, open Fabrics → AI Backends → `aifabric` → YAML. Two
   stripes, the spines, two tenants, EVPN, RoCEv2 QoS: that is the whole input for eight switches.
   `./aifabric-ssh -c 'show network-instance tenant-1 ipv6 route' l1-1` shows what EDA generated.
2. **Isolation per GPU.** Server gpu-2 has GPU0-1 in tenant-1 and GPU2-3 in tenant-2. In the matrix they cannot
   reach each other, although they sit in one server: with NVLink off their only path is the fabric, and the
   tenant VRFs close it.
3. **Booking through the API.** Run `orchestrator.py book tenant-1 gpu-3-g2`. Within about 10 seconds the
   dashboard tile turns tenant-1 blue, the matrix regroups, and the GPU has an address from tenant-1's pool,
   configured by itself.
4. **Losing a spine.** Run `fail-spine.py l2-1 down`. On the dashboard every l2-1 bundle goes grey, the l2-2
   bundles carry all the traffic, and the matrix stays green.
5. **QoS on the converged NIC.** Run `./aifabric-ssh -c 'info qos scheduler-policies' l1-1` and `qos-path.py`.
   Control traffic is strict priority (capped at 10 %), RDMA is lossless, and serving and bulk are weighted.
   A frontend packet marked AF31 never reaches the RDMA queue.

## Notes

- The simulator forwards real packets but never congests, so ECN, PFC and the WRR shares show in the
  configuration, not in the counters.
- Dynamic load balancing is off: SR Linux does not yet apply it to VXLAN traffic, so traffic between rails is
  ECMP-hashed.
- The network config of a simulated host lives in its pod. If a host pod restarts, run
  `./deploy.sh --phase hosts`.
