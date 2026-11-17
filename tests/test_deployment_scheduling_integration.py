"""Exercise the shipped freshness command against an HTTP GitHub boundary."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.github/actions/current-deployment/current.py'


@pytest.fixture
def github(tmp_path):
    state = {'runs': [{'id': 200, 'run_number': 20}], 'requests': [], 'denied': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state['requests'].append(self.path)
            if state['denied'] or self.headers.get('Authorization') != 'Bearer LOCAL_TEST':
                self.send_error(403)
                return
            if self.path == '/repos/owner/app/actions/runs/200':
                value = {'id': 200, 'run_number': 20, 'workflow_id': 9, 'head_branch': 'release/test'}
            else:
                parsed = urlsplit(self.path)
                assert parsed.path == '/repos/owner/app/actions/workflows/9/runs'
                query = parse_qs(parsed.query)
                assert query['branch'] == ['release/test']
                assert query['event'] == ['push']
                page = int(query['page'][0])
                value = {'workflow_runs': state['runs'][(page - 1) * 100:page * 100]}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def invoke(event='push'):
        output = tmp_path / 'output'
        summary = tmp_path / 'summary'
        output.write_text('')
        summary.write_text('')
        result = subprocess.run([sys.executable, str(SCRIPT)], env={
            **os.environ, 'GITHUB_API_URL': f'http://127.0.0.1:{server.server_port}',
            'GITHUB_REPOSITORY': 'owner/app', 'GITHUB_RUN_ID': '200',
            'GH_TOKEN': 'LOCAL_TEST', 'GITHUB_EVENT_NAME': event,
            'GITHUB_OUTPUT': str(output), 'GITHUB_STEP_SUMMARY': str(summary),
        }, capture_output=True, text=True, timeout=10)
        return result, output.read_text(), summary.read_text()

    yield state, invoke
    server.shutdown()
    thread.join()


@pytest.mark.parametrize('newer_status', ['queued', 'in_progress', 'completed'])
def test_newer_push_prevents_an_old_run_from_publishing_after_waiting(github, newer_status):
    # GIVEN checks completed on an older push while another deployment held the lock.
    state, invoke = github
    state['runs'].append({'id': 201, 'run_number': 21, 'status': newer_status, 'conclusion': 'failure'})
    # WHEN the actual command executes after the publication lock is acquired.
    result, output, summary = invoke()
    # THEN a newer push prevents stale publication even when its checks fail.
    assert result.returncode == 0, result.stderr
    assert output == 'current=false\n'
    assert 'newer push' in summary


def test_latest_push_can_publish_even_when_an_old_run_is_rerun(github):
    # GIVEN the latest push and a rerun of an older workflow, returned first by the API.
    state, invoke = github
    state['runs'].insert(0, {'id': 199, 'run_number': 19, 'run_attempt': 5})
    # WHEN the real freshness command reads workflow history.
    result, output, _ = invoke()
    # THEN it compares push order, not completion order or rerun attempts.
    assert result.returncode == 0, result.stderr
    assert output == 'current=true\n'


def test_history_is_paginated_without_treating_old_reruns_as_new_pushes(github):
    # GIVEN a page of rerun older workflows before the current push.
    state, invoke = github
    state['runs'] = [{'id': i, 'run_number': 19} for i in range(100)] + state['runs']
    # WHEN the shipped command searches the paginated history.
    result, output, _ = invoke()
    # THEN it finds the current push instead of assuming the first API page is complete.
    assert result.returncode == 0, result.stderr
    assert output == 'current=true\n'
    assert any('page=2' in path for path in state['requests'])


@pytest.mark.parametrize('denied', [False, True])
def test_unverifiable_history_blocks_publication(github, denied):
    # GIVEN unavailable API permissions or history missing the current run.
    state, invoke = github
    state['denied'] = denied
    state['runs'] = []
    # WHEN the real freshness command cannot establish eligibility.
    result, output, _ = invoke()
    # THEN publication receives no authorization output and the job fails.
    assert result.returncode != 0
    assert output == ''
    assert 'Cannot verify deployment freshness' in result.stderr
    assert 'LOCAL_TEST' not in result.stderr


def test_manual_redeployment_preserves_the_selected_revision(github):
    # GIVEN an explicit manual redeployment, including historical revisions.
    state, invoke = github
    state['denied'] = True
    # WHEN the shipped command executes for workflow_dispatch.
    result, output, _ = invoke('workflow_dispatch')
    # THEN the explicit operator choice does not depend on automatic push freshness.
    assert result.returncode == 0, result.stderr
    assert output == 'current=true\n'
    assert state['requests'] == []


def test_shared_workflow_separates_cancellation_from_the_application_publication_lock():
    # GIVEN the shipped entry point and its actual child workflows.
    def workflow(name):
        return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)

    entry = workflow('build-and-deploy-service.yml')
    verify = workflow('verify-service-images.yml')
    publish = workflow('publish-service-images.yml')
    # WHEN following the reusable-job boundaries interpreted by GitHub Actions.
    checks = entry['jobs']['verify']
    deployment = entry['jobs']['publish']
    assert 'concurrency' not in entry
    assert checks['uses'] == './.github/workflows/verify-service-images.yml'
    assert checks['concurrency']['cancel-in-progress'] == 'true'
    assert 'github.run_id' in checks['concurrency']['group']
    assert deployment['uses'] == './.github/workflows/publish-service-images.yml'
    assert deployment['concurrency']['cancel-in-progress'] == 'false'
    assert 'github.ref' not in deployment['concurrency']['group']
    assert set(deployment['needs']) == {'verify', 'infrastructure-ready'}
    assert "needs.verify.result == 'success'" in deployment['if']
    assert "github.event_name != 'pull_request'" in deployment['if']
    # THEN the gate still protects every service, and freshness runs once inside
    # the application lock before the entire service matrix starts publication.
    assert set(verify['jobs']['checks']['needs']) == {'prepare', 'build', 'image', 'e2e'}
    assert publish['jobs']['publish']['needs'] == 'current'
    assert "needs.current.outputs.current == 'true'" in publish['jobs']['publish']['if']
    assert publish['jobs']['publish']['strategy']['matrix'] == '${{ fromJSON(inputs.matrix) }}'
    assert set(checks['with']) == set(verify['on']['workflow_call']['inputs'])
    assert entry['on']['workflow_call']['inputs'] == verify['on']['workflow_call']['inputs']


def test_all_mutating_paths_share_one_environment_lock_without_waiting_inside_it():
    # GIVEN all shipped entrypoints that can change the same deployment,
    def workflow(name):
        return yaml.load((ROOT / '.github/workflows' / name).read_text(), Loader=yaml.BaseLoader)

    build = workflow('build-and-deploy-service.yml')['jobs']
    direct = workflow('deploy.yml')['jobs']
    infrastructure = workflow('deploy-infrastructure.yml')['jobs']
    # WHEN the actual native scheduler definitions are compared,
    for job in [build['publish'], direct['deploy'], infrastructure['deploy-infrastructure']]:
        lock = job['concurrency']
        assert lock['cancel-in-progress'] == 'false'
        group = lock['group'].replace(" || 'production'", '')
        assert group == 'startup-deploy-${{ inputs.environment }}'
    for jobs in [build, direct]:
        assert 'concurrency' not in jobs['infrastructure-ready']
        assert 'infrastructure-ready' in jobs['publish' if jobs is build else 'deploy']['needs']
    # THEN any gate still inside the shared lock is nonblocking, avoiding a
    # dependency cycle when infrastructure is queued for that same lock.
    publish = workflow('publish-service-images.yml')['jobs']
    for steps in [direct['deploy']['steps'], publish['publish']['steps']]:
        wait = next(s for s in steps if '/wait-for-infrastructure@' in s.get('uses', ''))
        assert wait['with']['timeout_minutes'] == '0'
    assert set(publish['health']['needs']) == {'current', 'publish'}
    assert '/deployment-health@' in publish['health']['steps'][0]['uses']


def test_direct_deploy_checks_freshness_before_mutation_and_health_afterwards():
    # GIVEN the direct deployment path, which does not build application images,
    workflow = yaml.load((ROOT / '.github/workflows/deploy.yml').read_text(), Loader=yaml.BaseLoader)
    steps = workflow['jobs']['deploy']['steps']
    # WHEN following its executable action order,
    current = next(i for i, s in enumerate(steps) if s.get('id') == 'current')
    deploy = next(i for i, s in enumerate(steps) if '/actions/deploy@' in s.get('uses', ''))
    health = next(i for i, s in enumerate(steps) if '/deployment-health@' in s.get('uses', ''))
    # THEN a superseded run cannot mutate, and success requires configured health.
    assert current < deploy < health
    assert steps[deploy]['if'] == steps[health]['if'] == "steps.current.outputs.current == 'true'"


def test_repository_ci_keeps_documentation_tests_and_has_no_legacy_alias():
    # GIVEN the workflow inventory after consolidation,
    workflows = ROOT / '.github/workflows'
    # WHEN repository CI invokes the full real test runner,
    roles = yaml.load((workflows / 'ci-roles.yml').read_text(), Loader=yaml.BaseLoader)
    commands = [s.get('run', '') for s in roles['jobs']['check']['steps']]
    # THEN both documentation suites are still collected by pytest, while the
    # redundant workflow and obsolete public API are absent.
    assert './make.sh test' in commands
    assert 'testpaths = ["tests"]' in (ROOT / 'pyproject.toml').read_text()
    assert not (workflows / 'testing-policy.yml').exists()
    assert not (workflows / 'build-and-deploy-service-parallel.yml').exists()
