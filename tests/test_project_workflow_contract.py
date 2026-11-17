"""Keep the single-version entry a transparent router to the existing workflows."""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1] / '.github/workflows'


def load(name):
    return yaml.safe_load((ROOT / name).read_text())


@pytest.mark.parametrize('router,operations', [
    ('project.yml', {'build', 'deploy', 'infrastructure'}),
    ('project-pages.yml', {'pages', 'e2e'}),
])
def test_router_forwards_the_public_contract_and_fails_unknown_operations(router, operations):
    # GIVEN the real workflows shipped in this repository.
    workflow = load(router)
    public = workflow.get('on', workflow.get(True))['workflow_call']
    # WHEN a project routes an operation through its single native reference.
    for operation in operations:
        job = workflow['jobs'][operation]
        definition = load(Path(job['uses']).name)
        nested = definition.get('on', definition.get(True))['workflow_call']
        # THEN every existing input and secret is explicitly forwarded without changed defaults.
        assert job['if'] == f"inputs.operation == '{operation}'"
        assert set(job['with']) == set(nested.get('inputs', {}))
        for name, value in job['with'].items():
            assert value == '${{ inputs.' + name + ' }}'
            assert public['inputs'][name].get('default') == nested['inputs'][name].get('default') or name in {'environment', 'service', 'project_name'}
        assert set(job.get('secrets', {})) == set(nested.get('secrets', {}))
    # AND unknown operations start a failing job, while valid calls allocate no extra runner.
    guard = workflow['jobs']['validate']
    assert all(f"inputs.operation != '{op}'" in guard['if'] for op in operations)
    assert 'exit 1' in guard['steps'][0]['run']
