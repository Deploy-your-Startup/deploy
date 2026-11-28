"""Run the shipped role on disposable Ubuntu nodes with the real policy parser.

Only kernel policy loading is a boundary stand-in: Docker cannot enforce the
Ubuntu host kernel's forced stacking. Full VM qualification remains separate.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "roles/containerd-apparmor/files/cri-containerd.apparmor.d"


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], text=True, capture_output=True,
                          check=True, timeout=180, **kwargs)


@pytest.fixture(scope="module")
def apparmor_image(tmp_path_factory):
    if not shutil.which("docker") or subprocess.run(
        ["docker", "info"], capture_output=True, timeout=15
    ).returncode:
        if os.environ.get("STARTUP_REQUIRE_DOCKER_TESTS") == "1":
            pytest.fail("Docker is required for containerd AppArmor role integration")
        pytest.skip("Docker is unavailable for containerd AppArmor role integration")
    root = tmp_path_factory.mktemp("apparmor-image")
    (root / "Dockerfile").write_text("""FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y python3 apparmor
COPY parser.py /usr/local/bin/test-apparmor-parser
RUN chmod 755 /usr/local/bin/test-apparmor-parser
COPY runtime.py /usr/local/bin/k3s
RUN chmod 755 /usr/local/bin/k3s && cp /usr/local/bin/k3s /usr/local/bin/systemctl
CMD ["sleep", "infinity"]
""")
    (root / "parser.py").write_text(r"""#!/usr/bin/python3
