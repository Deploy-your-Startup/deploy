"""Production-mode Keycloak, Postgres and the rendered OAuth2 Proxy in a real browser."""
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template
import pytest
import yaml

from test_keycloak_project_integration import PROVIDER, resource

ROOT = Path(__file__).resolve().parents[1]


def trusted(value):
    if isinstance(value, str):
        return trust_as_template(value)
    if isinstance(value, dict):
        return {key: trusted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [trusted(item) for item in value]
    return value


def render(path, values):
    return yaml.safe_load(Templar(loader=DataLoader(), variables=trusted(values)).template(
        trust_as_template((ROOT / path).read_text())))


def request(url, data=None, token=None, form=False, method=None):
    payload = None if data is None else (
        urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode())
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    # The endpoint is a loopback-only disposable container.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, payload, headers, method=method), timeout=10) as response:
        return json.loads(response.read() or "null")


@pytest.fixture(params=["ingress", "external_proxy"])
def tls_termination(request):
    return request.param


@pytest.fixture
def stack(tmp_path, tls_termination):
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        if os.environ.get("STARTUP_REQUIRE_DOCKER_TESTS") == "1":
            pytest.fail("Docker is required for the real Keycloak integration")
        pytest.skip("Docker is required for the real Keycloak integration")
    name = "keycloak-check-" + uuid.uuid4().hex[:10]
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    admin_password, db_password = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    environment = {
        **os.environ, "POSTGRES_PASSWORD": db_password,
        "KC_BOOTSTRAP_ADMIN_USERNAME": "startup-admin",
        "KC_BOOTSTRAP_ADMIN_PASSWORD": admin_password,
        "KC_DB_PASSWORD": db_password,
    }
    created = []

    def docker(*args, check=True):
        return subprocess.run(["docker", *args], env=environment, text=True,
                              capture_output=True, check=check, timeout=180)

    def mapped(container, container_port):
        return docker("port", container, f"{container_port}/tcp").stdout.strip().rsplit(":", 1)[1]

    try:
        docker("network", "create", name)
        db = name + "-db"
        docker("run", "-d", "--name", db, "--network", name, "--network-alias", "postgres",
               "-e", "POSTGRES_USER=keycloak", "-e", "POSTGRES_PASSWORD", "postgres:18.6")
        created.append(db)
        for _ in range(60):
            if docker("exec", db, "pg_isready", "-h", "127.0.0.1", "-U", "keycloak", check=False).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("The authentication database did not start")
        defaults = yaml.safe_load((ROOT / "roles/keycloak/defaults/main.yml").read_text())
        values = {**defaults, "keycloak_hostname": f"auth.example.test:{port}",
                  "keycloak_tls_termination": tls_termination}
        deployment = render("roles/keycloak/templates/deployment.yml", values)
        spec = deployment["spec"]["template"]["spec"]["containers"][0]
        kc = name + "-server"
        args = ["run", "-d", "--name", kc, "--network", name, "--network-alias", "keycloak",
                "--memory", spec["resources"]["limits"]["memory"].replace("Mi", "m"),
                "-p", "127.0.0.1::8080"]
        for item in spec["env"]:
            if "value" in item:
                args += ["-e", item["name"] + "=" + str(item["value"])]
            else:
                args += ["-e", item["name"]]
        docker(*args, spec["image"], *spec["args"])
        created.append(kc)
        api = "http://127.0.0.1:" + mapped(kc, 8080)
        for _ in range(600):
            try:
                discovery = request(api + "/realms/master/.well-known/openid-configuration")
                break
            except (OSError, urllib.error.URLError):
                time.sleep(1)
        else:
            state = docker("inspect", "--format", "{{json .State}}", kc, check=False).stdout
            pytest.fail("The rendered production Keycloak did not start: " + state)
        assert discovery["issuer"] == f"https://auth.example.test:{port}/realms/master"
        auth_upstream = "keycloak:8080"
        if tls_termination == "external_proxy":
            # Exercise the shipped middleware in real Traefik behind a TLS edge.
            # The intermediate HTTP hop must not turn Keycloak's public origin insecure.
            middleware = render("roles/keycloak/templates/external-tls-middleware.yml", values)
            ingress = render("roles/keycloak/templates/ingress.yml", values)
            assert ingress["spec"]["tls"] == []
            assert ingress["metadata"]["annotations"]["cert-manager.io/issuer"] == ""
            (tmp_path / "ingress-proxy.yml").write_text(yaml.safe_dump({"http": {
                "routers": {"auth": {"rule": "Host(`auth.example.test`)", "entryPoints": ["web"],
                                      "middlewares": ["public-origin"], "service": "keycloak"}},
                "middlewares": {"public-origin": middleware["spec"]},
                "services": {"keycloak": {"loadBalancer": {"servers": [{"url": "http://keycloak:8080"}]}}},
            }}))
            ingress_proxy = name + "-ingress"
            docker("run", "-d", "--name", ingress_proxy, "--network", name,
                   "--network-alias", "cluster-ingress", "-v", f"{tmp_path}:/test:ro",
                   "-e", "NO_PROXY=keycloak,postgres,localhost,127.0.0.1",
                   "rancher/mirrored-library-traefik:3.7.13", "--entrypoints.web.address=:8080",
                   "--providers.file.filename=/test/ingress-proxy.yml")
            created.append(ingress_proxy)
            auth_upstream = "cluster-ingress:8080"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(tmp_path / "key.pem"), "-out", str(tmp_path / "cert.pem"),
            "-days", "1", "-subj", "/CN=auth.example.test",
            "-addext", "subjectAltName=DNS:auth.example.test,DNS:app.example.test",
        ], check=True, capture_output=True)
        # OAuth2 Proxy runs as an unprivileged user. Only the public test
        # certificate is shared with it; the private key stays in the gateway.
        (tmp_path / "cert.pem").chmod(0o644)
        (tmp_path / "nginx.conf").write_text(f"""
events {{}}
http {{
  resolver 127.0.0.11 valid=1s;
  # OAuth2 Proxy may split its encrypted OIDC session across several cookies.
  # The test gateway must accept those headers, as the production Traefik does.
  proxy_buffer_size 32k;
  proxy_buffers 8 32k;
  server {{
    listen {port} ssl;
    server_name auth.example.test;
    ssl_certificate /test/cert.pem;
    ssl_certificate_key /test/key.pem;
    location / {{
      set $backend http://{auth_upstream};
      proxy_pass $backend;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Host $http_host;
      proxy_set_header X-Forwarded-Port {port};
      proxy_set_header X-Forwarded-For $remote_addr;
    }}
  }}
  server {{
    listen {port} ssl;
    server_name app.example.test;
    ssl_certificate /test/cert.pem;
    ssl_certificate_key /test/key.pem;
    location / {{
      set $backend http://oauth2-proxy:4180;
      proxy_pass $backend;
      proxy_set_header Host $http_host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header X-Forwarded-Host $http_host;
      proxy_set_header X-Forwarded-For $remote_addr;
    }}
  }}
}}
""")
        gateway = name + "-gateway"
        docker("run", "-d", "--name", gateway, "--network", name,
               "--network-alias", "auth.example.test", "--network-alias", "app.example.test",
               "-p", f"127.0.0.1:{port}:{port}",
               "-v", f"{tmp_path}:/test:ro",
               "-v", f"{tmp_path}/nginx.conf:/etc/nginx/nginx.conf:ro",
               "nginx:1.28.0")
        created.append(gateway)
        yield {
            "api": api, "port": port, "docker": docker, "created": created,
            "name": name, "environment": environment, "temporary": tmp_path,
            "password": admin_password, "database": db,
        }
    finally:
        for container in reversed(created):
            docker("rm", "-f", "-v", container, check=False)
        docker("network", "rm", name, check=False)


