# 0007: A synced release refers only to itself

## Status

Accepted

## Context

GitHub resolves `uses:` references literally, so a shared workflow cannot name
"my own revision" for the actions it calls. The shared workflows and actions
therefore referred to the owner copy's `main`. A project that pinned its
workflow references and `shared-roles.ref` still loaded those nested actions
from `main`, so the next `startup sync` changed what it ran, contrary to
[0001](0001-owner-copy-and-pinned-roles.md).

## Decision

This template keeps referring to the owner copy's `main`. `startup sync`
publishes every sync as a release tag in the owner copy, named after the synced
template version or source commit, and rewrites those references to that tag
before committing. The tag is created on the commit that contains them, so a
release resolves its workflows, actions and roles from itself. Sync never moves
a published tag. `scripts/check_release_refs.py` accepts only the two
consistent states, all `main` or all one tag that holds exactly those contents;
it runs in the tests here and in the owner copy's `check-pinned-runtime`
workflow.

## Consequences

- A project pinned to a release is unaffected by later syncs; adopting a newer
  release is an explicit, reviewable change in the project.
- The template stays valid for CLI versions that do not rewrite references;
  their syncs keep the previous behaviour.
- References to this repository must use the plain `uses: <owner copy>/.github/...@main`
  form in `.github/**/*.yml`; any other form is not rewritten.
- Commits synced before releases existed still load nested actions from `main`
  until the project adopts a release once.
- A tag is immutable by convention, so projects keep pinning the commit SHA;
  the tag names the release for people and for the nested references.
