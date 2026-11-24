"""Run the real owner/attached role against a persistent Kubernetes boundary."""

import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

PROVIDER = '''
import base64, copy, json, os
from pathlib import Path
import yaml
from ansible.module_utils.basic import AnsibleModule
module = AnsibleModule(argument_spec=dict(
    kind=dict(type='str'), namespace=dict(type='str'), name=dict(type='str'),
    api_version=dict(type='str'), label_selectors=dict(type='list'),
    state=dict(type='str'), definition=dict(type='raw'), wait=dict(type='bool'),
    wait_timeout=dict(type='int'), wait_condition=dict(type='dict'),
))
path = Path(os.environ['TEST_KUBERNETES_STATE'])
objects = json.loads(path.read_text())
params = module.params
definition = params['definition']
if params['state'] == 'absent':
    key = '/'.join([params['kind'], params['namespace'] or '', params['name']])
    changed = objects.pop(key, None) is not None
    path.write_text(json.dumps(objects, sort_keys=True))
    module.exit_json(changed=changed)
if definition is not None:
    definitions = list(yaml.safe_load_all(definition)) if isinstance(definition, str) else [definition]
    changed = False
    for definition in definitions:
        if definition is None:
            continue
        metadata = definition['metadata']
        key = '/'.join([definition['kind'], metadata.get('namespace', ''), metadata['name']])
        if 'stringData' in definition:
            definition['data'] = {k: base64.b64encode(v.encode()).decode()
                                  for k, v in definition.pop('stringData').items()}
        previous = copy.deepcopy(objects.get(key, {}))
        previous.pop('status', None)
        changed |= previous != definition
        if definition['kind'] == 'Deployment':
            definition['status'] = {'readyReplicas': 1, 'conditions': [{'type': 'Available', 'status': 'True'}]}
        objects[key] = definition
    path.write_text(json.dumps(objects, sort_keys=True))
    module.exit_json(changed=changed)
resources = [obj for obj in objects.values()
    if obj['kind'] == params['kind']
    and (not params['namespace'] or obj['metadata'].get('namespace') == params['namespace'])
    and (not params['name'] or obj['metadata']['name'] == params['name'])
    and all(obj['metadata'].get('labels', {}).get(item.split('=')[0]) == item.split('=')[1]
            for item in params['label_selectors'] or [])]
module.exit_json(changed=False, resources=resources)
'''


def resource(kind, name, namespace=None, **values):
    metadata = {"name": name}
    if namespace:
        metadata["namespace"] = namespace
    return {"apiVersion": "v1", "kind": kind, "metadata": metadata, **values}


@pytest.fixture
def project_cluster(tmp_path):
    modules = tmp_path / "collections/ansible_collections/kubernetes/core/plugins/modules"
    modules.mkdir(parents=True)
    for name in ("k8s", "k8s_info"):
        (modules / f"{name}.py").write_text(PROVIDER)
    objects = {
        "Node//node-a": resource("Node", "node-a"),
        "Secret/owner/application": resource("Secret", "application", "owner", data={"password": "unchanged"}),
        "PersistentVolume//owner-postgres": resource("PersistentVolume", "owner-postgres", spec={"hostPath": {"path": "/srv/owner/postgres"}}),
        "Namespace//cert-manager": resource("Namespace", "cert-manager"),
        "HelmChart/kube-system/cert-manager": resource("HelmChart", "cert-manager", "kube-system", spec={"version": "existing"}),
        "Deployment/cert-manager/cert-manager": resource("Deployment", "cert-manager", "cert-manager", status={"readyReplicas": 1}),
        "Certificate/authentication/keycloak-tls": resource("Certificate", "keycloak-tls", "authentication",
            status={"conditions": [{"type": "Ready", "status": "True"}]}),
    }
    for name in ("issuers.cert-manager.io", "gatewayclasses.gateway.networking.k8s.io",
                 "gateways.gateway.networking.k8s.io", "httproutes.gateway.networking.k8s.io"):
        objects[f"CustomResourceDefinition//{name}"] = resource("CustomResourceDefinition", name, status={"conditions": [{"type": "Established", "status": "True"}]})
    state = tmp_path / "cluster.json"
    state.write_text(json.dumps(objects, sort_keys=True))

    def deploy(*, managed=True, namespace="owner", tags=None, role="keycloak", **settings):
        variables = {
            "project_name": namespace, "k8s_namespace": namespace,
            "startup_cluster_managed": managed,
            "keycloak_hostname": "auth.example.com",
            "keycloak_cluster_id": "a" * 32,
            "keycloak_tls_termination": "external_proxy",
            # The owner's application database overrides must never reach Keycloak.
            "postgres_pv_name": "owner-postgres",
            "postgres_data_path": "/srv/owner/postgres",
            "postgres_port": 6543, "postgres_version": "application-version",
            "postgres_admin_username": "application", "postgres_admin_password": "application-password",
            **settings,
        }
        playbook = tmp_path / "playbook.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": variables,
            "tasks": [
                {"ansible.builtin.import_role": {"name": role}, "tags": [role]},
                {"name": "Deploy application after shared authentication",
                 "kubernetes.core.k8s": {"state": "present", "definition": {
                     "apiVersion": "v1", "kind": "ConfigMap",
                     "metadata": {"name": "application", "namespace": "{{ k8s_namespace }}"},
                     "data": {"project": "{{ project_name }}", "port": "{{ postgres_port | string }}"},
                 }}, "tags": ["application"]},
            ],
        }]))
        return subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(playbook), *(["--tags", tags] if tags else [])],
            cwd=tmp_path, capture_output=True, text=True, timeout=90,
            env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
                 "ANSIBLE_COLLECTIONS_PATH": str(tmp_path / "collections"),
                 "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
                 "ANSIBLE_LOCAL_TEMP": str(tmp_path / "controller"),
                 "ANSIBLE_REMOTE_TEMP": str(tmp_path / "remote"),
                 "ANSIBLE_HOME": str(tmp_path / "ansible-home"),
                 "TEST_KUBERNETES_STATE": str(state)},
        )
    return deploy, state