def configure(stack, realm, client_secret, *, session_settings=None, check=True):
    port = int(stack["api"].rsplit(":", 1)[1])
    encode = lambda value: base64.b64encode(value.encode()).decode()
    variables = {
        "project_name": realm, "k8s_namespace": realm, "keycloak_namespace": "authentication",
        "keycloak_realm": realm, "keycloak_client_id": realm,
        "keycloak_client_origin": f"https://app.example.test:{stack['port']}",
        "keycloak_client_service": {"resources": [{"spec": {"clusterIP": "127.0.0.1", "ports": [{"port": port}]}}]},
        "keycloak_client_admin": {"resources": [{"data": {
            "admin_username": encode("startup-admin"), "admin_password": encode(stack["password"]),
        }}]},
        "keycloak_client_credentials": {"resources": [{"data": {
            "OAUTH2_PROXY_CLIENT_SECRET": encode(client_secret),
        }}]},
    }
    if session_settings is not None:
        variables["keycloak_realm_session_settings"] = session_settings
    playbook = stack["temporary"] / "configure.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": variables,
        "tasks": [{"ansible.builtin.import_tasks": str(ROOT / "roles/keycloak-client/tasks" / phase)}
                  for phase in ("preflight.yml", "configure.yml")],
    }]))
    playbook.chmod(0o600)
    result = subprocess.run(
        [str(Path(sys.executable).parent / "ansible-playbook"), "-i", "localhost,", str(playbook)],
        env={**os.environ, "ANSIBLE_LOCAL_TEMP": str(stack["temporary"] / "ansible"),
             "ANSIBLE_REMOTE_TEMP": str(stack["temporary"] / "ansible-remote"),
             "NO_PROXY": "127.0.0.1,localhost"},
        capture_output=True, text=True, timeout=90,
    )
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.mark.parametrize("tls_termination", ["ingress"])
def test_deployment_manages_only_declared_realm_session_settings(stack):
    # GIVEN the shipped role and an explicit, startup-owned session policy.
    client_secret = secrets.token_urlsafe(36)
    policy = {
        "ssoSessionIdleTimeout": 604800,
        "ssoSessionMaxLifespan": 2592000,
        "clientSessionIdleTimeout": 0,
        "clientSessionMaxLifespan": 0,
        "ssoSessionIdleTimeoutRememberMe": 0,
        "ssoSessionMaxLifespanRememberMe": 0,
    }
    def admin_token():
        # Each check gets a fresh token; deployment may outlast its short TTL.
        return request(stack["api"] + "/realms/master/protocol/openid-connect/token", {
            "grant_type": "password", "client_id": "admin-cli",
            "username": "startup-admin", "password": stack["password"],
        }, form=True)["access_token"]

    realm_url = stack["api"] + "/admin/realms/journal"
    master_url = stack["api"] + "/admin/realms/master"
    master_before = request(master_url, token=admin_token())

    # WHEN the startup is deployed for the first time.
    configure(stack, "journal", client_secret, session_settings=policy)
    # THEN Keycloak contains the configured policy, including inheritance values.
    first = request(realm_url, token=admin_token())
    assert {key: first[key] for key in policy} == policy

    # GIVEN console edits to both a managed timeout and unrelated realm settings.
    request(realm_url, {
        "ssoSessionIdleTimeout": 120, "registrationAllowed": True,
        "displayName": "Operator-owned journal", "accessTokenLifespan": 240,
    }, admin_token(), method="PUT")
    # WHEN the same deployment is applied again.
    configure(stack, "journal", client_secret, session_settings=policy)
    # THEN it corrects only declared session fields and leaves the master alone.
    corrected = request(realm_url, token=admin_token())
    assert {key: corrected[key] for key in policy} == policy
    assert corrected["registrationAllowed"] is True
    assert corrected["displayName"] == "Operator-owned journal"
    assert corrected["accessTokenLifespan"] == 240
    assert request(master_url, token=admin_token()) == master_before

    # WHEN the policy is reapplied without drift, THEN no update is reported.
    repeated = configure(stack, "journal", client_secret, session_settings=policy)
    assert "changed=0" in repeated.stdout
    assert request(realm_url, token=admin_token()) == corrected

    # WHEN management is omitted, THEN the existing policy remains untouched.
    configure(stack, "journal", client_secret)
    assert request(realm_url, token=admin_token()) == corrected


