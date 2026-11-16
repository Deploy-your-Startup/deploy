# 0001: Projects pin releases of this repository directly

## Status

Accepted

## Context

Every startup deploys with the same Ansible roles and GitHub workflows. Users
own their repositories, servers and credentials, and a shared change must never
alter a running production deployment without the owner noticing. This
repository is public and MIT-licensed, so any repository, private ones of other
owners included, may call its workflows and actions.

## Decision

Projects call `Deploy-your-Startup/deploy` directly and pin one release: their
workflow references and `deployment/shared-roles.ref` name the same release
commit, and `deployment/shared-roles.sha256` records its content checksum. The
CLI and the CI actions refuse a mismatch. `startup update` moves a project to
the latest release. An owner who needs changes forks this repository and pins
releases of the fork instead.

## Consequences

- Merging here changes no project; adopting a release is a reviewed change in
  each project.
- Pins are commits of this repository, so their history is traceable here.
- Projects depend on this repository staying available: `main` admits no force
  pushes or deletions and published release tags never move.
- Callers pass secrets explicitly, because `secrets: inherit` does not cross
  owners. Organizations that restrict Actions must allow this repository.
- Shared playbooks run from `.shared-roles/` and cannot read project
  `group_vars`; they rely on CLI extra-vars, node facts and role defaults.
- Several projects may lag behind; release notes must state the minimum CLI
  version a change needs.