@pytest.mark.parametrize("tls", ["external_proxy", "ingress"])
def test_owner_installs_shared_auth_without_using_application_storage(project_cluster, tls):
    # GIVEN an owner project with its own namespace, database and port overrides.
    deploy, state = project_cluster
    before = json.loads(state.read_text())
    # WHEN its normal playbook deploys the actual Keycloak and Postgres roles.
    result = deploy(keycloak_tls_termination=tls)
    assert result.returncode == 0, result.stdout + result.stderr
    objects = json.loads(state.read_text())
    # THEN authentication is separate and later application tasks retain their settings.
    assert objects["PersistentVolume//owner-postgres"] == before["PersistentVolume//owner-postgres"]
    assert objects["Secret/owner/application"] == before["Secret/owner/application"]
    volume = objects["PersistentVolume//authentication-postgres"]
    assert volume["spec"]["hostPath"]["path"] == "/srv/authentication/postgres"
    assert volume["spec"]["claimRef"]["namespace"] == "authentication"
    database = objects["Deployment/authentication/postgres"]["spec"]["template"]["spec"]["containers"][0]
    assert database["image"] == "postgres:18.6"
    assert objects["Service/authentication/postgres"]["spec"]["ports"][0]["port"] == 5432
    assert objects["ConfigMap/owner/application"]["data"] == {"project": "owner", "port": "6543"}
    if tls == "ingress":
        assert "Issuer/authentication/letsencrypt-prod" in objects
        assert "Issuer/owner/letsencrypt-prod" not in objects
    secret = objects["Secret/authentication/keycloak"]
    assert base64.b64decode(secret["data"]["database_password"]).decode() != "application-password"


def test_existing_installation_is_reused_until_the_owner_requests_an_update(project_cluster):
    # GIVEN an installation with persisted recovery credentials and storage.
    deploy, state = project_cluster
    assert deploy(tags="keycloak").returncode == 0
    snapshot = state.read_bytes()
    # WHEN the owner's pin changes or a second startup deploys another version.
    for managed in (True, False):
        result = deploy(managed=managed, namespace="second", tags="keycloak", keycloak_version="reviewed-version")
        assert result.returncode == 0, result.stdout + result.stderr
        assert state.read_bytes() == snapshot
    # THEN only an explicit owner update changes the workload, retaining its data.
    before = json.loads(snapshot)
    result = deploy(tags="keycloak", keycloak_upgrade=True, keycloak_version="reviewed-version")
    assert result.returncode == 0, result.stdout + result.stderr
    after = json.loads(state.read_text())
    assert after["Deployment/authentication/keycloak"]["spec"]["template"]["spec"]["containers"][0]["image"].endswith(":reviewed-version")
    for key in ("Secret/authentication/keycloak", "PersistentVolume//authentication-postgres", "PersistentVolumeClaim/authentication/postgres"):
        assert after[key] == before[key]
    assert "ConfigMap/owner/application" not in after