@pytest.mark.parametrize("settings", [
    {"registrationAllowed": True}, {"ssoSessionIdleTimeout": "7d"},
    {"ssoSessionIdleTimeout": 0}, {"ssoSessionMaxLifespan": -1},
    {"ssoSessionIdleTimeout": True}, [604800],
])
def test_invalid_session_policy_is_rejected_before_contacting_keycloak(tmp_path, settings):
    # GIVEN invalid deployment configuration and no Keycloak server.
    stack = {"api": "http://127.0.0.1:1", "port": 443,
             "password": "unused-local-test", "temporary": tmp_path}
    # WHEN the actual deployment tasks run.
    result = configure(stack, "journal", "unused-local-test",
                       session_settings=settings, check=False)
    # THEN validation stops before credentials or realm data are requested.
    assert result.returncode != 0
    assert "Authenticate the cluster automation account" not in result.stdout
    assert "session" in result.stdout.lower()


def provision_startups(stack, client_secrets):
    directory = stack["temporary"]
    modules = directory / "collections/ansible_collections/kubernetes/core/plugins/modules"
    modules.mkdir(parents=True, exist_ok=True)
    for name in ("k8s", "k8s_info"):
        (modules / f"{name}.py").write_text(PROVIDER)

    def encode(value):
        return base64.b64encode(value.encode()).decode()

    state = directory / "kubernetes.json"
    if not state.exists():
        port = int(stack["api"].rsplit(":", 1)[1])
        resources = {
            "Service/authentication/keycloak": resource(
                "Service", "keycloak", "authentication",
                spec={"clusterIP": "127.0.0.1", "ports": [{"port": port}]},
            ),
            "Secret/authentication/keycloak": resource(
                "Secret", "keycloak", "authentication",
                data={"admin_username": encode("startup-admin"), "admin_password": encode(stack["password"])},
            ),
        }
        for realm, secret in client_secrets.items():
            namespace = "app-" + realm
            resources[f"Namespace//{namespace}"] = resource("Namespace", namespace)
            resources[f"Secret/{namespace}/keycloak-client"] = {
                "kind": "Secret",
                "metadata": {"name": "keycloak-client", "namespace": namespace,
                             "annotations": {"startup.deploy/realm": realm, "startup.deploy/client": realm}},
                "data": {"OAUTH2_PROXY_CLIENT_SECRET": encode(secret)},
            }
        state.write_text(json.dumps(resources))
        state.chmod(0o600)
    playbook = directory / "provision.yml"
    playbook.write_text(yaml.safe_dump([
        {
            "name": f"Deploy startup {realm}",
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {
                "startup_cluster_managed": False,
                "project_name": realm,
                "k8s_namespace": "app-" + realm,
                "keycloak_client_origin": f"https://app.example.test:{stack['port']}",
            },
            "tasks": [{"ansible.builtin.import_role": {"name": "keycloak-client"}, "tags": ["keycloak-client"]}],
        }
        for realm in client_secrets
    ]))
    result = subprocess.run(
        [str(Path(sys.executable).parent / "ansible-playbook"), "-i", "localhost,", str(playbook),
         "--tags", "keycloak-client"],
        cwd=directory, capture_output=True, text=True, timeout=90,
        env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
             "ANSIBLE_COLLECTIONS_PATH": str(directory / "collections"),
             "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
             "ANSIBLE_LOCAL_TEMP": str(directory / "ansible"),
             "ANSIBLE_REMOTE_TEMP": str(directory / "ansible-remote"),
             "TEST_KUBERNETES_STATE": str(state), "NO_PROXY": "127.0.0.1,localhost"},
    )
    assert result.returncode == 0, result.stdout + result.stderr


