"""Apply the real client role to Keycloak and exercise password/passkey login."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.xdist_group("kpis-browser")


def request(url, data=None, token=None, method=None, form=False):
    payload = None if data is None else (
        urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode())
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, payload, headers, method=method), timeout=10) as response:
        return json.loads(response.read() or "null")


@pytest.fixture
def keycloak(tmp_path):
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        if os.environ.get("STARTUP_REQUIRE_DOCKER_TESTS") == "1":
            pytest.fail("Docker is required for the real Keycloak client checks")
        pytest.skip("Docker is unavailable")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    name = "keycloak-client-" + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(32)
    environment = {**os.environ, "KC_BOOTSTRAP_ADMIN_USERNAME": "test-operator",
                   "KC_BOOTSTRAP_ADMIN_PASSWORD": password}
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(tmp_path / "key.pem"), "-out", str(tmp_path / "cert.pem"),
        "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
    ], check=True, capture_output=True)
    # Only disposable certificates are shared with the container's unprivileged user.
    tmp_path.chmod(0o755)
    for filename in ("cert.pem", "key.pem"):
        (tmp_path / filename).chmod(0o644)
    try:
        subprocess.run([
            "docker", "run", "-d", "--name", name,
            "-p", "127.0.0.1::8080", "-p", f"127.0.0.1:{port}:8443",
            "-v", f"{tmp_path}:/test:ro", "-e", "KC_BOOTSTRAP_ADMIN_USERNAME",
            "-e", "KC_BOOTSTRAP_ADMIN_PASSWORD", "quay.io/keycloak/keycloak:26.8.1",
            "start-dev", f"--hostname=https://localhost:{port}",
            "--https-certificate-file=/test/cert.pem", "--https-certificate-key-file=/test/key.pem",
        ], env=environment, check=True, capture_output=True, timeout=180)
        mapped = subprocess.run(["docker", "port", name, "8080/tcp"],
                                check=True, capture_output=True, text=True).stdout.strip().rsplit(":", 1)[1]
        api = "http://127.0.0.1:" + mapped
        for _ in range(120):
            try:
                request(api + "/realms/master/.well-known/openid-configuration")
                break
            except (OSError, urllib.error.URLError):
                time.sleep(1)
        else:
            pytest.fail("Disposable Keycloak did not start")

        def admin(path, data=None, method=None):
            token = request(api + "/realms/master/protocol/openid-connect/token", {
                "grant_type": "password", "client_id": "admin-cli",
                "username": "test-operator", "password": password,
            }, form=True)["access_token"]
            return request(api + "/admin/" + path, data, token, method)

        yield {"api": api, "origin": f"https://localhost:{port}", "password": password,
               "temporary": tmp_path, "admin": admin}
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=30)


def configure(stack, *, realm="journal", check=True, **settings):
    encode = lambda value: base64.b64encode(value.encode()).decode()
    variables = {
        "project_name": realm, "k8s_namespace": realm, "keycloak_namespace": "authentication",
        "keycloak_realm": realm, "keycloak_client_id": realm,
        "keycloak_client_origin": stack["origin"],
        "keycloak_client_service": {"resources": [{"spec": {
            "clusterIP": "127.0.0.1", "ports": [{"port": int(stack["api"].rsplit(":", 1)[1])}],
        }}]},
        "keycloak_client_admin": {"resources": [{"data": {
            "admin_username": encode("test-operator"), "admin_password": encode(stack["password"]),
        }}]},
        "keycloak_client_credentials": {"resources": [{"data": {
            "OAUTH2_PROXY_CLIENT_SECRET": encode("disposable-client-secret"),
        }}]},
        **settings,
    }
    playbook = stack["temporary"] / "configure.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": variables,
        "tasks": [{"ansible.builtin.import_role": {"name": "keycloak-client", "tasks_from": phase}}
                  for phase in ("preflight.yml", "configure.yml")],
    }]))
    playbook.chmod(0o600)
    result = subprocess.run([
        str(Path(sys.executable).parent / "ansible-playbook"), "-i", "localhost,", str(playbook),
    ], env={**os.environ, "ANSIBLE_ROLES_PATH": str(ROOT / "roles"),
            "ANSIBLE_LOCAL_TEMP": str(stack["temporary"] / "ansible"),
            "ANSIBLE_REMOTE_TEMP": str(stack["temporary"] / "ansible-remote"),
            "NO_PROXY": "127.0.0.1,localhost"}, capture_output=True, text=True, timeout=90)
    if check:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def update_passwordless_policy(admin, **settings):
    realm = admin("realms/journal")
    policy = {key: value for key, value in realm.items() if key.startswith("webAuthnPolicyPasswordless")}
    admin("realms/journal", {**policy, **settings}, "PUT")


def test_passkey_policy_reconciles_only_managed_fields(keycloak):
    # GIVEN an existing realm, operator policy and another isolated realm.
    configure(keycloak)
    configure(keycloak, realm="other")
    admin = keycloak["admin"]
    other = admin("realms/other")
    master = admin("realms/master")
    update_passwordless_policy(admin, registrationAllowed=True, displayName="Operator journal",
                               webAuthnPolicyPasswordlessMediation="none")
    # WHEN the real role explicitly enables passkeys.
    configure(keycloak, keycloak_realm_passkeys_enabled=True)
    # THEN registration is optional, device verification is required, and repeats do no work.
    enabled = admin("realms/journal")
    assert enabled["webAuthnPolicyPasswordlessPasskeysEnabled"] is True
    assert enabled["webAuthnPolicyPasswordlessResidentKey"] == "required"
    assert enabled["webAuthnPolicyPasswordlessUserVerificationRequirement"] == "required"
    assert enabled["webAuthnPolicyPasswordlessMediation"] == "none"
    assert enabled["registrationAllowed"] is True
    assert enabled["displayName"] == "Operator journal"
    action = admin("realms/journal/authentication/required-actions/webauthn-register-passwordless")
    assert action["enabled"] is True
    assert action["defaultAction"] is False
    assert "changed=0" in configure(keycloak, keycloak_realm_passkeys_enabled=True).stdout
    # WHEN console edits drift from declared policy, THEN deployment corrects them.
    update_passwordless_policy(admin, webAuthnPolicyPasswordlessPasskeysEnabled=False,
                               webAuthnPolicyPasswordlessResidentKey="discouraged",
                               webAuthnPolicyPasswordlessUserVerificationRequirement="preferred")
    configure(keycloak, keycloak_realm_passkeys_enabled=True)
    assert admin("realms/journal") == enabled
    # WHEN management is omitted, THEN console policy remains untouched.
    configure(keycloak)
    assert admin("realms/journal") == enabled
    # WHEN disabled, THEN enrollment policy stays, with no changes to other realms.
    configure(keycloak, keycloak_realm_passkeys_enabled=False)
    disabled = admin("realms/journal")
    assert disabled["webAuthnPolicyPasswordlessPasskeysEnabled"] is False
    assert disabled["webAuthnPolicyPasswordlessResidentKey"] == "required"
    assert "changed=0" in configure(keycloak, keycloak_realm_passkeys_enabled=False).stdout
    assert admin("realms/other") == other
    assert admin("realms/master") == master


@pytest.mark.parametrize("value", ["true", "false", 1, [], {}])
def test_invalid_passkey_setting_fails_before_contacting_keycloak(tmp_path, value):
    # GIVEN invalid configuration and no identity server.
    stack = {"api": "http://127.0.0.1:1", "origin": "https://example.test",
             "password": "unused-local-test", "temporary": tmp_path}
    # WHEN the actual Ansible preflight runs, THEN no API request is attempted.
    result = configure(stack, keycloak_realm_passkeys_enabled=value, check=False)
    assert result.returncode != 0
    assert "keycloak_realm_passkeys_enabled" in result.stdout
    assert "Authenticate the cluster automation account" not in result.stdout


@pytest.mark.parametrize("settings", [{"realm": "master"}, {"k8s_namespace": "authentication"}])
def test_unsafe_identity_boundary_is_rejected_before_contacting_keycloak(tmp_path, settings):
    # GIVEN configuration that could change the master realm or shared namespace.
    stack = {"api": "http://127.0.0.1:1", "origin": "https://example.test",
             "password": "unused-local-test", "temporary": tmp_path}
    # WHEN the role runs, THEN it refuses the boundary without requesting credentials.
    result = configure(stack, keycloak_realm_passkeys_enabled=True, check=False, **settings)
    assert result.returncode != 0
    assert "Authenticate the cluster automation account" not in result.stdout


def authorization_url(stack):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    url = stack["origin"] + "/realms/journal/protocol/openid-connect/auth?" + urllib.parse.urlencode({
        "client_id": "journal", "redirect_uri": stack["origin"] + "/oauth2/callback",
        "response_type": "code", "scope": "openid", "code_challenge": challenge,
        "code_challenge_method": "S256", "state": secrets.token_urlsafe(16),
    })
    return url, verifier


def complete_login(page, stack, verifier):
    page.wait_for_url(stack["origin"] + "/oauth2/callback?**")
    code = urllib.parse.parse_qs(urllib.parse.urlsplit(page.url).query)["code"][0]
    token = request(stack["api"] + "/realms/journal/protocol/openid-connect/token", {
        "grant_type": "authorization_code", "code": code, "client_id": "journal",
        "client_secret": "disposable-client-secret", "code_verifier": verifier,
        "redirect_uri": stack["origin"] + "/oauth2/callback",
    }, form=True)
    assert token["access_token"]


def test_registered_passkey_and_password_both_complete_oidc_login(keycloak):
    from playwright.sync_api import sync_playwright

    # GIVEN a realm enabled by the real role and one user opting into enrollment.
    configure(keycloak, keycloak_realm_passkeys_enabled=True)
    admin = keycloak["admin"]
    update_passwordless_policy(admin, webAuthnPolicyPasswordlessMediation="none")
    password = secrets.token_urlsafe(24)
    admin("realms/journal/users", {
        "username": "tester", "email": "tester@example.test", "enabled": True, "emailVerified": True,
        "firstName": "Test", "lastName": "User",
        "credentials": [{"type": "password", "value": password, "temporary": False}],
        "requiredActions": ["webauthn-register-passwordless"],
    }, "POST")
    user = admin("realms/journal/users?username=tester")[0]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.route(keycloak["origin"] + "/oauth2/callback?**",
                   lambda route: route.fulfill(status=200, body="OIDC callback"))
        cdp = context.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True,
        }})
        try:
            # WHEN the user registers a platform passkey after password authentication.
            url, verifier = authorization_url(keycloak)
            page.goto(url)
            page.get_by_label("Username or email").fill("tester")
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_role("button", name="Sign In", exact=True).click()
            register = page.get_by_role("button", name="Register", exact=True)
            register.wait_for()
            # Keycloak shows the button before its module attaches the click handler.
            page.wait_for_load_state("networkidle")
            page.once("dialog", lambda dialog: dialog.accept("Test device"))
            register.click()
            complete_login(page, keycloak, verifier)
            credentials_url = "realms/journal/users/" + user["id"] + "/credentials"
            credentials = admin(credentials_url)
            assert any(item["type"] == "webauthn-passwordless" for item in credentials)
            # WHEN deployment repeats and the user signs in with the registered passkey.
            configure(keycloak, keycloak_realm_passkeys_enabled=True)
            context.clear_cookies()
            url, verifier = authorization_url(keycloak)
            page.goto(url)
            page.get_by_role("link", name="Sign in with Passkey", exact=True).click()
            complete_login(page, keycloak, verifier)
            # THEN password login still works and disabling preserves the enrolled key.
            context.clear_cookies()
            url, verifier = authorization_url(keycloak)
            page.goto(url)
            page.get_by_label("Username or email").fill("tester")
            page.get_by_label("Password", exact=True).fill(password)
            page.get_by_role("button", name="Sign In", exact=True).click()
            complete_login(page, keycloak, verifier)
            configure(keycloak, keycloak_realm_passkeys_enabled=False)
            assert {item["id"] for item in admin(credentials_url)} == {item["id"] for item in credentials}
        finally:
            context.close()
            browser.close()