def test_explicit_legacy_namespace_keeps_existing_authentication_in_place(project_cluster):
    # GIVEN an existing installation using the previous namespace.
    deploy, state = project_cluster
    result = deploy(tags="keycloak", keycloak_namespace="startup-auth")
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = state.read_bytes()
    # WHEN its project explicitly retains that namespace after the default changes.
    result = deploy(tags="keycloak", keycloak_namespace="startup-auth")
    # THEN its credentials/storage stay and no second installation appears.
    assert result.returncode == 0, result.stdout + result.stderr
    assert state.read_bytes() == snapshot
    objects = json.loads(snapshot)
    assert "Deployment/startup-auth/keycloak" in objects
    assert "Deployment/authentication/keycloak" not in objects


def test_fresh_namespace_requires_the_old_hostname_route_to_be_retired(project_cluster):
    # GIVEN the previous namespace still serving the same public hostname.
    deploy, state = project_cluster
    result = deploy(tags="keycloak", keycloak_namespace="startup-auth")
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = state.read_bytes()
    # WHEN the owner selects the new default, THEN no competing installation starts.
    result = deploy(tags="keycloak")
    assert result.returncode != 0
    assert "retire the old route explicitly" in result.stdout
    assert state.read_bytes() == snapshot
    # GIVEN the operator explicitly retires that route, WHEN the new role runs.
    objects = json.loads(snapshot)
    old_credentials = objects["Secret/startup-auth/keycloak"]
    del objects["Ingress/startup-auth/keycloak"]
    state.write_text(json.dumps(objects, sort_keys=True))
    result = deploy(tags="keycloak")
    # THEN the new installation has fresh credentials, with no old-data migration.
    assert result.returncode == 0, result.stdout + result.stderr
    objects = json.loads(state.read_text())
    assert objects["Secret/authentication/keycloak"] != old_credentials
    assert objects["Secret/startup-auth/keycloak"] == old_credentials
    assert objects["PersistentVolume//authentication-postgres"]["spec"]["hostPath"]["path"] == "/srv/authentication/postgres"


@pytest.mark.parametrize("existing,upgrade", [(False, False), (False, True), (True, True)])
def test_attached_project_cannot_install_or_update_the_shared_service(project_cluster, existing, upgrade):
    # GIVEN an attached project, with or without an owner's installation.
    deploy, state = project_cluster
    if existing:
        assert deploy(tags="keycloak").returncode == 0
    snapshot = state.read_bytes()
    # WHEN it asks to install or update authentication.
    result = deploy(managed=False, namespace="second", tags="keycloak", keycloak_upgrade=upgrade)
    # THEN the boundary contains no writes.
    assert result.returncode != 0
    assert "cluster owner project" in result.stdout
    assert state.read_bytes() == snapshot


@pytest.mark.parametrize("settings", [
    {"keycloak_namespace": "owner"},
    {"keycloak_namespace": "owner", "startup_keycloak_operator": True},
    {"keycloak_hostname": "invalid"},
])
def test_invalid_project_configuration_fails_before_cluster_changes(project_cluster, settings):
    # GIVEN configuration that would mix app/authentication or has no public hostname.
    deploy, state = project_cluster
    snapshot = state.read_bytes()
    # WHEN the project deploys, THEN no cluster resource changes.
    result = deploy(tags="keycloak", **settings)
    assert result.returncode != 0
    assert state.read_bytes() == snapshot


def test_recovery_secret_loss_never_reinitializes_existing_authentication(project_cluster):
    # GIVEN the existing database with a lost recovery Secret.
    deploy, state = project_cluster
    assert deploy(tags="keycloak").returncode == 0
    objects = json.loads(state.read_text())
    del objects["Secret/authentication/keycloak"]
    state.write_text(json.dumps(objects, sort_keys=True))
    snapshot = state.read_bytes()
    # WHEN the owner reapplies authentication, THEN recovery is required.
    result = deploy(tags="keycloak", keycloak_upgrade=True)
    assert result.returncode != 0
    assert "Restore the Keycloak Secret" in result.stdout
    assert state.read_bytes() == snapshot


@pytest.mark.parametrize("failure,diagnostic", [
    ("recovery", "Restore the Keycloak Secret"),
    ("workload", "Shared Keycloak is unavailable"),
    ("certificate", "The Keycloak certificate is missing or unready"),
])
def test_reusing_incomplete_installation_fails_without_writes(project_cluster, failure, diagnostic):
    # GIVEN a failed first install left a Deployment in the cluster.
    deploy, state = project_cluster
    assert deploy(tags="keycloak", keycloak_tls_termination="ingress").returncode == 0
    objects = json.loads(state.read_text())
    if failure == "recovery":
        del objects["Secret/authentication/keycloak"]
    elif failure == "workload":
        objects["Deployment/authentication/keycloak"]["status"] = {"readyReplicas": 0}
    else:
        del objects["Certificate/authentication/keycloak-tls"]
    state.write_text(json.dumps(objects, sort_keys=True))
    snapshot = state.read_bytes()

    # WHEN the owner repeats the normal command without forcing an upgrade.
    result = deploy(tags="keycloak", keycloak_tls_termination="ingress")

    # THEN the deployment reports the incomplete installation without writes.
    assert result.returncode != 0
    assert diagnostic in result.stdout
    assert state.read_bytes() == snapshot