def verify_backup_restore(stack):
    """Run the shipped backup playbook; only Kubernetes discovery/exec is a boundary."""
    directory = stack["temporary"]
    executable = directory / "bin"
    executable.mkdir()
    kubectl = executable / "kubectl"
    kubectl.write_text("#!" + sys.executable + "\n" + """
import base64, json, os, subprocess, sys
args = sys.argv[1:]
if args[0] == 'exec':
    stdin_args = ['-i'] if '-i' in args else []
    raise SystemExit(subprocess.call(['docker', 'exec', *stdin_args, os.environ['TEST_DB'],
                                     *args[args.index('--') + 1:]]))
if args[:2] == ['get', 'secret']:
    fields = {'admin_username': os.environ['KC_BOOTSTRAP_ADMIN_USERNAME'],
              'admin_password': os.environ['KC_BOOTSTRAP_ADMIN_PASSWORD'],
              'database_password': os.environ['KC_DB_PASSWORD']}
    print(json.dumps({'apiVersion': 'v1', 'kind': 'Secret',
                      'metadata': {'name': 'keycloak', 'namespace': 'authentication'},
                      'data': {key: base64.b64encode(value.encode()).decode() for key, value in fields.items()}}))
    raise SystemExit(0)
raise SystemExit(2)
""")
    kubectl.chmod(0o755)
    inventory = directory / "backup-inventory.yml"
    inventory.write_text(yaml.safe_dump({
        "hcloud_type_master": {"hosts": {"localhost": {
            "ansible_connection": "local", "ansible_python_interpreter": sys.executable,
        }}},
    }))
    variables = directory / "backup-vars.yml"
    backup = directory / "backups"
    variables.write_text(yaml.safe_dump({
        "startup_keycloak_operator": True, "keycloak_namespace": "authentication",
        "local_backup_root": str(backup),
    }))
    result = subprocess.run([
        str(Path(sys.executable).parent / "ansible-playbook"), "-i", str(inventory),
        str(ROOT / "roles/keycloak/files/backup-playbook.yml"), "--extra-vars", "@" + str(variables),
    ], env={
        **stack["environment"], "PATH": str(executable) + os.pathsep + os.environ["PATH"],
        "TEST_DB": stack["database"], "ANSIBLE_LOCAL_TEMP": str(directory / "ansible"),
        "ANSIBLE_REMOTE_TEMP": str(directory / "ansible-remote"),
    }, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot, = backup.glob("*.tar.gz")
    assert snapshot.stat().st_mode & 0o777 == 0o600
    with tarfile.open(snapshot) as archive:
        assert set(archive.getnames()) == {"database.dump", "credentials.yml"}
        assert archive.extractfile("database.dump").read(5) == b"PGDMP"
        recovery_secret = yaml.safe_load(archive.extractfile("credentials.yml"))
        assert recovery_secret["kind"] == "Secret"

    # The new destination has an empty database and no application in Kubernetes.
    # Keep the original data under a separate name to distinguish it from the restore.
    server = stack["name"] + "-server"
    stack["docker"]("stop", server)
    stack["docker"]("exec", stack["database"], "psql", "-v", "ON_ERROR_STOP=1",
                    "-U", "keycloak", "-d", "postgres", "-c", "ALTER DATABASE keycloak RENAME TO original")
    stack["docker"]("exec", stack["database"], "createdb", "-U", "keycloak", "keycloak")
    state = directory / "recovery-kubernetes.json"
    state.write_text(json.dumps({"Node//recovery": resource("Node", "recovery")}))
    state.chmod(0o600)
    variables.write_text(yaml.safe_dump({
        "startup_keycloak_operator": True, "keycloak_namespace": "authentication",
        "restore_db": True, "restore_media": False, "db_backup_file": str(snapshot),
    }))

    def restore():
        return subprocess.run([
            str(Path(sys.executable).parent / "ansible-playbook"), "-i", str(inventory),
            str(ROOT / "roles/keycloak/files/restore-playbook.yml"), "--extra-vars", "@" + str(variables),
            "--skip-tags", "keycloak-prepare-client",
        ], cwd=directory, env={
            **os.environ, "PATH": str(executable) + os.pathsep + os.environ["PATH"],
            "TEST_DB": stack["database"], "TEST_KUBERNETES_STATE": str(state),
            "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
            "ANSIBLE_COLLECTIONS_PATH": str(directory / "collections"),
            "ANSIBLE_LOCAL_TEMP": str(directory / "ansible"),
            "ANSIBLE_REMOTE_TEMP": str(directory / "ansible-remote"),
        }, capture_output=True, text=True, timeout=90)

    restored = restore()
    assert restored.returncode == 0, restored.stdout + restored.stderr
    recovered = json.loads(state.read_text())
    assert recovered["Secret/authentication/keycloak"]["data"] == recovery_secret["data"]
    assert "Deployment/authentication/keycloak" not in recovered

    # The second invocation refuses the populated database before applying resources.
    repeated = restore()
    assert repeated.returncode != 0
    assert "The target Keycloak database is not empty" in repeated.stdout
    assert json.loads(state.read_text()) == recovered
    stack["docker"]("start", server)

    # Wait through the same HTTPS gateway that OAuth2 Proxy will use. The
    # test certificate is trusted here; the loopback address is only for DNS.
    port = stack["port"]
    tls = ssl.create_default_context(cafile=str(directory / "cert.pem"))
    tls.check_hostname = False
    gateway = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                           urllib.request.HTTPSHandler(context=tls))
    discovery = "/realms/first/.well-known/openid-configuration"
    probe = urllib.request.Request(f"https://127.0.0.1:{port}{discovery}",
                                   headers={"Host": f"auth.example.test:{port}"})
    for _ in range(60):
        try:
            with gateway.open(probe, timeout=2) as response:
                assert json.load(response)["issuer"] == f"https://auth.example.test:{port}/realms/first"
            break
        except (OSError, urllib.error.URLError):
            time.sleep(1)
    else:
        pytest.fail("The restored realm did not become discoverable through the HTTPS gateway")
    return recovered["Secret/authentication/keycloak"]


