# 0007: A release refers only to itself

## Status

Accepted

## Context

GitHub resolves `uses:` references literally, so a shared workflow cannot name
"my own revision" for the actions it calls. If the shared workflows referred to
this repository's `main`, a project that pinned its workflow references and
`shared-roles.ref` would still load nested actions from `main`, and the next
merge would change what it runs, contrary to
[0001](0001-projects-pin-releases-directly.md).

## Decision

On `main`, references to this repository use the plain
`uses: Deploy-your-Startup/deploy/.github/...@main` form. `scripts/release.py
<version>` takes the commit at the remote `main`, rewrites those references to
the version tag, commits that on top of `main` and pushes only the annotated
tag, so a release resolves its workflows, actions and roles from itself. A
release commit is reachable only through its tag. `scripts/check_release_refs.py`
accepts only the two consistent states, all `main` or all one tag that holds
exactly those contents; it runs in the tests and in `check-pinned-runtime`.

## Consequences

- A project pinned to a release is unaffected by later merges.
- Any other form of self-reference in `.github/**/*.yml` is not rewritten.
- A fork releases with the same script; its releases refer to the fork.
- Tags are immutable by ruleset, yet projects pin the release commit SHA; the tag
  names the release for people and for the nested references.
