#!/usr/bin/env python3
"""EDA login + kubectl helpers for the aifabric tools (orchestrator.py, fail-spine.py, deploy.sh preflight).

Which EDA and which Kubernetes cluster: env EDA_URL / KUBECTL first, else what deploy.sh saved in .state/
(connection.env + the kubectl wrapper pinned to the kubeconfig and context the person chose), else plain kubectl.

EDA REST login = password grant through EDA's confidential Keycloak client `eda`. Its secret is read with the
Keycloak master admin (env KC_MASTER_USER / KC_MASTER_PASSWORD, default admin / admin, the EDA install default).
Admin actions run as EDA_ADMIN_USER / EDA_ADMIN_PASSWORD (default admin / admin).

  python3 lib/eda_auth.py check     log in as the EDA admin, print the EDA user count (deploy.sh preflight)
"""
from __future__ import annotations

import json
import os
import shlex
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Sequence

sys.dont_write_bytecode = True
STATE = Path(__file__).resolve().parent.parent / ".state"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE          # EDA ships a self-signed certificate


def saved(key: str) -> str:
    """A value deploy.sh saved in .state/connection.env ("" when absent)."""
    f = STATE / "connection.env"
    if f.is_file():
        for line in f.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key:
                return shlex.split(v)[0] if v.strip() else ""
    return ""


def eda_url(required: bool = True) -> str:
    url = (os.environ.get("EDA_URL") or saved("EDA_URL")).rstrip("/")
    if not url and required:
        sys.exit("EDA address unknown: run ./deploy.sh first (it saves the connection) or set EDA_URL")
    return url


def kubectl_args() -> list[str]:
    if os.environ.get("KUBECTL"):
        return shlex.split(os.environ["KUBECTL"])
    if os.access(STATE / "kubectl", os.X_OK):
        return [str(STATE / "kubectl")]
    return ["kubectl"]


def http(method: str, url: str, headers: dict | None = None, data: bytes | None = None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def eda_client_secret(url: str) -> str:
    """Secret of the confidential Keycloak client `eda` (realm eda), via the Keycloak master admin."""
    try:
        st, b = http("POST", f"{url}/core/proxy/v1/identity/realms/master/protocol/openid-connect/token", FORM,
                     urllib.parse.urlencode({"client_id": "admin-cli", "grant_type": "password",
                                             "username": os.environ.get("KC_MASTER_USER", "admin"),
                                             "password": os.environ.get("KC_MASTER_PASSWORD", "admin")}).encode())
    except urllib.error.URLError as e:
        raise RuntimeError(f"EDA not reachable at {url}: {e.reason}") from None
    if st != 200:
        raise RuntimeError(f"Keycloak master login failed: HTTP {st} (set KC_MASTER_USER / KC_MASTER_PASSWORD "
                           "if the Keycloak admin is not admin / admin)")
    mt = json.loads(b)["access_token"]
    st, b = http("GET", f"{url}/core/proxy/v1/identity/admin/realms/eda/clients?clientId=eda",
                 {"Authorization": f"Bearer {mt}"})
    if st != 200 or not json.loads(b):
        raise RuntimeError(f"Keycloak client 'eda' not readable: HTTP {st}")
    return json.loads(b)[0]["secret"]


def user_token(url: str, secret: str, user: str, password: str) -> tuple[int, str]:
    """(HTTP status, access token or error text) for a password grant through client `eda`."""
    st, b = http("POST", f"{url}/core/proxy/v1/identity/realms/eda/protocol/openid-connect/token", FORM,
                 urllib.parse.urlencode({"client_id": "eda", "client_secret": secret, "grant_type": "password",
                                         "scope": "openid", "username": user, "password": password}).encode())
    try:
        j = json.loads(b)
    except ValueError:
        return st, b[:200]
    return st, j.get("access_token") or j.get("error_description") or j.get("error") or b[:200]


class Admin:
    """EDA admin API (/core/admin/...) as the EDA admin user."""

    def __init__(self, url: str, secret: str):
        self.url = url
        st, tok = user_token(url, secret, os.environ.get("EDA_ADMIN_USER", "admin"),
                             os.environ.get("EDA_ADMIN_PASSWORD", "admin"))
        if st != 200:
            raise RuntimeError(f"EDA admin login failed: HTTP {st} {tok}")
        self.h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}

    def call(self, method: str, path: str, body=None, ok=(200, 201, 204)):
        st, b = http(method, self.url + path, self.h, None if body is None else json.dumps(body).encode())
        if st not in ok:
            raise RuntimeError(f"{method} {path} -> HTTP {st} {b[:300]}")
        return json.loads(b) if b.strip() else None

    def find(self, kind: str, key: str, value: str) -> dict | None:
        return next((x for x in self.call("GET", f"/core/admin/{kind}") or [] if x.get(key) == value), None)


def _run(args: Sequence[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), check=check, text=True, capture_output=True)


def toolbox(K: Sequence[str]) -> str:
    pod = _run(list(K) + ["-n", "eda-system", "get", "pods", "-l", "eda.nokia.com/app=eda-toolbox",
                          "--field-selector=status.phase=Running",
                          "-o", "jsonpath={.items[0].metadata.name}"]).stdout.strip()
    if not pod:
        raise RuntimeError("no Running eda-toolbox pod in eda-system")
    return pod


def edactl(K: Sequence[str], verb: str, path: Path, log: Callable[[str], None]) -> None:
    """edactl apply|delete -f <file> inside the toolbox = an EDA transaction (a plain kubectl write of
    an EDA user Role does not reach EDA's own store)."""
    tb = toolbox(K)
    _run(list(K) + ["-n", "eda-system", "cp", str(path), f"{tb}:/tmp/{path.name}"])
    p = _run(list(K) + ["-n", "eda-system", "exec", tb, "--", "edactl", verb, "-f", f"/tmp/{path.name}"],
             check=False)
    out = (p.stdout + p.stderr).strip().replace("\n", " | ")
    if p.returncode != 0:
        raise RuntimeError(f"edactl {verb} {path.name} failed: {out[:400]}")
    log(f"edactl {verb} {path.name}: {out[:200]}")


def role_exists(K: Sequence[str], kind: str, ns: str, name: str) -> bool:
    return _run(list(K) + ["-n", ns, "get", f"{kind}.core.eda.nokia.com", name], check=False).returncode == 0


def check() -> int:
    url = eda_url()
    try:
        a = Admin(url, eda_client_secret(url))
        users = a.call("GET", "/core/admin/users") or []
    except RuntimeError as e:
        print(f"  EDA API {url}: {e}")
        return 1
    print(f"  EDA API {url}: login as {os.environ.get('EDA_ADMIN_USER', 'admin')} OK ({len(users)} EDA users)")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["check"]:
        sys.exit(check())
    sys.exit(__doc__)