def test_real_keycloak_client_is_repeatable_and_oauth_proxy_login_isolated(stack):
    # GIVEN a real fresh PostgreSQL-backed Keycloak in production mode.
    client_secret = secrets.token_urlsafe(36)
    client_secrets = {"first": client_secret, "second": secrets.token_urlsafe(36)}
    provision_startups(stack, client_secrets)
    token = request(stack["api"] + "/realms/master/protocol/openid-connect/token", {
        "grant_type": "password", "client_id": "admin-cli",
        "username": "startup-admin", "password": stack["password"],
    }, form=True)["access_token"]
    assert request(stack["api"] + "/admin/realms/first", token=token)["registrationAllowed"] is False
    # The operator can open registration in Keycloak; deployment must retain it.
    request(stack["api"] + "/admin/realms/first", {
        "registrationAllowed": True,
    }, token, method="PUT")
    test_password = secrets.token_urlsafe(24)
    request(stack["api"] + "/admin/realms/first/users", {
        "username": "tester", "email": "tester@example.test", "enabled": True,
        "emailVerified": True, "firstName": "Test", "lastName": "User",
        "credentials": [{"type": "password", "value": test_password, "temporary": False}],
    }, token)
    # WHEN each attached startup repeats its own provisioning run.
    provision_startups(stack, client_secrets)
    token = request(stack["api"] + "/realms/master/protocol/openid-connect/token", {
        "grant_type": "password", "client_id": "admin-cli",
        "username": "startup-admin", "password": stack["password"],
    }, form=True)["access_token"]
    # THEN users and client credentials survive and realms remain isolated.
    assert len(request(stack["api"] + "/admin/realms/first/users", token=token)) == 1
    assert request(stack["api"] + "/admin/realms/second/users", token=token) == []
    assert request(stack["api"] + "/admin/realms/first", token=token)["registrationAllowed"] is True
    assert request(stack["api"] + "/admin/realms/second", token=token)["registrationAllowed"] is False
    clients = request(stack["api"] + "/admin/realms/first/clients?clientId=first", token=token)
    assert request(stack["api"] + "/admin/realms/first/clients/" + clients[0]["id"] + "/client-secret", token=token)["value"] == client_secret
    assert clients[0]["directAccessGrantsEnabled"] is False
    assert clients[0]["attributes"]["pkce.code.challenge.method"] == "S256"
    recovered_secret = verify_backup_restore(stack)

    # Recover an application namespace that lost its client and cookie Secret.
    state = stack["temporary"] / "kubernetes.json"
    objects = json.loads(state.read_text())
    objects["Secret/authentication/keycloak"] = recovered_secret
    del objects["Secret/app-first/keycloak-client"]
    port = stack["docker"]("port", stack["name"] + "-server", "8080/tcp").stdout.strip().rsplit(":", 1)[1]
    objects["Service/authentication/keycloak"]["spec"]["ports"][0]["port"] = int(port)
    state.write_text(json.dumps(objects))
    provision_startups(stack, {"first": client_secret})
    credentials = json.loads(state.read_text())["Secret/app-first/keycloak-client"]["data"]
    recovered_client_secret = base64.b64decode(credentials["OAUTH2_PROXY_CLIENT_SECRET"]).decode()
    assert recovered_client_secret != client_secret

    defaults = yaml.safe_load((ROOT / "roles/oauth2-proxy/defaults/main.yml").read_text())
    values = {
        **defaults, "base_domain": "app.example.test", "k8s_namespace": "first",
        "auth_domain": f"https://auth.example.test:{stack['port']}/realms/first",
        "auth_audience": "first", "ansible_date_time": {"iso8601_basic": "test"},
        "oauth2_proxy_provider": "keycloak-oidc",
        "oauth2_proxy_oidc_issuer_url": f"https://auth.example.test:{stack['port']}/realms/first",
        "oauth2_proxy_host": f"app.example.test:{stack['port']}",
        "oauth2_proxy_whitelist_domain": "app.example.test:*",
        "oauth2_proxy_upstream": "static://200",
        "oauth2_proxy_extra_args": ["--provider-ca-file=/test-ca.pem"],
    }
    proxy = render("roles/oauth2-proxy/templates/deployment.yml", values)["spec"]["template"]["spec"]["containers"][0]
    stack["environment"].update(
        OAUTH2_PROXY_CLIENT_ID="first", OAUTH2_PROXY_CLIENT_SECRET=recovered_client_secret,
        OAUTH2_PROXY_COOKIE_SECRET=base64.b64decode(credentials["OAUTH2_PROXY_COOKIE_SECRET"]).decode(),
    )
    container = stack["name"] + "-proxy"
    stack["docker"]("run", "-d", "--name", container, "--network", stack["name"],
                    "--network-alias", "oauth2-proxy", "-v", f"{stack['temporary']}/cert.pem:/test-ca.pem:ro",
                    "-e", "NO_PROXY=auth.example.test,app.example.test,keycloak,postgres,localhost,127.0.0.1",
                    "-e", "OAUTH2_PROXY_CLIENT_ID", "-e", "OAUTH2_PROXY_CLIENT_SECRET",
                    "-e", "OAUTH2_PROXY_COOKIE_SECRET", proxy["image"], *proxy["args"])
    stack["created"].append(container)
    from playwright.sync_api import Error as PlaywrightError, sync_playwright
    with sync_playwright() as playwright:
        browser_options = {}
        if proxy_address := os.environ.get("HTTPS_PROXY"):
            # Only these disposable loopback services bypass the environment's proxy.
            browser_options["proxy"] = {
                "server": proxy_address,
                "bypass": "auth.example.test,app.example.test,localhost,127.0.0.1",
            }
        browser = playwright.chromium.launch(
            args=["--host-resolver-rules=MAP auth.example.test 127.0.0.1, MAP app.example.test 127.0.0.1"],
            **browser_options,
        )
        try:
            context = browser.new_context(ignore_https_errors=True)
            page = context.new_page()
            origin = f"https://app.example.test:{stack['port']}"
            for _ in range(30):
                try:
                    response = page.goto(origin + "/ping")
                except PlaywrightError as error:
                    # Parallel Docker scenarios change network interfaces during startup.
                    if "net::ERR_NETWORK_CHANGED" not in str(error):
                        raise
                    time.sleep(1)
                    continue
                if response and response.status == 200:
                    break
                running = stack["docker"](
                    "inspect", "--format", "{{.State.Running}}", container,
                ).stdout.strip()
                if running != "true":
                    pytest.fail("Disposable proxy exited: " + stack["docker"](
                        "logs", "--tail", "20", container, check=False,
                    ).stderr)
                time.sleep(1)
            else:
                pytest.fail("The rendered OAuth2 Proxy did not start: " + stack["docker"](
                    "logs", "--tail", "20", container, check=False,
                ).stderr)
            # Invalid credentials never reach the protected upstream.
            rejected = page.evaluate("""async () => (await fetch('/private_api/session', {
                headers: {Authorization: 'Bearer invalid'}, redirect: 'manual'
            })).status""")
            assert rejected in (0, 401, 403)
            page.goto(origin + "/oauth2/start?" + urllib.parse.urlencode({"rd": origin + "/private_api/session"}))
            page.get_by_label("Username or email").fill("tester")
            page.get_by_label("Password", exact=True).fill("incorrect")
            page.get_by_role("button", name="Sign In", exact=True).click()
            assert page.get_by_text("Invalid username or password.").is_visible()
            page.get_by_label("Password", exact=True).fill(test_password)
            page.get_by_role("button", name="Sign In", exact=True).click()
            try:
                page.wait_for_url(origin + "/private_api/session", timeout=30000)
            except Exception as error:
                # All identities in this stack are disposable. Keep query
                # parameters and cookies out of failure reports.
                page.screenshot(path=str(stack["temporary"] / "login-failure.png"))
                pytest.fail(
                    "Login stopped at " + page.url.split("?", 1)[0]
                    + ": " + page.locator("body").inner_text()[:1600]
                    + f" ({type(error).__name__})"
                )
            assert page.evaluate("async () => (await fetch('/private_api/session')).status") == 200
            identity = page.evaluate("async () => (await fetch('/oauth2/userinfo')).json()")
            assert identity["email"] == "tester@example.test"
            cookies = [cookie for cookie in context.cookies() if cookie["name"].startswith("_oauth2_proxy")]
            assert cookies and all(cookie["secure"] and cookie["httpOnly"] for cookie in cookies)
            identity_cookies = [cookie for cookie in context.cookies()
                                if cookie["name"].startswith("KEYCLOAK_IDENTITY")]
            assert identity_cookies and all(cookie["secure"] and cookie["httpOnly"] for cookie in identity_cookies)
            context.close()
        finally:
            browser.close()
