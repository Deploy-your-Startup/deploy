# 0009: One project deployment version

## Context

Projects duplicated their deployment revision in every workflow and in
`deployment/shared-roles.ref`, plus a separately committed runtime checksum.
Local operations and CI could disagree. GitHub requires literal reusable
workflow references, so a separate version setting would still need generated
copies in workflow YAML.

## Decision

The sole active deployment reference is the external `uses:` in
`.github/workflows/startup.yml`. Other project workflows call that local
workflow and retain their triggers, job dependencies, service settings and
explicit secret forwarding. The local workflow calls `project.yml` for server
applications or `project-pages.yml` for Pages. These thin shared workflows
route the requested operation to the existing implementations at the same
release commit. Valid operations allocate no additional runner.

The CLI reads the same reference for roles, updates, forks and template
preservation. `startup update` migrates supported direct callers and removes
the two legacy pin files. Unsupported custom calls fail before writing edits.
The release workflow's public input schema generates the local entry point.

Git checkouts must be clean and at the selected commit. Exports without Git
metadata are compared with an independently fetched tree of that commit.
No checksum supplied by an export is trusted and no second checksum is
committed. Legacy projects and explicit rollback to a pre-router release keep
the old pin contract; immutable published releases are not rewritten.

GitHub propagates permissions through the nested workflows without elevation.
Server callers therefore grant the shared build's permission set and Pages
callers grant `contents: read` and `deployments: write`. Secrets are forwarded
by name through each layer. See [GitHub's reuse documentation](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## Consequences

- One reviewed reference configures the next local or CI operation.
- The configured version does not establish the last successful deployment;
  the successful Actions run is the execution record.
- Copier answers remain historical render metadata, not deployment state.
- Old releases remain available for rollback, including their legacy pins.
- Application behavior and infrastructure playbooks are unchanged.
