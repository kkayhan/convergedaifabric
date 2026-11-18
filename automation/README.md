# Automating GPU bookings: a script and Terraform

Two ways to give GPUs to tenants on this fabric, built side by side. Both change one thing: the `tenant` label on
each GPU's switch port (EDA Interface). `COMPARISON.md` has the measurements and the pros and cons of each.

Both find a GPU's port from the switches (LLDP) and refuse any change when EDA's inventory labels on that port
disagree, which catches crossed cables and wrong topology data. Both do an EDA dry run first and log in as their own
limited EDA user, which may only change Interfaces. The GPU VMs handle their side themselves: each runs a host agent
(`host-agent.sh`, installed by `./deploy.sh`) that takes the tenant's /64 by SLAAC when its port is booked and drops
it when released. Keep `bash hosts.sh --follow` running in a terminal for the frontend VLAN 2 address.

GPU names: `gpu-<server>-g<index>`, e.g. `gpu-2-g0` = GPU0 of server gpu-2 (its NIC sits on rail 1, leaf l1-1).
`gpu-2` alone = all four GPUs of that server. Tenants: `tenant-1`, `tenant-2`, or `free`.

## The script: `gpu-tenant.py`

Python 3.9+, standard library only. It uses the connection `./deploy.sh` saved and the EDA user `gpu-orchestrator`.

```bash
python3 automation/gpu-tenant.py list                          # every GPU: port, LLDP check, tenant, VRF, address
python3 automation/gpu-tenant.py assign tenant-1 gpu-3-g2       # book (a GPU of the other tenant needs --move)
python3 automation/gpu-tenant.py unassign gpu-3-g2              # back to the free pool
python3 automation/gpu-tenant.py apply automation/tenancy.yaml  # make the fabric match the file
```

Add `--dry-run` to see EDA's verdict without committing, or `--no-wait` to return right after the commit. Every
command is one EDA dry run, then one transaction for all its GPUs. It then waits until the switches have the port in
the tenant's VRF and the GPU's NIC has an address from the tenant's /64. Exit codes: 0 done, 1 EDA refused or someone
else changed the same GPU meanwhile, 2 refused before EDA (bad input, or inventory and LLDP disagree).

## Terraform: `automation/terraform/`

Terraform 1.9 or newer, with Nokia's EDA providers from the Terraform registry. It uses the EDA user `gpu-terraform`.

```bash
cd automation/terraform
python3 setup.py          # once after ./deploy.sh: creates the user, writes terraform.tfvars, runs terraform init
terraform plan            # shows the label changes, EDA's dry-run verdict and the switches touched
terraform apply
```

The desired state is `../tenancy.yaml`, the same file the script's `apply` reads: edit the line of a GPU, plan,
apply. The first plan imports the 16 GPU ports; it never creates or deletes one. `terraform destroy` is refused on
purpose, because destroying a resource here would delete a switch port's Interface. To release GPUs, set them to
`free`. `python3 setup.py remove` deletes the user and the local state (`./destroy.sh` runs it).

Terraform reports changes made outside it, in the EDA GUI, by kubectl or by the script, as drift, and the next apply
puts the file's state back. Give the GPU labels one owner.
