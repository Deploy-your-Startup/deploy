# Releasing and pinning

Merging to `main` here changes no project. A change reaches an application only
after it is released and the project adopts that release; see
[role distribution](role-distribution.md) for the path.

## Verification before release

After `mise run check`, exercise a fresh single-node project in an authorized
test account: first install, repeat infrastructure, application deployment, DNS
and HTTPS, backup and restore into a disposable database, then explicit
upgrades. Also verify private-node bootstrap with Tailscale and the ten-minute
join-key window. Review optional charts and provider settings separately.
Static checks do not establish successful provisioning or production readiness.

A feature that needs a CLI change is usable only once that CLI is released;
state the minimum CLI version in the release notes.

## Publishing a release

Check out the merged `main` without local changes, then run:

```sh
mise run release v1.2.0
```

`scripts/release.py` refuses anything but the commit at the remote `main` and a
version tag that does not exist yet. It rewrites every
`uses: Deploy-your-Startup/deploy/.github/...@main` to the version tag, commits
that on top of `main`, checks the result with `scripts/check_release_refs.py`
and pushes only the annotated tag. The release therefore resolves its workflows,
actions and roles from itself
([ADR 0007](adr/0007-releases-are-self-contained.md)). `main` keeps its `@main`
references. The tag ruleset lets only maintainers create tags, and published
tags never move. The script prints the release commit that projects pin.

A fork releases the same way; pass `--repository <owner>/<fork>` if its remote
is not a GitHub URL. Its releases then refer to the fork.

## Pinning a project's roles

Move the project to the latest release, or to a specific release:

```sh
startup update --check
startup update
startup ansible pin --working-directory deployment --version <release-tag>
```

Commit `shared-roles.ref` and `shared-roles.sha256` with the CLI dependency and
lock file. Subsequent runs use that immutable commit. CI exports must match the
checksum of role files and runtime configuration, even without Git metadata; a
mismatch fails before any role executes. Merging to `main` here does not change
a pinned project.

When `deployment/shared-roles.ref` exists, the `deploy` and
`export-shared-roles` actions dispatch to the action code at that exact commit.
Projects without a pin use the action's current revision. A runtime-equivalent
commit can change action code while leaving the role checksum unchanged, so
review the action diff too.

## Pinning workflow references

Point application workflows at the release commit, not `@main`:

```yaml
uses: Deploy-your-Startup/deploy/.github/workflows/build-and-deploy-service.yml@<release-commit>
```

`startup update` writes the release commit into the project's shared workflow
references together with the role pin; `startup ansible pin --version
<release-tag>` is the underlying step. Independently, `pin-ci-runtime` loads the
shared build actions from the caller's workflow commit, and `deploy` and
`export-shared-roles` follow `shared-roles.ref`.

## Moving from an owner copy

Projects that call a per-owner `<github-owner>/deploy-your-startup` copy switch
once:

```sh
startup migrate deploy --dry-run
startup migrate deploy
```

It points the workflow references and `shared-roles.ref` at the current release
of this repository and refreshes `deployment/.shared-roles`; review and commit
the result. Archive the owner copy only after all its projects have moved, and
do not delete it, so the old pins stay available for a rollback.

## Credentials in CI

The workflows take the caller's `VAULT_PASSWORD` secret and pass it to the CLI
as the scoped `STARTUP_VAULT_PASSWORD` environment variable. The CLI streams
the deploy key directly to `ssh-add`; no password enters a process argument.
Callers pass secrets explicitly; `secrets: inherit` does not cross owners.
