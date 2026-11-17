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

The only active reference is in `.github/workflows/startup.yml`. The CLI
migrates supported direct workflow calls to that local entry and removes
`shared-roles.ref` and `shared-roles.sha256`. Local operations read the same
reference. Upgrade the project-locked CLI dependency and lockfile before
migrating; a newer global installation alone does not replace that runtime.
Run `startup update` before adopting an application template that declares
`deployment_version: single`. Review the entry and caller changes together with the project's CLI
dependency and lockfile. The configured version describes the next operation;
a successful Actions deployment run is the execution record.

The shared `project.yml` and `project-pages.yml` workflows route operations to
the existing build, deploy, infrastructure and Pages workflows at the same
release commit. Local checkouts must be clean; CI exports are compared to an
independently fetched tree at that commit before Ansible executes.

Projects on older releases keep their legacy direct references and role pins.
An explicit rollback to a release without the project router restores that
legacy interface. Published releases and tags are never modified.

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
