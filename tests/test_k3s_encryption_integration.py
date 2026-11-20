"""Boot real K3s servers with the deployment role's encryption drop-in."""

import json
import os
import shutil
import subprocess
import time
from uuid import uuid4

import pytest

from test_role_safety import load, render_k3s_encryption_config


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=90)
    if check and result.returncode:
        pytest.fail(f"Docker {' '.join(args[:2])} failed\n{result.stderr}")
    return result


def wait_for_encryption_status(server):
    # Node registration can precede K3s publishing the encryption hash.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = docker(
            "exec", server, "k3s", "secrets-encrypt", "status", "--output", "json", check=False,
        )
        if result.returncode == 0:
            status = json.loads(result.stdout)
            if status.get("hashmatch") is True:
                return status
        time.sleep(2)
    logs = docker("logs", "--tail=40", server, check=False)
    pytest.fail(
        f"{server} did not report matching encryption hashes\n"
        f"{result.stdout}{result.stderr}\n{logs.stderr}"
    )


def test_new_k3s_ha_cluster_uses_secretbox(tmp_path):
    if not shutil.which("docker") or docker("info", check=False).returncode:
        if os.environ.get("STARTUP_REQUIRE_DOCKER_TESTS") == "1":
            pytest.fail("Docker is required for K3s encryption integration in CI")
        pytest.skip("Docker is required for K3s encryption integration")

    version = load("roles/k3s/defaults/main.yml")["k3s_version"]
    image = f"rancher/k3s:{version.replace('+', '-')}"
    network = f"secretbox-{uuid4().hex[:10]}"
    servers = [f"{network}-{i}" for i in range(3)]
    docker("network", "create", network)
    try:
        # GIVEN a fresh etcd cluster and the production role's server config,
        for i, server in enumerate(servers):
            # WHEN each joining server uses the first server's real status output,
            status = wait_for_encryption_status(servers[0]) if i else None
            config = render_k3s_encryption_config(status)
            assert config is not None
            config_dir = tmp_path / server
            config_dir.mkdir()
            (config_dir / "00-startup-secret-encryption.yaml").write_text(config)
            join = ["--cluster-init"] if i == 0 else ["--server", f"https://{servers[0]}:6443"]
            docker(
                "run", "--detach", "--privileged", "--name", server, "--network", network,
                "--volume", f"{config_dir}:/etc/rancher/k3s/config.yaml.d:ro",
                "--env", f"K3S_TOKEN={network}", image, "server", *join,
                "--node-name", server, "--snapshotter=native",
                "--disable=coredns,traefik,servicelb,metrics-server,local-storage",
            )
            wait_for_encryption_status(server)

        # THEN every server agrees on the provider and can read a Secret written by a peer.
        docker("exec", servers[-1], "kubectl", "create", "secret", "generic",
               "encryption-check", "--from-literal=check=ok")
        for server in servers:
            status = wait_for_encryption_status(server)
            assert status["enable"]
            assert status["activekey"].startswith("XSalsa20-POLY1305 ")
            secret = docker("exec", server, "kubectl", "get", "secret",
                            "encryption-check", "-o", "json")
            assert json.loads(secret.stdout)["data"] == {"check": "b2s="}
    finally:
        docker("rm", "--force", "--volumes", *servers, check=False)
        docker("network", "rm", network)
