# Weekly role dependency audit

Dependabot in `.github/dependabot.yml` checks GitHub Actions weekly. It cannot
resolve Ansible role variables, templated images or the collection pins here.
The repository-only `ci-check-role-dependencies.yml` workflow checks these every
Monday at 07:23 UTC and can also be started manually. It updates one audit issue,
closes it when no findings remain and reopens it for new findings. It never
changes pins or deploys. GitHub schedules run from the default branch and may
start late. Merge the workflow into `main` to activate the schedule.

Run the same comparison locally:

```sh
uv run --locked python scripts/check_role_dependencies.py
```

The manifest in `scripts/role-dependencies.json` maps every explicit role version
default and Ansible collection to an official source. Add entries with new pins;
an integration check rejects untracked version defaults and collection pins.
The checker reads role defaults directly and compares numeric versions, not
lexical strings or release dates. It excludes prereleases, drafts, deprecated
charts and yanked Python packages, and follows upstream pagination.

k3s's same-series column stays on the current Kubernetes minor; other entries
stay on the current major. The latest stable column also exposes larger jumps.
Neither column proves compatibility. Check release notes, supported Kubernetes
versions and migration requirements before proposing an update. In particular,
PostgreSQL major versions require migration and Kubernetes Python client majors
follow Kubernetes releases. k3s uses its official stable and minor update channels,
avoiding GitHub's 1,000-release history limit. cert-manager and Hetzner chart
versions come from their official chart repository indexes.

Each run produces `report.md`, `report.json` and `sources.json` under
`reports/role-dependencies/`. CI retains them for 30 days. Replay the captured
upstream responses against the **same repository revision and manifest**:

```sh
uv run --locked python scripts/check_role_dependencies.py \
  --snapshot reports/role-dependencies/sources.json --output /tmp/role-audit-replay
```

The replay makes no network requests and produces identical reports for identical
inputs. A missing response, API error, malformed pin or source without stable
versions is an error, not an up-to-date result. CI publishes partial diagnostics
and fails if any dependency could not be checked. Available updates appear in
the issue without failing CI. `GH_TOKEN` optionally increases GitHub's API limit;
it is sent only to `api.github.com` and is never stored in the snapshot.

This audit covers version availability, not vulnerabilities, support lifecycles
or installed production versions. Ubuntu images, distribution-managed packages,
the unpinned Tailscale installation script and k3s-bundled components such as
Traefik require separate review. Consumer projects retain their release pins.

Tests exercise the real command with an HTTP registry stand-in: pagination,
numeric comparison, release filtering, source errors and offline replay. Issue
tests use a GitHub CLI boundary stand-in to cover create, update, close, reopen
and denied access. No cloud resources are needed; use `mise run check`.
