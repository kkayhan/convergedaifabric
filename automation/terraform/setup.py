#!/usr/bin/env python3
"""Prepare the Terraform project: its own EDA user, the connection variables, terraform init.

  python3 setup.py           user gpu-terraform (group gpu-orchestrator = Role aifabric:gpu-orchestrator: read the
                             namespace, write Interfaces only) + terraform.tfvars (EDA URL, the client secret of
                             EDA's Keycloak client `eda`) + terraform init
  python3 setup.py remove    delete the user, the tfvars and the local Terraform state (the lab is gone or reset)

Run it once after ./deploy.sh (the group comes from its phase orchestrator). Uses the connection deploy.sh saved
(.state/) and the EDA admin login (EDA_ADMIN_USER / EDA_ADMIN_PASSWORD, default admin / admin); the Keycloak admin
(KC_MASTER_USER / KC_MASTER_PASSWORD, default admin / admin) is needed here only, to read the client secret once:
Terraform itself then logs in with that secret and never needs the Keycloak admin.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "lib"))
import eda_auth as eda  # noqa: E402

USER = PASSWORD = "gpu-terraform"
GROUP = "gpu-orchestrator"
TFVARS = HERE / "terraform.tfvars"


def main() -> int:
    url = eda.eda_url()
    secret = eda.eda_client_secret(url)
    a = eda.Admin(url, secret)
    if sys.argv[1:] == ["remove"]:
        u = a.find("users", "username", USER)
        if u:
            a.call("DELETE", f"/core/admin/users/{u['uuid']}")
            print(f"deleted EDA user {USER}")
        for p in [TFVARS, *HERE.glob("terraform.tfstate*"), *HERE.glob("*.tfplan")]:
            p.unlink(missing_ok=True)
        shutil.rmtree(HERE / ".terraform", ignore_errors=True)
        print("removed terraform.tfvars, the local state and .terraform/")
        return 0
    g = a.find("groups", "name", GROUP)
    if not g:
        sys.exit(f"EDA group {GROUP} missing: run ./deploy.sh (phase orchestrator) first")
    u = a.find("users", "username", USER)
    if not u:
        a.call("POST", "/core/admin/users", {"username": USER, "email": f"{USER}@example.com", "enabled": True,
                                             "firstName": "GPU", "lastName": "Terraform"})
        u = a.find("users", "username", USER)
    a.call("PUT", f"/core/admin/groups/{g['uuid']}/users/{u['uuid']}")
    a.call("PUT", f"/core/admin/users/{u['uuid']}/resetpassword", {"value": PASSWORD, "temporary": False})
    st, tok = eda.user_token(url, secret, USER, PASSWORD)
    if st != 200:
        sys.exit(f"login as {USER} failed: HTTP {st} {tok}")
    print(f"EDA user {USER} in group {GROUP} (Role aifabric:gpu-orchestrator), login OK")
    TFVARS.write_text(f'eda_url           = "{url}"\neda_client_secret = "{secret}"\n')
    TFVARS.chmod(0o600)
    print(f"wrote {TFVARS.name} (EDA URL + client secret; git-ignored)")
    tf = shutil.which("terraform") or sys.exit("terraform not found in PATH (Terraform 1.9 or newer)")
    return subprocess.run([tf, "init", "-input=false", "-no-color"], cwd=HERE).returncode


if __name__ == "__main__":
    sys.exit(main())