@pytest.mark.parametrize("managed,operator", [(False, True), (True, False)])
def test_only_the_explicit_cluster_operator_can_provision_clients(project_cluster, managed, operator):
    # GIVEN a project that lacks one of the two ownership conditions.
    deploy, state = project_cluster
    snapshot = state.read_bytes()

    # WHEN it tries to run the actual provisioning role.
    result = deploy(role="keycloak-client", tags="keycloak-client", managed=managed,
                    startup_keycloak_operator=operator)

    # THEN it is rejected before accessing authentication resources.
    assert result.returncode != 0
    assert "Configure realms and clients from the cluster owner project" in result.stdout
    assert "Read shared Keycloak administrator" not in result.stdout
    assert state.read_bytes() == snapshot


def test_owner_rejects_a_client_declared_for_two_applications(project_cluster):
    # GIVEN two namespaces would receive different secrets for the same client.
    deploy, state = project_cluster
    snapshot = state.read_bytes()
    client = {"realm": "demo", "client_id": "demo", "origin": "https://demo.example.com"}

    # WHEN the owner applies the declarations.
    result = deploy(role="keycloak-client", tags="keycloak-client", startup_keycloak_operator=True,
                    keycloak_clients=[{**client, "namespace": "one"}, {**client, "namespace": "two"}])

    # THEN no shared credential is read and no application resource is changed.
    assert result.returncode != 0
    assert "Each realm and client ID pair must be assigned" in result.stdout
    assert "Read shared Keycloak administrator" not in result.stdout
    assert state.read_bytes() == snapshot


@pytest.mark.parametrize("credential_state", ["valid", "missing", "incomplete"])
def test_oauth_proxy_tag_reuses_keycloak_credentials_before_applying_resources(project_cluster, credential_state):
    # GIVEN a selected, namespaced Keycloak client Secret at the Kubernetes boundary.
    _, state = project_cluster
    temporary = state.parent
    values = {"OAUTH2_PROXY_CLIENT_ID": "journal", "OAUTH2_PROXY_CLIENT_SECRET": "synthetic-client-secret",
              "OAUTH2_PROXY_COOKIE_SECRET": "x" * 32}
    objects = json.loads(state.read_text())
    if credential_state != "missing":
        data = {key: base64.b64encode(value.encode()).decode() for key, value in values.items()}
        if credential_state == "incomplete":
            del data["OAUTH2_PROXY_CLIENT_SECRET"]
        objects["Secret/journal/keycloak-client"] = resource("Secret", "keycloak-client", "journal", data=data)
    state.write_text(json.dumps(objects, sort_keys=True))
    snapshot = state.read_bytes()
    playbook = temporary / "proxy.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"k8s_namespace": "journal", "base_domain": "example.com", "auth_audience": "journal",
                 "oauth2_proxy_provider": "keycloak-oidc",
                 "oauth2_proxy_oidc_issuer_url": "https://auth.example.com/realms/journal",
                 "oauth2_proxy_existing_secret": "keycloak-client",
                 "ansible_date_time": {"iso8601_basic": "20261124T120000"}},
        "roles": ["oauth2-proxy"],
    }]))
    # WHEN the real role runs through the existing tag-filtered deploy interface.
    result = subprocess.run(["ansible-playbook", "-i", "localhost,", str(playbook),
                             "--tags", "oauth2-proxy"], cwd=temporary,
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
             "ANSIBLE_COLLECTIONS_PATH": str(temporary / "collections"),
             "ANSIBLE_PYTHON_INTERPRETER": sys.executable,
             "ANSIBLE_HOME": str(temporary / "ansible-home"),
             "TEST_KUBERNETES_STATE": str(state)})
    # THEN missing credentials stop without mutation; valid credentials reach only the Secret.
    if credential_state != "valid":
        assert result.returncode != 0
        assert state.read_bytes() == snapshot
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        applied = json.loads(state.read_text())
        assert applied["Secret/journal/oauth2-proxy"]["data"] == objects["Secret/journal/keycloak-client"]["data"]
        container = applied["Deployment/journal/oauth2-proxy"]["spec"]["template"]["spec"]["containers"][0]
        assert "--provider=keycloak-oidc" in container["args"]
        assert "--oidc-issuer-url=https://auth.example.com/realms/journal" in container["args"]
        assert "--code-challenge-method=S256" in container["args"]
    assert "synthetic-client-secret" not in result.stdout + result.stderr
