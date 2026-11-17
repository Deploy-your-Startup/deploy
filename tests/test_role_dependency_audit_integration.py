"""Exercise real audit commands; stand in only for external registry/GitHub APIs."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_role_dependencies.py"
PUBLISH = ROOT / "scripts/publish_role_dependency_report.py"


@contextmanager
def registry():
    routes, requests = {}, []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            status, data, headers = routes.get(self.path, (404, {}, {}))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data.encode() if isinstance(data, str) else json.dumps(data).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", routes, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_audit(root, manifest, output, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root),
                           "--manifest", str(manifest), "--output", str(output), *extra],
                          capture_output=True, text=True, timeout=30,
                          env={**os.environ, "GH_TOKEN": "test-token-must-not-leak"})


def release(tag, **flags):
    return {"tag_name": tag, "draft": False, "prerelease": False, **flags}


def test_registry_comparison_pagination_and_identical_offline_replay(tmp_path):
    # GIVEN real role pins and external sources with misleading ordering,
    # prereleases, yanked files and latest versions on later pages.
    pins = {"k3s_version": "v1.36.9+k3s1", "chart_version": "1.9.0",
            "client_version": "36.0.3", "postgres_version": "18.6"}
    (tmp_path / "defaults.yml").write_text(yaml.safe_dump(pins))
    (tmp_path / "requirements.yml").write_text(yaml.safe_dump({"collections": [
        {"name": "example.collection", "version": "6.9.0"}]}))
    with registry() as (base, routes, requests):
        routes.update({
            "/github": (200, [release("v1.37.2+k3s1"),
                               release("v1.99.0+k3s1", draft=True),
                               release("v1.38.0-rc1+k3s1", prerelease=True)],
                        {"Link": f'<{base}/github?page=2>; rel="next"'}),
            "/github?page=2": (200, [release("v1.36.10+k3s2"),
                                      release("v1.36.10+k3s10"),
                                      release("v1.36.9+k3s1")], {}),
            "/channels": (200, {"data": [
                {"id": "stable", "latest": "v1.37.2+k3s1"},
                {"id": "v1.36", "latest": "v1.36.10+k3s10"},
                {"id": "testing", "latest": "v1.99.0+k3s1"},
                {"id": "v1.38-testing", "latest": "v1.38.0-rc1+k3s1"}]}, {}),
            "/index.yaml": (200, yaml.safe_dump({"entries": {"example": [
                {"version": "1.9.0"}, {"version": "1.10.0"},
                {"version": "2.0.0-rc1"}, {"version": "2.0.0", "deprecated": True}]}}), {}),
            "/pypi": (200, {"releases": {"36.0.3": [{"yanked": False}],
                       "37.0.2": [{"yanked": False}], "38.0.0rc1": [{"yanked": False}],
                       "39.0.0": [{"yanked": True}], "40.0.0": []}}, {}),
            "/postgres": (200, [{"major": "18", "latestMinor": "7", "supported": True},
                                  {"major": "19", "latestMinor": "1", "supported": True},
                                  {"major": "99", "latestMinor": "0", "supported": False}], {}),
            "/galaxy": (200, {"data": [{"version": "6.9.0"}],
                               "links": {"next": "/galaxy?page=2"}}, {}),
            "/galaxy?page=2": (200, {"data": [{"version": "6.10.0"}, {"version": "7.0.0"}],
                                      "links": {"next": None}}, {}),
        })
        entries = []
        for name, kind, key, url, extra in [
            ("k3s", "github", "k3s_version", "/github", {"series": 2}),
            ("channels", "k3s", "k3s_version", "/channels", {"series": 2}),
            ("helm", "helm", "chart_version", "/index.yaml", {"chart": "example"}),
            ("pypi", "pypi", "client_version", "/pypi", {}),
            ("postgres", "postgres", "postgres_version", "/postgres", {}),
            ("galaxy", "galaxy", None, "/galaxy", {"collection": "example.collection"}),
        ]:
            entries.append({"name": name, "kind": kind, "key": key,
                            "path": "defaults.yml" if key else "requirements.yml",
                            "url": base + url, "series": 1, **extra})
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps(entries))
        original = (tmp_path / "defaults.yml").read_bytes()
        # WHEN the real CLI reads those registries, THEN numeric ordering,
        # version boundaries and filtering yield the expected published versions.
        output = tmp_path / "live"
        result = run_audit(tmp_path, manifest, output)
        assert result.returncode == 0, result.stdout + result.stderr
        rows = {row["name"]: row for row in json.loads((output / "report.json").read_text())}
        assert rows["k3s"]["same_series"] == "v1.36.10+k3s10"
        assert rows["k3s"]["latest"] == "v1.37.2+k3s1"
        assert rows["channels"]["same_series"] == "v1.36.10+k3s10"
        assert rows["channels"]["latest"] == "v1.37.2+k3s1"
        assert rows["helm"]["latest"] == "1.10.0"
        assert rows["pypi"]["same_series"] == "36.0.3"
        assert rows["pypi"]["latest"] == "37.0.2"
        assert rows["postgres"]["same_series"] == "18.7"
        assert rows["galaxy"]["same_series"] == "6.10.0"
        assert all(row["status"] == "update" for row in rows.values())
        assert (tmp_path / "defaults.yml").read_bytes() == original
        assert all(auth is None for _, auth in requests)
        assert "test-token-must-not-leak" not in (output / "sources.json").read_text()
    # WHEN replaying with all registry servers stopped, THEN every artifact is
    # byte-identical, including cached pagination. No network fallback is possible.
    replay = tmp_path / "replay"
    result = run_audit(tmp_path, manifest, replay, "--snapshot", str(output / "sources.json"))
    assert result.returncode == 0, result.stdout + result.stderr
    for filename in ("report.md", "report.json", "sources.json"):
        assert (replay / filename).read_bytes() == (output / filename).read_bytes()


@pytest.mark.parametrize("problem", ["denied", "malformed", "missing", "loop", "unstable", "ahead"])
def test_unverified_sources_fail_and_preserve_diagnostics(tmp_path, problem):
    # GIVEN a failed/invalid upstream or pin, WHEN checking the real command,
    # THEN it fails, writes an error report, and never claims the pin is current.
    (tmp_path / "defaults.yml").write_text("pin_version: '1.0.0'\n")
    with registry() as (base, routes, requests):
        payload = [release("1.0.0")]
        if problem == "unstable":
            payload = [release("2.0.0-rc1", prerelease=True)]
        if problem == "ahead":
            payload = [release("0.9.0")]
        routes["/releases"] = (403 if problem == "denied" else 200,
                                "{broken" if problem == "malformed" else payload,
                                {"Link": f'<{base}/releases>; rel="next"'} if problem == "loop" else {})
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps([{"name": "test", "path": "defaults.yml",
            "key": "pin_version", "kind": "github", "url": base + "/releases", "series": 1}]))
        extra = []
        if problem == "missing":
            snapshot = tmp_path / "empty.json"
            snapshot.write_text("{}")
            extra = ["--snapshot", str(snapshot)]
        output = tmp_path / "report"
        result = run_audit(tmp_path, manifest, output, *extra)
        assert result.returncode == 1, result.stdout + result.stderr
        assert json.loads((output / "report.json").read_text())[0]["status"] == "error"
        assert "test-token-must-not-leak" not in result.stdout + result.stderr
        count = len(requests)
        replay = tmp_path / "replay"
        repeated = run_audit(tmp_path, manifest, replay, "--snapshot", str(output / "sources.json"))
        assert repeated.returncode == 1
        assert len(requests) == count
        for name in ("report.md", "report.json", "sources.json"):
            assert (output / name).read_bytes() == (replay / name).read_bytes()


def test_manifest_tracks_all_explicit_role_and_collection_versions():
    # GIVEN the repository's actual defaults and requirements, WHEN adding a
    # pin, THEN CI requires it to be present exactly once in the audit manifest.
    manifest = json.loads((ROOT / "scripts/role-dependencies.json").read_text())
    actual = [(entry["path"], entry.get("key") or entry["collection"]) for entry in manifest]
    expected = []
    for path in (ROOT / "roles").glob("*/defaults/*.yml"):
        for key in yaml.safe_load(path.read_text()) or {}:
            if key.endswith("_version"):
                expected.append((str(path.relative_to(ROOT)), key))
    expected.extend(("requirements.yml", entry["name"]) for entry in
                    yaml.safe_load((ROOT / "requirements.yml").read_text())["collections"])
    assert sorted(actual) == sorted(expected)


def test_issue_lifecycle_and_denied_access(tmp_path):
    # GIVEN a GitHub CLI boundary that persists issue state, WHEN the real
    # publisher runs repeatedly, THEN one issue is created, updated, closed and
    # reopened; denied access fails without creating a duplicate.
    cli = tmp_path / "gh"
    cli.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
p = pathlib.Path(os.environ['ISSUE_STATE'])
state = json.loads(p.read_text())
args = sys.argv[1:]
if os.environ.get('DENY_GH'):
    sys.exit(1)
command = args[1]
if command == 'list':
    print(json.dumps(state))
elif command == 'create':
    state.append(dict(number=1, title=args[args.index('--title')+1], state='OPEN',
                      body=pathlib.Path(args[args.index('--body-file')+1]).read_text()))
elif command == 'edit':
    state[0]['body'] = pathlib.Path(args[args.index('--body-file')+1]).read_text()
else:
    state[0]['state'] = 'OPEN' if command == 'reopen' else 'CLOSED'
p.write_text(json.dumps(state))
''')
    cli.chmod(0o755)
    state = tmp_path / "issues.json"
    state.write_text("[]")
    report = tmp_path / "report"
    report.mkdir()
    body = report / "report.md"
    body.write_text("<!-- deploy-role-dependency-audit -->\nfirst report\n")
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "ISSUE_STATE": str(state)}

    def publish(status, **extra):
        (report / "report.json").write_text(json.dumps([{"status": status}]))
        return subprocess.run([sys.executable, str(PUBLISH), "--repo", "example/deploy",
                               "--report", str(report)], env={**env, **extra},
                              capture_output=True, text=True, timeout=15)

    assert publish("current").returncode == 0
    assert json.loads(state.read_text()) == []
    assert publish("update").returncode == 0
    body.write_text("<!-- deploy-role-dependency-audit -->\nnew report\n")
    assert publish("update").returncode == 0
    issues = json.loads(state.read_text())
    assert len(issues) == 1 and "new report" in issues[0]["body"]
    assert publish("current").returncode == 0
    assert json.loads(state.read_text())[0]["state"] == "CLOSED"
    assert publish("error").returncode == 0
    assert json.loads(state.read_text())[0]["state"] == "OPEN"
    previous = state.read_bytes()
    assert publish("update", DENY_GH="1").returncode != 0
    assert state.read_bytes() == previous


def test_weekly_workflow_retains_failed_audit_evidence():
    # GIVEN the real repository-only workflow, THEN source failures remain red
    # while summary, issue and snapshot publication can still run.
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci-check-role-dependencies.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["schedule"] == [{"cron": "23 7 * * 1"}]
    assert "workflow_dispatch" in triggers and "workflow_call" not in triggers
    assert workflow["permissions"] == {"contents": "read", "issues": "write"}
    steps = workflow["jobs"]["audit"]["steps"]
    audit = next(step for step in steps if step.get("id") == "audit")
    assert audit["continue-on-error"] is True
    assert "steps.audit.outcome == 'failure'" in steps[-1]["if"]
    assert steps[-1]["run"] == "exit 1"
    assert any("sources" in step.get("name", "") or "snapshot" in step.get("name", "") for step in steps)
