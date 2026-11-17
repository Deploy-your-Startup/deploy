"""Run repository-owned checks through the real preparation command and gate."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("script,expected", [("exit 0", 0), ("exit 7", 7), (None, 127)])
@pytest.mark.parametrize("reuse", ["false", "true"])
def test_project_check_runs_before_reuse_and_blocks_publication(tmp_path, script, expected, reuse):
    # GIVEN a project check in a path containing shell punctuation, and the real workflow.
    path = tmp_path / "check assets; touch injected.sh"
    if script is not None:
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + script + "\n")
    jobs = workflow("verify-service-images.yml")["jobs"]
    steps = jobs["prepare"]["steps"]
    index = next(i for i, s in enumerate(steps) if s.get("name") == "Check project preflight")
    assert steps[index - 1]["uses"].startswith("actions/checkout@")
    assert index < next(i for i, s in enumerate(steps) if s.get("id") == "verified")
    step = steps[index]
    assert step["if"] == "inputs.preflight_script != ''"
    assert "continue-on-error" not in step
    # WHEN the exact shipped command runs from the checked-out repository root.
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path,
                            env={**os.environ, "PREFLIGHT_SCRIPT": path.name}, capture_output=True)
    assert result.returncode == expected
    assert not (tmp_path / "injected.sh").exists()
    # THEN failed/missing checks fail preparation and cannot pass the publication gate,
    # even when a previously verified PR image would otherwise be reusable.
    gate = jobs["checks"]["steps"][0]
    status = "skipped" if reuse == "true" else "success"
    result = subprocess.run(["bash", "-e", "-c", gate["run"]], env={**os.environ,
        "PREPARE_RESULT": "success" if expected == 0 else "failure", "REUSE": reuse,
        "PROOF_ID": "123", "INTEGRATION_RESULT": status, "IMAGE_RESULT": status,
        "E2E_RESULT": status, "RUN_E2E": "true"}, capture_output=True)
    assert (result.returncode == 0) == (expected == 0)


def test_existing_callers_opt_out_and_all_entrypoints_forward_the_check():
    # GIVEN existing callers without project preflight configuration.
    for name in ("build-and-deploy-service.yml", "build-and-deploy-service-parallel.yml", "verify-service-images.yml"):
        data = workflow(name)
        # THEN the extension defaults off and remains part of the complete reuse identity.
        assert data["on"]["workflow_call"]["inputs"]["preflight_script"]["default"] == ""
        if name != "verify-service-images.yml":
            caller = next(j for j in data["jobs"].values() if "preflight_script" in j.get("with", {}))
            assert caller["with"]["preflight_script"] == "${{ inputs.preflight_script }}"
