"""Run the shipped release check as a process against real Git repositories."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/check_release_refs.py'
ENV = {**os.environ, 'GITHUB_REPOSITORY': 'example-owner/deploy-your-startup',
       'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.invalid',
       'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.invalid'}


def check(root):
    return subprocess.run(['python3', str(SCRIPT), str(root)], capture_output=True, text=True, env=ENV)


def test_the_template_refers_to_itself_only_in_the_form_sync_rewrites():
    # GIVEN the shipped workflows and actions, WHEN the release check reads them,
    result = check(ROOT)
    # THEN every reference to the owner copy is the `@main` form that sync pins to a release tag.
    assert result.returncode == 0, result.stderr
    assert 'references follow main' in result.stdout
    assert not result.stdout.startswith('0 ')


@pytest.mark.parametrize('scenario', ['release', 'mixed', 'moved-tag', 'missing-tag'])
def test_a_synced_owner_copy_must_be_one_self_contained_release(tmp_path, scenario):
    # GIVEN an owner copy as `startup sync` publishes it: references pinned to a release tag.
    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True, env=ENV)
    owner = 'example-owner/deploy-your-startup/.github/actions'
    workflow = tmp_path / '.github/workflows/deploy.yml'
    workflow.parent.mkdir(parents=True)
    second = 'main' if scenario == 'mixed' else 'v1.2.0'
    workflow.write_text(f'steps:\n  - uses: {owner}/deploy@v1.2.0\n  - uses: {owner}/join-tailnet@{second}\n'
                        '  - uses: actions/checkout@main\n')
    git('init', '-b', 'main')
    git('add', '.')
    git('commit', '-m', 'sync')
    if scenario != 'missing-tag':
        git('tag', 'v1.2.0')
    if scenario == 'moved-tag':
        (tmp_path / 'roles.yml').write_text('later contents\n')
        git('add', '.')
        git('commit', '-m', 'later')
    # WHEN the shipped check runs in that repository,
    result = check(tmp_path)
    # THEN only a release whose tag holds exactly these contents passes.
    assert (result.returncode == 0) is (scenario == 'release'), result.stdout + result.stderr