import json, pathlib, subprocess, sys
root = pathlib.Path('/node')
args = sys.argv[1:]
with (root / 'parser-calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
if '-Q' in args and (root / 'reject-policy').exists():
    sys.exit(1)
# Use the distribution parser for both validation and reload. Only the final
# kernel replacement is simulated, never the policy grammar or includes.
parsed = subprocess.run(['/sbin/apparmor_parser', '-Q', '-K', args[-1]])
if parsed.returncode:
    sys.exit(parsed.returncode)
if '-r' in args:
    if (root / 'fail-load').exists():
        sys.exit(1)
    if not (root / 'missing-enforcement').exists():
        (root / 'sys/kernel/security/apparmor/profiles').write_text('cri-containerd.apparmor.d (enforce)\n')
""")
    (root / "runtime.py").write_text(r"""#!/usr/bin/python3
import pathlib, sys
root = pathlib.Path('/node')
if pathlib.Path(sys.argv[0]).name == 'systemctl':
    print('disabled' if (root / 'disable-apparmor-boot').exists() else 'enabled')
else:
    if (root / 'broken-runtime').exists():
        sys.exit(1)
    print((root / 'runtime-version').read_text() if (root / 'runtime-version').exists()
          else 'ctr github.com/containerd/containerd/v2 v2.3.4-k3s1.36')
""")
    tag = "startup-apparmor-test:" + uuid.uuid4().hex[:12]
    docker("build", "-t", tag, str(root))
    try:
        yield tag
    finally:
        docker("image", "rm", tag)


@pytest.fixture
def apparmor_node(apparmor_image, tmp_path):
    identifier = docker("run", "-d", "-v", f"{tmp_path}:/node", apparmor_image).stdout.strip()
    for name, value in {
        'sys/module/apparmor/parameters/enabled': 'Y\n',
        'sys/kernel/security/apparmor/profiles': 'cri-containerd.apparmor.d (enforce)\n',
        'proc/101/comm': 'containerd\n',
        'proc/101/attr/current': 'unconfined\n',
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    docker("exec", identifier, "sh", "-c", "mkdir -p /node/etc && cp -a /etc/apparmor.d /node/etc/")
    inventory = tmp_path / "inventory.yml"
    inventory.write_text(yaml.safe_dump({'all': {'hosts': {'node': {
        'ansible_connection': 'community.docker.docker',
        'ansible_host': identifier,
        'ansible_python_interpreter': '/usr/bin/python3',
        'ansible_become': False,
    }}}}))
    (tmp_path / "ansible.cfg").write_text('[defaults]\n')
    try:
        yield tmp_path, identifier
    finally:
        docker("rm", "-f", identifier)


def run_role(node, entry="role", check_mode=False, tags=None, **variables):
    path, _ = node
    variables = {'containerd_apparmor_root': '/node',
                 'containerd_apparmor_parser': '/usr/local/bin/test-apparmor-parser',
                 **variables}
    (path / 'variables.json').write_text(json.dumps(variables))
    play = [{'hosts': 'all', 'gather_facts': False, 'roles': ['containerd-apparmor']}]
    if entry == 'k3s':
        play[0]['roles'] = [{'role': 'k3s', 'tags': ['k3s']}]
    elif entry == 'os-upgrade':
        play[0]['roles'] = ['os-upgrade']
    (path / 'playbook.yml').write_text(yaml.safe_dump(play))
    playbook = ROOT / 'update-vms-playbook.yml' if entry == 'update-vms' else path / 'playbook.yml'
    return subprocess.run([
        str(Path(sys.executable).parent / 'ansible-playbook'), str(playbook),
        '-i', str(path / 'inventory.yml'), '-e', '@' + str(path / 'variables.json'),
        *(['--check'] if check_mode else []),
        *(['--tags', tags] if tags else []),
    ], env={**os.environ, 'ANSIBLE_CONFIG': str(path / 'ansible.cfg'),
            'ANSIBLE_ROLES_PATH': str(ROOT / 'roles')},
       capture_output=True, text=True, timeout=120)


def policy(node):
    return node[0] / 'etc/apparmor.d/cri-containerd.apparmor.d'


def test_missing_stacked_permissions_are_installed_and_reruns_are_idempotent(apparmor_node):
    # GIVEN the previously generated in-kernel profile and no persistent policy,
    # WHEN the actual role runs twice, THEN the parser accepts the exact rules,
    # the file survives reloads and the second run changes nothing.
    result = run_role(apparmor_node)
    assert result.returncode == 0, result.stdout + result.stderr
    assert policy(apparmor_node).read_bytes() == PROFILE.read_bytes()
    assert policy(apparmor_node).stat().st_mode & 0o777 == 0o644
    text = policy(apparmor_node).read_text()
    assert 'signal (send,receive) peer=cri-containerd.apparmor.d//&unconfined,' in text
    assert 'ptrace (trace,tracedby,read,readby) peer=cri-containerd.apparmor.d//&unconfined,' in text
    assert '//&*' not in text
    result = run_role(apparmor_node)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'changed=0' in result.stdout
    calls = [json.loads(line) for line in (apparmor_node[0] / 'parser-calls.jsonl').read_text().splitlines()]
    assert sum('-r' in call for call in calls) == 2


@pytest.mark.parametrize('entry', ['k3s', 'update-vms', 'os-upgrade'])
def test_custom_policy_stops_installation_and_maintenance_before_mutation(apparmor_node, entry):
    # GIVEN an administrator-owned policy, WHEN a real installation or maintenance
    # entry is invoked, THEN the profile is preserved before installers/apt/drain.
    policy(apparmor_node).write_text('# administrator policy\n')
    result = run_role(apparmor_node, entry, tags='k3s' if entry == 'k3s' else None,
                      os_upgrade_target='26.04')
    assert result.returncode != 0 and 'already exists' in result.stdout
    assert policy(apparmor_node).read_text() == '# administrator policy\n'
    assert not (apparmor_node[0] / 'parser-calls.jsonl').exists()
    for name in ['Install first master (cluster-init)', 'Refresh apt package index',
                 'Drain this node while respecting disruption budgets and local data']:
        assert 'TASK [' + name + ']' not in result.stdout


@pytest.mark.parametrize('failure', ['confined-daemon', 'rootless', 'missing-include', 'invalid-policy', 'new-runtime', 'boot-disabled', 'broken-runtime'])
def test_unsupported_runtime_and_invalid_policy_fail_without_installing(apparmor_node, failure):
    # GIVEN an unsupported node or rejected policy, WHEN preparation runs, THEN
    # no persistent profile replaces the operator's runtime configuration.
    path, identifier = apparmor_node
    if failure == 'confined-daemon':
        (path / 'proc/101/attr/current').write_text('custom-runtime (enforce)\n')
    elif failure == 'rootless':
        docker('exec', identifier, 'sh', '-c', 'touch /usr/local/bin/rootlesskit && chmod 755 /usr/local/bin/rootlesskit')
    elif failure == 'missing-include':
        (path / 'etc/apparmor.d/abi/3.0').unlink()
    elif failure == 'new-runtime':
        (path / 'runtime-version').write_text('ctr github.com/containerd/containerd/v2 v2.3.99-k3s1')
    elif failure == 'boot-disabled':
        (path / 'disable-apparmor-boot').touch()
    elif failure == 'broken-runtime':
        (path / 'broken-runtime').touch()
    else:
        (path / 'reject-policy').touch()
    result = run_role(apparmor_node)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not policy(apparmor_node).exists()


def test_failed_load_is_retried_even_when_the_profile_file_already_matches(apparmor_node):
    # GIVEN a parser load failure after copying, WHEN preparation is rerun,
    # THEN the existing file is reloaded instead of falsely claiming protection.
    path, _ = apparmor_node
    (path / 'fail-load').touch()
    result = run_role(apparmor_node)
    assert result.returncode != 0 and policy(apparmor_node).exists()
    (path / 'fail-load').unlink()
    result = run_role(apparmor_node)
    assert result.returncode == 0, result.stdout + result.stderr
    assert policy(apparmor_node).read_bytes() == PROFILE.read_bytes()


def test_successful_parser_exit_without_enforcement_is_rejected(apparmor_node):
    # GIVEN the kernel does not enforce the policy, WHEN its parser exits zero,
    # THEN the role still blocks subsequent maintenance.
    path, _ = apparmor_node
    (path / 'missing-enforcement').touch()
    (path / 'sys/kernel/security/apparmor/profiles').write_text('')
    result = run_role(apparmor_node)
    assert result.returncode != 0 and 'not enforcing' in result.stdout


def test_disabled_apparmor_is_left_unchanged(apparmor_node):
    # GIVEN AppArmor is explicitly disabled, WHEN preparation runs, THEN no
    # profile is installed and no kernel or hardening setting is rewritten.
    path, _ = apparmor_node
    (path / 'sys/module/apparmor/parameters/enabled').write_text('N\n')
    result = run_role(apparmor_node)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not policy(apparmor_node).exists()
    assert not (path / 'parser-calls.jsonl').exists()
    assert (path / 'sys/module/apparmor/parameters/enabled').read_text() == 'N\n'


def test_ubuntu_upgrade_prepares_the_policy_before_entering_26_04(apparmor_node):
    # GIVEN the source LTS, WHEN an upgrade to 26.04 is prepared, THEN the
    # corrected persistent policy is present before the new kernel boots.
    _, identifier = apparmor_node
    docker('exec', identifier, 'sh', '-c', "sed -i 's/26.04/24.04/g' /etc/os-release")
    skipped = run_role(apparmor_node)
    assert skipped.returncode == 0, skipped.stdout + skipped.stderr
    assert not policy(apparmor_node).exists()
    result = run_role(apparmor_node, containerd_apparmor_target_release='26.04')
    assert result.returncode == 0, result.stdout + result.stderr
    assert policy(apparmor_node).read_bytes() == PROFILE.read_bytes()


def test_fresh_install_requires_a_reviewed_k3s_target(apparmor_node):
    # GIVEN no running or installed runtime, WHEN a known k3s target is selected,
    # THEN the profile is prepared before the installer starts containerd.
    path, identifier = apparmor_node
    docker('exec', identifier, 'rm', '/usr/local/bin/k3s')
    (path / 'proc/101/comm').unlink()
    refused = run_role(apparmor_node, k3s_version='v1.99.0+k3s1')
    assert refused.returncode != 0 and not policy(apparmor_node).exists()
    result = run_role(apparmor_node, k3s_version='v1.36.5+k3s1')
    assert result.returncode == 0, result.stdout + result.stderr
    assert policy(apparmor_node).read_bytes() == PROFILE.read_bytes()


def test_known_runtime_cannot_upgrade_to_an_unreviewed_target(apparmor_node):
    # GIVEN a supported running runtime, WHEN a different k3s upgrade is requested,
    # THEN it stops before replacing the future release's stronger default policy.
    result = run_role(apparmor_node, k3s_upgrade=True, k3s_version='v1.99.0+k3s1')
    assert result.returncode != 0 and 'reviewed containerd' in result.stdout
    assert not policy(apparmor_node).exists()


def test_check_mode_validates_eligibility_without_changing_the_kernel_or_file(apparmor_node):
    # GIVEN an affected supported node, WHEN Ansible previews the role, THEN
    # observations still run but neither persistent nor kernel policy is changed.
    result = run_role(apparmor_node, check_mode=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not policy(apparmor_node).exists()
    assert not (apparmor_node[0] / 'parser-calls.jsonl').exists()


def test_unrelated_service_tags_do_not_apply_a_node_policy(apparmor_node):
    # GIVEN a service-only operation, WHEN k3s is outside its selected tags,
    # THEN no node policy or kernel loading occurs as a side effect.
    result = run_role(apparmor_node, 'k3s', tags='postgres')
    assert result.returncode == 0, result.stdout + result.stderr
    assert not policy(apparmor_node).exists()
    assert not (apparmor_node[0] / 'parser-calls.jsonl').exists()
