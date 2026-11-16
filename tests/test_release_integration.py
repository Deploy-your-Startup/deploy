"""Run the shipped release script as a process against real Git repositories."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, 'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
       'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}
WORKFLOW = ('jobs:\n  deploy:\n    steps:\n'
            '      - uses: Deploy-your-Startup/deploy/.github/actions/deploy@main\n'
            '      - uses: Deploy-your-Startup/deploy/.github/actions/join-tailnet@main\n'
            '      - uses: actions/checkout@main\n')


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True,
                          text=True, env=ENV).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """A clone of a published `main` that carries the shipped release tooling."""
    remote, work = tmp_path / 'remote.git', tmp_path / 'work'
    git(tmp_path, 'init', '--quiet', '--bare', '-b', 'main', str(remote))
    git(tmp_path, 'init', '--quiet', '-b', 'main', str(work))
    (work / 'scripts').mkdir()
    for name in ('release.py', 'check_release_refs.py'):
        (work / 'scripts' / name).write_text((ROOT / 'scripts' / name).read_text())
    (work / '.github/workflows').mkdir(parents=True)
    (work / '.github/workflows/deploy.yml').write_text(WORKFLOW)
    git(work, 'add', '.')
    git(work, 'commit', '--quiet', '-m', 'main')
    git(work, 'remote', 'add', 'origin', str(remote))
    git(work, 'push', '--quiet', 'origin', 'main')
    return work, remote


def release(work, *args):
    return subprocess.run(['python3', 'scripts/release.py', *args], cwd=work, capture_output=True,
                          text=True, env=ENV)


@pytest.mark.parametrize('repository', ['Deploy-your-Startup/deploy', 'example-owner/deploy'])
def test_a_release_names_only_itself_and_leaves_main_alone(checkout, repository):
    work, remote = checkout
    main = git(work, 'rev-parse', 'HEAD')
    # GIVEN a checked-out, published main, WHEN its owner or a fork releases it,
    result = release(work, 'v1.2.0', '--repository', repository)
    # THEN the remote holds a tag whose commit refers to itself and builds on main,
    assert result.returncode == 0, result.stdout + result.stderr
    commit = git(remote, 'rev-parse', 'v1.2.0^{commit}')
    assert git(remote, 'rev-parse', commit + '^') == main
    released = git(remote, 'show', 'v1.2.0:.github/workflows/deploy.yml')
    assert released.count(f'uses: {repository}/.github/actions/') == 2
    assert '@v1.2.0' in released and 'actions/checkout@main' in released
    assert 'Deploy-your-Startup/deploy/.github/actions/deploy@main' not in released
    assert commit in result.stdout
    # AND main and the working tree stay as they were.
    assert git(remote, 'rev-parse', 'main') == main
    assert git(work, 'status', '--porcelain') == ''
    assert (work / '.github/workflows/deploy.yml').read_text() == WORKFLOW


@pytest.mark.parametrize('scenario', ['dirty', 'unpublished', 'existing-tag', 'bad-version'])
def test_only_a_new_release_of_published_main_is_created(checkout, scenario):
    work, remote = checkout
    version = 'v1.2.0'
    # GIVEN a checkout that must not be released as it is,
    if scenario == 'dirty':
        (work / '.github/workflows/deploy.yml').write_text(WORKFLOW + '# local edit\n')
    elif scenario == 'unpublished':
        (work / 'later.yml').write_text('later\n')
        git(work, 'add', '.')
        git(work, 'commit', '--quiet', '-m', 'not pushed')
    elif scenario == 'existing-tag':
        assert release(work, version, '--repository', 'Deploy-your-Startup/deploy').returncode == 0
    else:
        version = '1.2'
    tags = git(remote, 'tag')
    # WHEN a release is requested,
    result = release(work, version, '--repository', 'Deploy-your-Startup/deploy')
    # THEN it is refused and the remote's tags are unchanged.
    assert result.returncode != 0
    assert git(remote, 'tag') == tags
