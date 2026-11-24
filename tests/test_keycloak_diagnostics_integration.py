"""Exercise the actual read-only role entry point with a local kubectl boundary."""
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_status_tag_reports_acme_failures_without_deploying_or_exposing_configuration(tmp_path):
    # GIVEN a cluster boundary returning a pending certificate and sensitive pod config.
    boundary = tmp_path / "bin"
    boundary.mkdir()
    kubectl = boundary / "kubectl"
    kubectl.write_text("#!" + sys.executable + "\n" + '''import json, os, sys
from pathlib import Path
if sys.argv[1:] == ["get", "services", "--namespace", "kube-system", "--selector=app.kubernetes.io/name=traefik", "--output=json"]:
    print(json.dumps({"items": [{"metadata": {"name": "traefik"}, "spec": {"ports": [{"name": "web", "nodePort": 31380}]}}]}))
    raise SystemExit(0)
if sys.argv[1:] == ["get", "ingressclasses", "--output=json"]:
    print(json.dumps({"items": [{"metadata": {"name": "test-traefik"}, "spec": {"controller": "traefik.io/ingress-controller"}}]}))
    raise SystemExit(0)
if sys.argv[1:] == ["get", "deployments", "--namespace", "kube-system", "--selector=app.kubernetes.io/name=traefik", "--output=json"]:
    print(json.dumps({"items": [{"spec": {"template": {"spec": {"containers": [{"name": "traefik", "image": "traefik:test", "args": ["--providers.kubernetesingress.namespaces=legacy", "--other=MUST_NOT_APPEAR"]}]}}}}]}))
    raise SystemExit(0)
assert sys.argv[1:] == ["get", "deployments,pods,ingresses,certificates,certificaterequests,orders,challenges", "--namespace", "authentication", "--output=json"]
Path(os.environ["CALL_RECORD"]).write_text("read-only status")
print(json.dumps({"items": [{"kind": "Challenge", "metadata": {"name": "auth-tls-test"},
    "spec": {"sensitive_fixture": "MUST_NOT_APPEAR"},
    "status": {"state": "pending", "reason": "HTTP-01 self-check returned 404"}}]}))
''')
    kubectl.chmod(0o755)
    ss = boundary / "ss"
    ss.write_text("#!/bin/sh\nprintf 'LISTEN 0 128 0.0.0.0:80 0.0.0.0:* users:((external-proxy))\\n'\n")
    ss.chmod(0o755)
    playbook = tmp_path / "status.yml"
    playbook.write_text(yaml.safe_dump([{"hosts": "localhost", "connection": "local",
                                       "gather_facts": False, "roles": ["keycloak"]}]))
    # WHEN an operator selects the shipped status tag rather than a deployment.
    result = subprocess.run(["ansible-playbook", str(playbook), "-i", "localhost,",
                             "--tags", "keycloak-status"], capture_output=True, text=True,
                            env={**os.environ, "PATH": str(boundary) + os.pathsep + os.environ["PATH"],
                                 "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
                                 "ANSIBLE_LOCAL_TEMP": str(tmp_path / "controller"),
                                 "ANSIBLE_REMOTE_TEMP": str(tmp_path / "remote"),
                                 "CALL_RECORD": str(tmp_path / "calls")}, timeout=60)
    # THEN only the read-only boundary was called and useful failure context is visible.
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "calls").read_text() == "read-only status"
    assert "HTTP-01 self-check returned 404" in result.stdout
    assert "test-traefik" in result.stdout
    assert "--providers.kubernetesingress.namespaces=legacy" in result.stdout
    assert "external-proxy" in result.stdout
    assert "31380" in result.stdout
    assert "MUST_NOT_APPEAR" not in result.stdout + result.stderr
    assert "changed=0" in result.stdout
